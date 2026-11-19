#!/usr/bin/env python3
"""命令行：扫发票 → 填发票明细表 → 三表金额校验。

用法：
    python3 scripts/invoice_report.py --activity 惠民社区保障资金项目/11-共治行动-第一期
    python3 scripts/invoice_report.py --activity ... --out /tmp/发票明细表.xlsx

流程：
1. scan_invoices 读 12-发票/ 下的 PDF 与图片（文件名优先，正文交叉校验）
2. 把结果填进 templates/library/发票明细表.xlsx，默认写到 <活动>/12-发票/发票明细表.xlsx
3. cross_check 比对 决算 / 报销 / 发票 三个合计，打印结论与差异

不涉及内部审计表格：哪些模板不进项目资料见 config/excluded_templates.txt。
退出码：0 三表一致，1 活动目录不存在，3 三表不一致或资料缺失。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import openpyxl

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from engine import invoice  # noqa: E402
from scripts import gen_brief as brief  # noqa: E402

# 发票明细表模板（表头 7 列、20 行数据行、末行合计公式）
DETAIL_TEMPLATE = Path("/srv/social-work/templates/library/发票明细表.xlsx")
# 明细表默认落在发票目录，和票据原件放在一起
DETAIL_NAME = "发票明细表.xlsx"
MAX_ROWS = 20


def _money(value) -> str:
    """金额显示：None → 「—」，其余保留两位。"""
    return "—" if value is None else f"{value:.2f}"


def render_detail(items: list[dict], dst: Path) -> None:
    """把发票列表填进明细表模板，写到 dst（模板本身不改）。"""
    if not DETAIL_TEMPLATE.is_file():
        raise FileNotFoundError(f"发票明细表模板不存在：{DETAIL_TEMPLATE}")

    workbook = openpyxl.load_workbook(str(DETAIL_TEMPLATE))
    sheet = workbook.active
    for index, item in enumerate(items[:MAX_ROWS], start=2):
        sheet.cell(row=index, column=2, value=item.get("date") or "")
        sheet.cell(row=index, column=3, value=item.get("invoice_no") or "")
        sheet.cell(row=index, column=4, value=item.get("merchant") or "")
        sheet.cell(row=index, column=5, value=item.get("item") or "")
        amount = item.get("amount")
        if amount is not None:
            sheet.cell(row=index, column=6, value=float(amount))
        if item.get("parse_error"):
            sheet.cell(row=index, column=7, value="待核对")

    dst.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(dst))
    workbook.close()


def print_report(result: dict, act_dir: Path, dst: Path) -> None:
    """打印发票明细 + 三表结论。"""
    items = result["发票"]["items"]
    print(f"## 报销校验：{act_dir.parent.parent.name}/{act_dir.name}\n")

    if items:
        print(f"### 发票明细（{len(items)} 张）\n")
        print("| 序号 | 发票日期 | 发票号码 | 商户名称 | 项目内容 | 金额（元） | 备注 |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
        for seq, item in enumerate(items, start=1):
            note = item.get("parse_error") or ""
            print(f"| {seq} | {item.get('date') or '—'} | {item.get('invoice_no') or '—'} "
                  f"| {item.get('merchant') or '—'} | {item.get('item') or '—'} "
                  f"| {_money(item.get('amount'))} | {note or ''} |")
        print()

    print("### 三表金额\n")
    print(f"- 决算：{_money(result['决算']['value'])} 元"
          f"（{result['决算']['source']}）")
    print(f"- 报销：{_money(result['报销']['value'])} 元"
          f"（{result['报销']['source']}）")
    print(f"- 发票：{_money(result['发票']['value'])} 元"
          f"（{result['发票']['count']} 张）")

    diffs = "；".join(f"{key} {value:+.2f}" for key, value in result["diff"].items()
                     if value is not None) or "—"
    print(f"- 差异：{diffs}")
    print(f"- 结论：{'一致' if result['consistent'] else '不一致'}")

    if result["issues"]:
        print("\n### 问题\n")
        for issue in result["issues"]:
            print(f"- {issue}")

    print(f"\n明细表：{dst}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发票明细表 + 三表金额校验")
    parser.add_argument("--activity", required=True,
                        help="活动目录（可用 项目名/活动名 简写）")
    parser.add_argument("--out", help="明细表输出路径，默认 <活动>/12-发票/发票明细表.xlsx")
    args = parser.parse_args(argv)

    act_dir = brief._resolve_activity(args.activity)
    if act_dir is None:
        print(f"活动目录不存在：{args.activity}", file=sys.stderr)
        return 1

    dst = Path(args.out).expanduser() if args.out \
        else act_dir / invoice.INVOICE_SUBDIR / DETAIL_NAME

    items = invoice.scan_invoices(act_dir)
    if len(items) > MAX_ROWS:
        print(f"警告：发票 {len(items)} 张，超过明细表 {MAX_ROWS} 行，"
              f"只填前 {MAX_ROWS} 张", file=sys.stderr)
    try:
        render_detail(items, dst)
    except FileNotFoundError as exc:
        print(f"生成明细表失败：{exc}", file=sys.stderr)
        return 2

    result = invoice.cross_check(act_dir)
    print_report(result, act_dir, dst)
    return 0 if result["consistent"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
