#!/usr/bin/env python3
"""从 计划书.docx 的三张表生成 Excel 填写辅助表。

用法：
    python3 scripts/make_plan_xlsx.py --out /tmp/计划书-填写表.xlsx
    python3 scripts/make_plan_xlsx.py --template 计划书.docx --out ...

Word 模板不动：Excel 只负责让用户好填（下拉、自动算小计、行数放宽），
填完由 scripts/fill_plan_from_xlsx.py 写回 Word 的对应表格。

三个 sheet：
  环节安排  阶段/时间/内容/所需物资/人员/备注        20 行，阶段列带下拉
  预见困难  预计困难/解决方案                        10 行
  物资预算  类别/物资名称/数量\\规格/单价/小计        20 行 + 合计行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docx import Document
from docx.table import _Cell
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 小计公式与自动填表共用一份（engine/plan_sheet.fill_workbook 也写同一公式）
from engine.plan_sheet import SUBTOTAL_FORMULA  # noqa: E402

PLAN_TEMPLATE = Path("/srv/social-work/templates/library/计划书.docx")
DEFAULT_OUT = Path("/srv/social-work/templates/library/计划书-填写表.xlsx")

# (sheet 名, Word 表序号, 数据行数, 列宽)
SHEET_SPECS = (
    ("环节安排", 0, 20, (12, 16, 40, 22, 14, 16)),
    ("预见困难", 1, 10, (30, 44)),
    ("物资预算", 2, 20, (12, 22, 14, 12, 12)),
)

# 阶段列下拉选项
STAGES = ("准备阶段", "执行阶段", "总结归档")

HEADER_FONT = Font(bold=True)
HEADER_FILL = PatternFill("solid", fgColor="D9D9D9")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center")


def _headers(doc, table_index: int) -> list[str]:
    """取 Word 表第一行的表头文本（物理单元格，避免合并格重复计数）。"""
    table = doc.tables[table_index]
    if not table.rows:
        raise ValueError(f"Word 第 {table_index + 1} 张表没有表头行")
    # 用物理单元格取，避免合并单元格把同一格重复算成多列
    return [_Cell(tc, table).text.replace("\n", "").strip()
            for tc in table.rows[0]._tr.tc_lst]


def _style_header(sheet, columns: int) -> None:
    """表头加粗、浅灰底、居中；冻结首行。"""
    for col in range(1, columns + 1):
        cell = sheet.cell(row=1, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
    sheet.freeze_panes = "A2"


def _set_widths(sheet, widths: tuple[int, ...]) -> None:
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def _blank_rows(sheet, data_rows: int, columns: int) -> None:
    """把数据区写成空串，让空表也有 20/10 行的实际范围。

    不写空串的话 openpyxl 只保存表头，用户看到的是一张「1 行」的表。
    物资预算的单价列（D）留真空着，避免 D*数量 变成 #VALUE!。
    """
    for row in range(2, data_rows + 2):
        for col in range(1, columns + 1):
            sheet.cell(row=row, column=col, value="")


def _stage_validation(sheet, rows: int) -> None:
    """阶段列下拉，直接写进 xlsx（不用宏）。"""
    validation = DataValidation(
        type="list",
        formula1='"' + ",".join(STAGES) + '"',
        allow_blank=True,
        showDropDown=False,        # openpyxl 里 False 才是「显示下拉箭头」
    )
    validation.promptTitle = "阶段"
    validation.prompt = "请选择：准备阶段 / 执行阶段 / 总结归档"
    validation.errorTitle = "阶段取值无效"
    validation.error = "请从下拉列表中选择阶段"
    validation.showInputMessage = True
    validation.showErrorMessage = True
    sheet.add_data_validation(validation)
    validation.add(f"A2:A{rows + 1}")


def build(template: Path, out: Path) -> Path:
    """读 Word 表头，生成填写表并写到 out。"""
    template, out = Path(template), Path(out)
    if not template.is_file():
        raise FileNotFoundError(f"计划书模板不存在：{template}")

    doc = Document(str(template))
    if len(doc.tables) < len(SHEET_SPECS):
        raise ValueError(f"计划书模板应有 3 张表，实际 {len(doc.tables)} 张")

    workbook = Workbook()
    # 默认带一个空 sheet，先删掉，再按 SHEET_SPECS 的顺序新建三个
    workbook.remove(workbook.active)

    for sheet_name, table_index, data_rows, widths in SHEET_SPECS:
        headers = _headers(doc, table_index)
        if len(headers) != len(widths):
            raise ValueError(f"Word 第 {table_index + 1} 张表有 {len(headers)} 列，"
                             f"与预期 {len(widths)} 列不符")
        sheet = workbook.create_sheet(sheet_name)

        for col, header in enumerate(headers, start=1):
            sheet.cell(row=1, column=col, value=header)

        _style_header(sheet, len(headers))
        _set_widths(sheet, widths)
        # 物资预算只铺到 C 列：D 列留真空着，E 列另有小计公式
        _blank_rows(sheet, data_rows, 3 if sheet_name == "物资预算" else len(headers))

        if sheet_name == "环节安排":
            _stage_validation(sheet, data_rows)
        elif sheet_name == "物资预算":
            for row in range(2, data_rows + 2):
                # 单价、小计按两位小数显示
                sheet.cell(row=row, column=4).number_format = "0.00"
                sheet.cell(row=row, column=5).number_format = "0.00"
                sheet.cell(row=row, column=5,
                           value=SUBTOTAL_FORMULA.format(row=row))
            # 合计行：A 列写「合计」，E 列求和
            total_row = data_rows + 2
            sheet.cell(row=total_row, column=1, value="合计").font = HEADER_FONT
            total_cell = sheet.cell(row=total_row, column=5,
                                    value=f"=SUM(E2:E{data_rows + 1})")
            total_cell.number_format = "0.00"
            total_cell.font = HEADER_FONT

    out.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(out))
    workbook.close()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成计划书 Excel 填写辅助表")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="输出 xlsx 路径")
    parser.add_argument("--template", default=str(PLAN_TEMPLATE),
                        help="计划书.docx 模板路径")
    args = parser.parse_args(argv)

    try:
        path = build(Path(args.template), Path(args.out).expanduser())
    except (FileNotFoundError, ValueError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1

    print(f"已生成 {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
