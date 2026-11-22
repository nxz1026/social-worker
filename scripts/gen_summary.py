#!/usr/bin/env python3
"""命令行：从活动资料解析 facts，调 LLM 生成总结报告表 1 的 19 个文字格。

用法：
    python3 scripts/gen_summary.py --activity 惠民社区保障资金项目/11-共治行动-第一期 --style 务实
    python3 scripts/gen_summary.py --activity ... --dry-run    # 只打印 prompt
    python3 scripts/gen_summary.py --activity ... --out /tmp/总结报告.docx

facts 来源：
- 01-*.docx（计划书）：活动基本信息、问题现状 / 需求来源 / 回应思路、目的与目标、
  环节表、预计困难、预算合计、执行社工与项目负责人
- 06-*.docx（总结报告）：实际人数
- 08-*.docx（决算表）：决算合计
- 05-*.docx（满意度测评汇总表）：平均得分与回收问卷份数（可选）
- 11-活动照片资料/：照片数量（可选）

计划书解析直接复用 scripts/gen_brief.py 里已写好的函数，不改它的对外行为。
解析不到的字段留空并打印警告，不中断。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from engine import activity, fill, llm  # noqa: E402
from scripts import gen_brief as brief  # noqa: E402

# 总结报告模板（占位符：19 个表格文字 + 基本信息 + 人手分配）
SUMMARY_TEMPLATE = Path("/srv/social-work/templates/library/总结报告.docx")

# facts 的字段顺序，也是 prompt 里「活动基本信息」的顺序
FACT_KEYS = (
    "活动名称", "活动时间", "活动地点", "服务对象", "预计人数", "实际人数",
    "项目社工", "项目负责人", "项目名称", "问题现状", "需求来源", "回应思路",
    "活动目的", "活动目标", "环节", "预计困难", "决算金额", "预算金额",
    "满意度得分", "问卷份数", "照片数量",
)

# 可选字段：解析不到不告警（模板里也不依赖它们）
OPTIONAL_KEYS = ("满意度得分", "问卷份数", "照片数量")

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic"}


# --------------------------------------------------------------------------
# 计划书 / 佐证材料解析
# --------------------------------------------------------------------------

def _parse_person(md: str, label: str) -> str:
    """抓「项目社工：聂聃」这类落款；跳过「项目社工人数及分工：…」的说明行。"""
    match = re.search(rf"{re.escape(label)}[：:][ \t]*([^\s：:（(]+)", md)
    return match.group(1).strip() if match else ""


def _parse_difficulties(md: str) -> list[str]:
    """预计困难：计划书「预见困难及解决措施」表的第一列，一行一条。"""
    lines = md.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("| 预计困难")), None)
    if start is None:
        return []
    items: list[str] = []
    for line in lines[start + 1:]:
        if not line.startswith("|"):
            break                          # 表格结束
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells:
            continue
        if all(set(cell) <= {"-", " "} for cell in cells):
            continue                       # markdown 分隔行
        text = re.sub(r"^\d+\s*[.、]\s*", "", cells[0]).strip()
        text = brief._clean_item(text)
        if text:
            items.append(text)
    return items


def _parse_staff_count(md: str) -> int | None:
    """计划书「项目社工人数及分工：3人：…」→ 3。"""
    match = re.search(r"项目社工人数及分工[：:][ \t]*(\d+)\s*人", md)
    return int(match.group(1)) if match else None


def _parse_score(md: str) -> float | None:
    """满意度汇总表「平均得分 | 91.40 分」。"""
    match = re.search(r"\|\s*平均得分\s*\|\s*([\d.]+)", md)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_questionnaires(md: str) -> int | None:
    """满意度汇总表「回收问卷份数 | 10 份」。"""
    match = re.search(r"\|\s*回收问卷份数\s*\|\s*(\d+)", md)
    return int(match.group(1)) if match else None


def _count_photos(act_dir: Path) -> int | None:
    """活动照片资料目录里的图片数；目录不存在返回 None。"""
    folder = act_dir / "11-活动照片资料"
    if not folder.is_dir():
        return None
    return sum(1 for path in folder.rglob("*")
               if path.is_file() and path.suffix.lower() in PHOTO_SUFFIXES)


def build_facts(act_dir: Path) -> dict:
    """活动目录 → summary 的 facts。缺的字段留空串 / None，由调用方决定是否告警。"""
    plan_md = brief._to_md(brief._first_docx(act_dir, "活动计划"))
    report_md = brief._to_md(brief._first_docx(act_dir, "总结报告"))
    budget_md = brief._to_md(brief._first_docx(act_dir, "决算表"))
    survey_md = brief._to_md(brief._first_docx(act_dir, "满意度测评汇总表"))

    expected = brief._grab(plan_md, "预计参与人数", "预计人数", "参与人数")
    goals = brief._parse_goals(plan_md)
    facts = {
        "活动名称": brief._grab(plan_md, "活动名称"),
        "活动时间": brief._cn_date(brief._grab(plan_md, "活动时间")),
        "活动地点": brief._grab(plan_md, "活动地点"),
        "服务对象": brief._grab(plan_md, "服务对象"),
        "预计人数": int(expected) if expected.isdigit() else expected,
        "实际人数": brief._parse_actual_count(report_md),
        "项目社工": _parse_person(plan_md, "项目社工"),
        "项目负责人": _parse_person(plan_md, "项目负责人"),
        "项目名称": brief._project_title(brief._project_name(act_dir)),
        "问题现状": brief._grab(plan_md, "问题现状"),
        "需求来源": brief._grab(plan_md, "需求来源"),
        "回应思路": brief._grab(plan_md, "回应思路"),
        "活动目的": brief._line_after(plan_md, "活动目的"),
        # 总结报告骨架里活动目标是一段话，不是列表
        "活动目标": "；".join(goals),
        "环节": brief._parse_steps(plan_md),
        "预计困难": _parse_difficulties(plan_md),
        "决算金额": brief._parse_total_amount(budget_md),
        "预算金额": brief._parse_total_amount(plan_md),
        "满意度得分": _parse_score(survey_md),
        "问卷份数": _parse_questionnaires(survey_md),
        "照片数量": _count_photos(act_dir),
    }

    missing = [key for key in FACT_KEYS
               if key not in OPTIONAL_KEYS and facts.get(key) in (None, "", [])]
    if missing:
        print(f"警告：未解析到 {len(missing)} 个字段：{'、'.join(missing)}",
              file=sys.stderr)
    return {key: facts[key] for key in FACT_KEYS}


# --------------------------------------------------------------------------
# 写回 docx
# --------------------------------------------------------------------------

def _staffing(facts: dict, act_dir: Path) -> str:
    """按项目 yaml 的 staff + 计划书的社工人数推导「N名项目社工」。"""
    project_name = brief._project_name(act_dir)
    extra = {}
    count = _parse_staff_count(brief._to_md(brief._first_docx(act_dir, "活动计划")))
    if count:
        extra["社工人数"] = count
    try:
        data = activity.build_data(project_name, str(act_dir), extra=extra or None)
    except (FileNotFoundError, ValueError):
        return f"{count}名项目社工" if count else ""
    return str(data.get("人手分配") or "")


def render_summary(facts: dict, result: dict, out: Path, staffing: str = "") -> None:
    """把 19 个格子与基本信息填进 总结报告.docx。"""
    if not SUMMARY_TEMPLATE.is_file():
        raise FileNotFoundError(f"总结报告模板不存在：{SUMMARY_TEMPLATE}")

    data = {
        "项目名称": facts.get("项目名称") or "",
        "活动名称": facts.get("活动名称") or "",
        "服务对象": facts.get("服务对象") or "",
        "活动时间": facts.get("活动时间") or "",
        "活动地点": facts.get("活动地点") or "",
        "实际人数": facts.get("实际人数"),
        "人手分配": staffing,
        "项目社工": facts.get("项目社工") or "",
        "项目负责人": facts.get("项目负责人") or "",
        "年月日": facts.get("活动时间") or "",
    }
    data.update(result)                    # 19 个文字格
    stats = fill.render_docx(SUMMARY_TEMPLATE, out, data)
    print(f"已写入 {out}（填充 {stats['filled']} 处，"
          f"未匹配 {len(stats['missing'])} 个）", file=sys.stderr)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def _choose_style(args_style: str, project_name: str, activity_name: str) -> str:
    """定文风：显式 --style 优先；未指定时默认不套党建风。"""
    explicit = str(args_style or "").strip()
    if explicit:
        return explicit
    style = llm.resolve_style(project_name, activity_name)
    if style == "党建":
        fallback = llm.load_style_config()["default"]
        print(f"提示：总结报告默认不套党建风，{style} → {fallback}", file=sys.stderr)
        return fallback
    return style


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成活动总结报告表 1（LLM）")
    parser.add_argument("--activity", required=True,
                        help="活动目录（可用 项目名/活动名 简写）")
    parser.add_argument("--style", default="",
                        help="文风：务实 / 平实 / 党建；不填按项目与活动类型推断")
    parser.add_argument("--dry-run", action="store_true", help="只打印 prompt")
    parser.add_argument("--out", help="填进 总结报告.docx 模板后输出的路径")
    args = parser.parse_args(argv)

    act_dir = brief._resolve_activity(args.activity)
    if act_dir is None:
        print(f"活动目录不存在：{args.activity}", file=sys.stderr)
        return 1

    facts = build_facts(act_dir)
    style = _choose_style(args.style, brief._project_name(act_dir), act_dir.name)
    print(f"文风：{style}", file=sys.stderr)

    if args.dry_run:
        print(llm.build_prompt("summary", facts, style))
        return 0

    try:
        result = llm.write("summary", facts, style=style)
    except llm.LLMError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.out:
        try:
            render_summary(facts, result, Path(args.out).expanduser(),
                           staffing=_staffing(facts, act_dir))
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"写回 docx 失败：{exc}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
