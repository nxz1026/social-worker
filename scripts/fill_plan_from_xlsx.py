#!/usr/bin/env python3
"""用 Excel 填写表的数据填充 计划书.docx。

用法：
    python3 scripts/fill_plan_from_xlsx.py \
      --activity 惠民社区保障资金项目/11-共治行动-第一期 \
      --sheet /tmp/计划书-填写表.xlsx \
      --out /tmp/计划书-已填.docx

流程：activity.build_data() 拿项目基础数据 → plan_sheet.read_workbook()
读 Excel → plan_sheet.to_fill_data() 合并 → fill.render_docx() 出 docx。
模板文件只读，不会被原地修改。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# 复用 gen_brief 里已跑通的活动目录定位（支持「项目名/活动名」简写）
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import gen_brief  # noqa: E402
from engine import activity, fill, plan_sheet  # noqa: E402

PLAN_TEMPLATE = Path("/srv/social-work/templates/library/计划书.docx")
DEFAULT_OUT = Path("/tmp/计划书-已填.docx")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 Excel 填写表填充计划书")
    parser.add_argument("--activity", required=True,
                        help="活动目录（可用 项目名/活动名 简写）")
    parser.add_argument("--sheet", required=True, help="填写表 xlsx 路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 docx 路径")
    args = parser.parse_args(argv)

    act_dir = gen_brief._resolve_activity(args.activity)
    if act_dir is None:
        print(f"活动目录不存在：{args.activity}", file=sys.stderr)
        return 1
    if not PLAN_TEMPLATE.is_file():
        print(f"计划书模板不存在：{PLAN_TEMPLATE}", file=sys.stderr)
        return 1

    project_name = gen_brief._project_name(act_dir)
    base = activity.build_data(project_name, str(act_dir))

    try:
        book = plan_sheet.read_workbook(Path(args.sheet).expanduser())
    except (FileNotFoundError, ValueError) as exc:
        print(f"读取填写表失败：{exc}", file=sys.stderr)
        return 2

    data = plan_sheet.to_fill_data(book, base)
    out = Path(args.out).expanduser()
    try:
        stats = fill.render_docx(PLAN_TEMPLATE, out, data)
    except (FileNotFoundError, PermissionError) as exc:
        print(f"写回 docx 失败：{exc}", file=sys.stderr)
        return 3

    print(f"活动：{act_dir.name}（项目：{project_name or '未识别'}）")
    print(f"读到：环节 {len(book['环节'])} 条 / 困难 {len(book['困难'])} 条 / "
          f"物资 {len(book['物资'])} 条，物资合计 {book['物资合计']:.2f} 元")
    print(f"filled: {stats['filled']}")
    print(f"missing: {'、'.join(stats['missing']) if stats['missing'] else '无'}")
    print(f"warning: {data['extra']['warning'] or '无'}")
    print(f"输出: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
