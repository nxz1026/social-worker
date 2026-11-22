#!/usr/bin/env python3
"""命令行：生成活动计划书的文字部分，并填进 计划书.docx。

用法：
    python3 scripts/gen_plan.py --activity 惠民社区保障资金项目/11-共治行动-第一期 --style 务实
    python3 scripts/gen_plan.py --activity ... --dry-run    # 只打印 prompt
    python3 scripts/gen_plan.py --activity ... --out /tmp/计划书.docx

facts 来源：activity.yaml > 已有 01-*.docx（计划书）> 项目 yaml > 目录名推断。
解析不到的字段留空并打印警告，不中断——模型按现有事实写，不会凭空编造。

物资预算表（物资名称/规格/单价/小计）本工单不生成，输出时显式填空，
避免模板里留下 {{物资名称_1}} 这类原始占位符。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# 复用简报脚本里已跑通的活动目录定位与计划书解析，避免维护两套正则
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import gen_brief  # noqa: E402
from engine import activities, llm, projects  # noqa: E402
from engine import activity as activity_data  # noqa: E402
from engine.ingest import read_activity_yaml  # noqa: E402

# 计划书模板
PLAN_TEMPLATE = Path("/srv/social-work/templates/library/计划书.docx")

# facts 的字段顺序，也是 prompt 里「活动基本信息」的顺序
FACT_KEYS = ("活动名称", "活动时间", "活动地点", "服务对象", "预计人数",
             "项目社工", "志愿者人数", "项目名称", "活动类型",
             "项目背景", "预算上限")

# 物资预算表的占位符前缀；本工单不填，渲染时统一置空
BUDGET_PREFIXES = ("物资名称", "物资规格", "物资单价", "物资小计")
BUDGET_ROWS = 5


def _text(value) -> str:
    """空值统一成空串，其余去空白转字符串。"""
    return str(value).strip() if value is not None else ""


def _as_int(value):
    """预计人数这类字段转 int；转不了原样返回（留空或写「20人」都能过）。"""
    text = _text(value)
    return int(text) if text.isdigit() else text


def _project_data(project_name: str) -> dict:
    """读项目 yaml；项目目录或 yaml 缺失时返回空 dict。"""
    if not project_name:
        return {}
    try:
        return projects.load_project(project_name)
    except (FileNotFoundError, ValueError):
        return {}


def _strip_quotes(text: str) -> str:
    """剥掉整串包住的引号（项目 yaml 的 name 常写成 '"…"街区…'）。"""
    text = _text(text)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _activity_budget(project_data: dict, dirname: str):
    """该活动的预算上限：项目 indicators 按 count 均摊后的 activity_budget。"""
    for item in activities.plan_activities(project_data):
        if item.get("dirname") == dirname:
            return item.get("activity_budget")
    return None


def _project_background(project_data: dict, activity_type: str) -> str:
    """项目 yaml → 一段项目背景，给模型当上下文（不直接进文档）。"""
    proj = project_data.get("project") if isinstance(project_data.get("project"), dict) else {}
    period = proj.get("period") if isinstance(proj.get("period"), dict) else {}
    budget = project_data.get("budget") if isinstance(project_data.get("budget"), dict) else {}

    title = _strip_quotes(proj.get("name"))
    category = _text(proj.get("category"))
    level = _text(proj.get("level"))
    bits: list[str] = []
    if title:
        bits.append(f"项目「{title}」")
    if category and level:
        bits.append(f"属{category}（{level}）")
    elif category or level:
        bits.append(f"属{category or level}")
    if period.get("start") and period.get("end"):
        bits.append(f"执行期 {period['start']} 至 {period['end']}")
    if period.get("months"):
        bits.append(f"共 {period['months']} 个月")
    if budget.get("total"):
        bits.append(f"立项资金 {budget['total']} 元")

    # 同类活动的指标与总预算：让模型知道这次是一次系列活动中的一期
    for ind in project_data.get("indicators") or []:
        if not isinstance(ind, dict):
            continue
        if _text(ind.get("name")) == activity_type:
            target = _text(ind.get("target"))
            amount = _text(ind.get("budget"))
            if target or amount:
                bits.append(f"「{activity_type}」指标 {target}"
                            + (f"、该项预算 {amount} 元" if amount else ""))
            break
    return "，".join(bit for bit in bits if bit)


def build_facts(act_dir: Path) -> dict:
    """活动目录 → plan 的 facts。缺的字段留空，由调用方决定是否告警。"""
    project_name = gen_brief._project_name(act_dir)
    base = activity_data.build_data(project_name, str(act_dir))
    yaml_act = read_activity_yaml(act_dir)
    plan_md = gen_brief._to_md(gen_brief._first_docx(act_dir, "活动计划"))
    project_data = _project_data(project_name)
    act_type = llm._activity_type(act_dir.name)

    expected = _text(yaml_act.get("预计人数")) or gen_brief._grab(
        plan_md, "预计参与人数", "预计人数", "参与人数")
    facts = {
        "活动名称": (_text(yaml_act.get("活动名称")) or _text(yaml_act.get("name"))
                     or gen_brief._grab(plan_md, "活动名称")
                     or _text(base.get("活动名称"))),
        "活动时间": (_text(yaml_act.get("活动时间")) or _text(yaml_act.get("time"))
                     or gen_brief._cn_date(gen_brief._grab(plan_md, "活动时间"))),
        "活动地点": (_text(yaml_act.get("活动地点")) or _text(yaml_act.get("place"))
                     or gen_brief._grab(plan_md, "活动地点")),
        "服务对象": _text(yaml_act.get("服务对象")) or gen_brief._grab(plan_md, "服务对象"),
        "预计人数": _as_int(expected),
        "项目社工": (_text(yaml_act.get("项目社工")) or _text(yaml_act.get("执行社工"))
                     or gen_brief._grab(plan_md, "项目社工人数及分工", "项目社工")
                     or _text(base.get("项目社工"))),
        "志愿者人数": (_text(yaml_act.get("志愿者人数"))
                       or gen_brief._grab(plan_md, "志愿者人数及分工", "志愿者人数")),
        "项目名称": gen_brief._project_title(project_name),
        "活动类型": act_type,
        "项目背景": _project_background(project_data, act_type),
        "预算上限": _activity_budget(project_data, act_dir.name),
    }

    missing = [key for key in FACT_KEYS if facts.get(key) in (None, "", [])]
    if missing:
        print(f"警告：未解析到 {len(missing)} 个字段：{'、'.join(missing)}",
              file=sys.stderr)
    return {key: facts[key] for key in FACT_KEYS}


def render_plan(project_name: str, act_dir: Path, facts: dict,
                result: dict, out: Path) -> list[str]:
    """把 LLM 结果填进 计划书.docx，返回截断等提示。"""
    from engine import fill                       # 延迟导入，--dry-run 不必装 python-docx

    if not PLAN_TEMPLATE.is_file():
        raise FileNotFoundError(f"计划书模板不存在：{PLAN_TEMPLATE}")

    notes: list[str] = []
    data = activity_data.build_data(
        project_name, str(act_dir), extra={**facts, **result}, notes=notes)
    # 物资预算表由用户输入或从预算推，这里显式置空
    for prefix in BUDGET_PREFIXES:
        for seq in range(1, BUDGET_ROWS + 1):
            data.setdefault(f"{prefix}_{seq}", "")
    data.setdefault("物资小计", "")

    stats = fill.render_docx(PLAN_TEMPLATE, out, data)
    print(f"已写入 {out}（填充 {stats['filled']} 处，"
          f"未匹配 {len(stats['missing'])} 个）", file=sys.stderr)
    if stats["missing"]:
        print(f"警告：模板里还有未填占位符：{'、'.join(stats['missing'])}",
              file=sys.stderr)
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成活动计划书（LLM）")
    parser.add_argument("--activity", required=True,
                        help="活动目录（可用 项目名/活动名 简写）")
    parser.add_argument("--style", default="",
                        help="文风：务实 / 平实 / 党建；不填按项目与活动类型推断")
    parser.add_argument("--dry-run", action="store_true", help="只打印 prompt")
    parser.add_argument("--out", help="填进 计划书.docx 模板后输出的路径")
    args = parser.parse_args(argv)

    act_dir = gen_brief._resolve_activity(args.activity)
    if act_dir is None:
        print(f"活动目录不存在：{args.activity}", file=sys.stderr)
        return 1

    project_name = gen_brief._project_name(act_dir)
    facts = build_facts(act_dir)
    style = llm.resolve_style(project_name, act_dir.name, args.style)
    print(f"文风：{style}", file=sys.stderr)

    if args.dry_run:
        print(llm.build_prompt("plan", facts, style))
        return 0

    try:
        result = llm.write("plan", facts, style=style)
    except llm.LLMError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.out:
        try:
            notes = render_plan(project_name, act_dir, facts, result,
                                Path(args.out).expanduser())
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"写回 docx 失败：{exc}", file=sys.stderr)
            return 3
        for note in notes:
            print(f"提示：{note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
