#!/usr/bin/env python3
"""自动填写 计划书-填写表.xlsx：生成空表后直接把内容写进去，人只做审核修改。

用法：
    python3 scripts/autofill_plan_xlsx.py \
      --activity 惠民社区保障资金项目/11-共治行动-第一期 \
      --out /tmp/计划书-自动填写.xlsx
    python3 scripts/autofill_plan_xlsx.py --activity ... --dry-run   # 只打印将写入的数据

数据来源：
  1. 活动目录已有 01-*.docx 计划书 → engine.plan_parse 确定性解析，不经 LLM
  2. 没有可解析的计划书 → engine.llm.write("plan", facts) 生成（含物资清单）

流程：make_plan_xlsx.build() 出空表到临时文件 → fill_workbook() 写入 → 落到 --out。
物资清单由 LLM 按 facts 里的「预算上限」控制，脚本再核对一次并在超预算时告警。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# 复用已跑通的活动目录定位与 facts 提取，避免维护第二套解析
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import gen_brief  # noqa: E402
import gen_plan  # noqa: E402
import make_plan_xlsx  # noqa: E402
from engine import llm, plan_parse, plan_sheet  # noqa: E402

DEFAULT_OUT = Path("/tmp/计划书-自动填写.xlsx")

# 环节阶段的关键词：LLM 只给「内容/物资/人员」，阶段按内容判断
_PREP_WORDS = ("签到", "集合", "开场", "分工", "布置", "勘查", "就位", "宣读",
               "培训", "发布", "张贴", "通知")
_SUMMARY_WORDS = ("总结", "归档", "离场", "复原", "回访", "收尾", "复盘")


def _stages(contents: list[str]) -> list[str]:
    """按环节内容判断阶段，并保证阶段只前进不回退。

    LLM 只给「内容/物资/人员」，阶段只能从内容里推。关键词可能误伤
    （例如执行环节里写「培训安排」），所以按顺序取累计最大值：
    一旦进入执行阶段，后面的「准备」关键词不再把阶段拉回去。
    """
    ranks: list[int] = []
    for text in contents:
        if any(word in text for word in _SUMMARY_WORDS):
            ranks.append(2)
        elif any(word in text for word in _PREP_WORDS):
            ranks.append(0)
        else:
            ranks.append(1)
    for index in range(1, len(ranks)):
        ranks[index] = max(ranks[index], ranks[index - 1])

    names = ("准备阶段", "执行阶段", "总结归档")
    return [names[rank] for rank in ranks]


def _book_from_plan(result: dict, facts: dict) -> dict:
    """plan task 的输出 → plan_sheet.fill_workbook 认的 book 结构。

    plan 的环节只有 内容/物资/人员，这里补上阶段与时间；物资的
    名称/规格 改写成填写表的列名 物资名称/数量\\规格。
    """
    time_text = str(facts.get("活动时间") or "")
    contents = [str(item.get("内容") or "") for item in result.get("环节") or []]
    stages = _stages(contents)

    steps = []
    for item, content, stage in zip(result.get("环节") or [], contents, stages):
        steps.append({
            "阶段": stage,
            "时间": time_text,
            "内容": content,
            "所需物资": str(item.get("物资") or ""),
            "人员": str(item.get("人员") or ""),
            "备注": "",
        })

    difficulties = [
        {"预计困难": str(item.get("困难") or ""),
         "解决方案": str(item.get("方案") or "")}
        for item in result.get("预计困难") or []
    ]

    materials = [
        {
            "类别": str(item.get("类别") or ""),
            "物资名称": str(item.get("名称") or item.get("物资名称") or ""),
            "数量\\规格": str(item.get("规格") or item.get("数量\\规格") or ""),
            "单价": plan_sheet._money(item.get("单价")),
        }
        for item in result.get("物资") or []
    ]
    return {"环节": steps, "困难": difficulties, "物资": materials}


def _material_total(book: dict) -> float:
    """物资合计：与填写表小计公式同口径（单价 × 数量）。"""
    return round(sum(plan_sheet._subtotal(item) for item in book.get("物资") or []), 2)


def _print_budget(book: dict, budget) -> None:
    """打印物资合计与预算上限的对比；超预算在 stderr 告警。"""
    total = _material_total(book)
    if budget in (None, ""):
        print(f"物资合计：{total:.2f} 元（预算上限：未提供）")
        return

    try:
        limit = float(budget)
    except (TypeError, ValueError):
        print(f"物资合计：{total:.2f} 元（预算上限：{budget}）")
        return

    share = f"{total / limit * 100:.1f}%" if limit else "—"
    print(f"物资合计：{total:.2f} 元 / 预算上限 {limit:.0f} 元（占 {share}）")
    if limit and total > limit:
        print(f"警告：物资合计超出预算上限 {total - limit:.2f} 元，请人工核减",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自动填写计划书 Excel 填写表")
    parser.add_argument("--activity", required=True,
                        help="活动目录（可用 项目名/活动名 简写）")
    parser.add_argument("--style", default="",
                        help="文风：务实 / 平实 / 党建；不填按项目与活动类型推断")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 xlsx 路径")
    parser.add_argument("--template", default=str(make_plan_xlsx.PLAN_TEMPLATE),
                        help="计划书.docx 模板路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将写入的数据，不生成文件")
    args = parser.parse_args(argv)

    act_dir = gen_brief._resolve_activity(args.activity)
    if act_dir is None:
        print(f"活动目录不存在：{args.activity}", file=sys.stderr)
        return 1

    project_name = gen_brief._project_name(act_dir)
    facts = gen_plan.build_facts(act_dir)

    # 先试确定性解析：已有真实计划书就不花 LLM 调用
    book = plan_parse.parse_existing_plan(act_dir)
    if book is None:
        style = llm.resolve_style(project_name, act_dir.name, args.style)
        print(f"没有可解析的 01-*.docx，改用 LLM 生成（文风：{style}）",
              file=sys.stderr)
        try:
            result = llm.write("plan", facts, style=style)
        except llm.LLMError as exc:
            print(f"生成失败：{exc}", file=sys.stderr)
            return 2
        book = _book_from_plan(result, facts)
        source = f"LLM 生成（{style}）"
    else:
        source = "已有计划书解析"

    print(f"数据来源：{source}")
    print(f"活动：{act_dir.name}（项目：{project_name or '未识别'}）")
    print(f"将写入：环节 {len(book['环节'])} 条 / 困难 {len(book['困难'])} 条 / "
          f"物资 {len(book['物资'])} 条")
    _print_budget(book, facts.get("预算上限"))

    if args.dry_run:
        print(json.dumps(book, ensure_ascii=False, indent=2))
        return 0

    template = Path(args.template).expanduser()
    if not template.is_file():
        print(f"计划书模板不存在：{template}", file=sys.stderr)
        return 1

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="autofill-plan-") as tmp:
            blank = make_plan_xlsx.build(template, Path(tmp) / "计划书-填写表.xlsx")
            counts = plan_sheet.fill_workbook(blank, book)
            shutil.move(str(blank), str(out))
    except (FileNotFoundError, ValueError, PermissionError) as exc:
        print(f"生成填写表失败：{exc}", file=sys.stderr)
        return 3

    print(f"fill_workbook：环节 {counts['环节']} / 困难 {counts['困难']} / "
          f"物资 {counts['物资']}")
    print(f"输出：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
