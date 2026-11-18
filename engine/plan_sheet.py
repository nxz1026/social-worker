#!/usr/bin/env python3
"""计划书 Excel 填写辅助表：读回 xlsx，转成 fill.render_docx 认的 data dict。

Excel 由 scripts/make_plan_xlsx.py 从 计划书.docx 的三张表生成：
  环节安排（20 行）/ 预见困难（10 行）/ 物资预算（20 行 + 合计行）

约定：
- 只收「关键列非空」的行：环节看内容，困难看预计困难，物资看物资名称。
- 小计一律按「单价 × 数量」重算，不读 Excel 里的公式结果——用户可能在
  没装 Excel 的环境里填表，公式没有缓存值。
- 数量\规格里的前导数字视为数量（"2张" → 2，"5米" → 5），取不出按 1 算，
  与 Excel 小计公式的口径一致。
"""
from __future__ import annotations

import re
from copy import copy
from pathlib import Path

import openpyxl

# sheet 名与 Word 三张表的对应关系
SHEET_STEP = "环节安排"
SHEET_DIFFICULTY = "预见困难"
SHEET_MATERIAL = "物资预算"

# 各 sheet 的列顺序，与 Word 表头一致（生成脚本从 docx 表头读出后照抄）
STEP_FIELDS = ("阶段", "时间", "内容", "所需物资", "人员", "备注")
DIFFICULTY_FIELDS = ("预计困难", "解决方案")
MATERIAL_FIELDS = ("类别", "物资名称", "数量\\规格", "单价", "小计")

# Word 模板的物理数据行数：环节表 5 行、困难表 4 行、物资表 5 行
STEP_ROWS = 5
DIFFICULTY_ROWS = 4
MATERIAL_ROWS = 5

# 数量\规格的前导数字："2张" → 2，"1.5米" → 1.5，"两箱" → 取不到
_QUANTITY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)")

# 小计公式：先按「个」取前缀，取不出再按前导数字取，最后按 1 算。
# 与 scripts/make_plan_xlsx.py 共用同一份，避免两处口径漂移。
SUBTOTAL_FORMULA = (
    '=D{row}*IFERROR(VALUE(LEFT(C{row},FIND("个",C{row}&"个")-1)),'
    'IFERROR(LOOKUP(9E+307,--LEFT(C{row},ROW($1:$15))),1))'
)


def _text(value) -> str:
    """单元格值转文本：None → 空串，整数值浮点去掉 .0。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _money(value) -> float:
    """单价转数字：容忍 "50"、"50.5"、"￥50"、"50元"、千分位；转不了按 0。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raw = _text(value)
    for token in ("￥", "¥", "元", ",", "，", " "):
        raw = raw.replace(token, "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _quantity(value) -> float:
    """从「数量\\规格」里取数量：前导数字，取不出按 1 算。"""
    if isinstance(value, bool):
        return 1.0
    if isinstance(value, (int, float)):
        return float(value)
    match = _QUANTITY_RE.match(_text(value))
    return float(match.group(1)) if match else 1.0


def _subtotal(item: dict) -> float:
    """小计 = 单价 × 数量，保留两位小数。"""
    return round(_money(item.get("单价")) * _quantity(item.get("数量\\规格")), 2)


def _rows(sheet, fields: tuple[str, ...], key: str) -> list[dict]:
    """从第 2 行起读数据行，关键列 key 为空的行跳过。"""
    result: list[dict] = []
    for values in sheet.iter_rows(min_row=2, max_col=len(fields), values_only=True):
        row = dict(zip(fields, values))
        if not _text(row.get(key)):
            continue
        result.append({name: _text(row.get(name)) for name in fields})
    return result


def read_workbook(path: Path) -> dict:
    """读回填写表，返回环节 / 困难 / 物资 / 物资合计。

    物资行里的「小计」按 单价×数量 重算；「物资合计」是这些重算小计之和。
    sheet 缺失时抛 ValueError（多半是用户改坏了工作表名）。
    """
    path = Path(path)
    workbook = openpyxl.load_workbook(str(path), data_only=False)
    try:
        missing = [name for name in (SHEET_STEP, SHEET_DIFFICULTY, SHEET_MATERIAL)
                   if name not in workbook.sheetnames]
        if missing:
            raise ValueError(f"填写表缺少工作表：{'、'.join(missing)}")

        steps = _rows(workbook[SHEET_STEP], STEP_FIELDS, "内容")
        difficulties = _rows(workbook[SHEET_DIFFICULTY], DIFFICULTY_FIELDS, "预计困难")
        materials = _rows(workbook[SHEET_MATERIAL], MATERIAL_FIELDS, "物资名称")
    finally:
        workbook.close()

    for item in materials:
        item["单价"] = _money(item.get("单价"))
        item["小计"] = _subtotal(item)

    total = round(sum(item["小计"] for item in materials), 2)
    return {
        "环节": steps,
        "困难": difficulties,
        "物资": materials,
        "物资合计": total,
    }


# --------------------------------------------------------------------------
# 写入（自动填表）
# --------------------------------------------------------------------------

def _capacity(sheet, total_marker: str = "合计") -> tuple[int, int | None]:
    """返回 (数据行数, 合计行号)。

    合计行按 A 列的「合计」识别（物资预算表最后一行）；识别不到时把表尾
    当作数据区末尾。数据行从第 2 行开始，不含表头与合计行。
    """
    total_row: int | None = None
    for row in range(2, sheet.max_row + 1):
        if _text(sheet.cell(row=row, column=1).value) == total_marker:
            total_row = row
            break
    capacity = total_row - 2 if total_row else max(sheet.max_row - 1, 0)
    return max(capacity, 0), total_row


def _grow(sheet, capacity: int, needed: int, columns: int,
          total_row: int | None) -> int:
    """数据行不够时插行，并把上一行的样式复制到新行。

    openpyxl 的 insert_rows 只挪单元格，不复制样式；不补样式的话新行会变成
    默认字体、无边框，用户一眼就能看出是程序拼的。
    """
    if needed <= capacity:
        return capacity

    extra = needed - capacity
    # 有合计行时插在合计行之前，保证合计仍在最后一行
    at = total_row if total_row else capacity + 2
    sheet.insert_rows(at, extra)

    source = at - 1
    for row in range(at, at + extra):
        for col in range(1, columns + 1):
            sheet.cell(row=row, column=col)._style = copy(
                sheet.cell(row=source, column=col)._style)
        height = sheet.row_dimensions[source].height
        if height is not None:
            sheet.row_dimensions[row].height = height
    return capacity + extra


def _write_plain(sheet, fields: tuple[str, ...], rows: list[dict]) -> int:
    """环节 / 困难表：按列顺序逐格写文本，多余的空行写空串。"""
    old_capacity, _ = _capacity(sheet)
    capacity = _grow(sheet, old_capacity, len(rows), len(fields), None)

    for offset in range(capacity):
        row = 2 + offset
        item = rows[offset] if offset < len(rows) else {}
        for col, field in enumerate(fields, start=1):
            sheet.cell(row=row, column=col, value=_text(item.get(field)))
    return len(rows)


def _write_materials(sheet, rows: list[dict]) -> int:
    """物资预算表：小计一律写公式，单价写数字，合计行随行数下移。"""
    old_capacity, total_row = _capacity(sheet)
    capacity = _grow(sheet, old_capacity, len(rows), len(MATERIAL_FIELDS), total_row)
    if total_row is not None:
        total_row += capacity - old_capacity          # 插行后合计行下移

    for offset in range(capacity):
        row = 2 + offset
        item = rows[offset] if offset < len(rows) else {}
        sheet.cell(row=row, column=1, value=_text(item.get("类别")))
        sheet.cell(row=row, column=2, value=_text(item.get("物资名称")))
        sheet.cell(row=row, column=3, value=_text(item.get("数量\\规格")))

        # 单价为空时留空单元格：空值与数字相乘按 0 算，写空串会变成 #VALUE!
        raw_price = item.get("单价")
        price = _money(raw_price) if _text(raw_price) else None
        price_cell = sheet.cell(row=row, column=4, value=price)
        price_cell.number_format = "0.00"

        subtotal_cell = sheet.cell(
            row=row, column=5, value=SUBTOTAL_FORMULA.format(row=row))
        subtotal_cell.number_format = "0.00"

    if total_row is not None:
        sheet.cell(row=total_row, column=1, value="合计")
        total_cell = sheet.cell(row=total_row, column=5,
                                value=f"=SUM(E2:E{capacity + 1})")
        total_cell.number_format = "0.00"
    return len(rows)


def fill_workbook(path: Path, book: dict) -> dict:
    """把 book 内容写进已生成的 xlsx（就地覆盖空行）。

    book 结构同 read_workbook 的返回：{"环节": [...], "困难": [...], "物资": [...]}。
    环节/困难/物资 各自写入对应 sheet 的第 2 行起；超过 sheet 现有数据行数时
    自动加行（复制上一行样式）。物资小计列写公式，不写死值。
    返回 {"环节": n, "困难": n, "物资": n}。
    """
    path = Path(path)
    workbook = openpyxl.load_workbook(str(path))
    try:
        missing = [name for name in (SHEET_STEP, SHEET_DIFFICULTY, SHEET_MATERIAL)
                   if name not in workbook.sheetnames]
        if missing:
            raise ValueError(f"填写表缺少工作表：{'、'.join(missing)}")

        counts = {
            "环节": _write_plain(workbook[SHEET_STEP], STEP_FIELDS,
                                 list(book.get("环节") or [])),
            "困难": _write_plain(workbook[SHEET_DIFFICULTY], DIFFICULTY_FIELDS,
                                 list(book.get("困难") or [])),
            "物资": _write_materials(workbook[SHEET_MATERIAL],
                                     list(book.get("物资") or [])),
        }
        workbook.save(str(path))
    finally:
        workbook.close()
    return counts


def _truncate(rows: list[dict], limit: int, label: str,
              warnings: list[str]) -> list[dict]:
    """超过 Word 表行数时截断，并把说明记进 warnings。"""
    if len(rows) <= limit:
        return rows
    warnings.append(f"{label}共 {len(rows)} 条，Word 表只有 {limit} 行，"
                    f"已截断后 {len(rows) - limit} 条")
    return rows[:limit]


def to_fill_data(book: dict, base: dict | None = None) -> dict:
    """把 read_workbook 的结果转成 fill.render_docx 的 data dict。

    base 是 activity.build_data() 的项目基础数据（项目名/编号/社工等），
    本函数只覆盖三张表对应的带序号 key。返回值里的 extra["warning"] 是
    截断、模板列缺失等提示，用「；」连接；没有提示时是空串。
    """
    data = dict(base or {})
    warnings: list[str] = []

    # ---------------- 环节安排 ----------------
    steps = _truncate(list(book.get("环节") or []), STEP_ROWS, "环节", warnings)
    # 先铺满 5 行空串，避免模板里残留 {{环节内容_4}} 这类原始占位符
    for seq in range(1, STEP_ROWS + 1):
        item = steps[seq - 1] if seq <= len(steps) else {}
        data[f"环节阶段_{seq}"] = item.get("阶段") or ""
        data[f"环节时间_{seq}"] = item.get("时间") or ""
        data[f"环节内容_{seq}"] = item.get("内容") or ""
        data[f"环节物资_{seq}"] = item.get("所需物资") or ""
        data[f"环节人员_{seq}"] = item.get("人员") or ""
        data[f"环节备注_{seq}"] = item.get("备注") or ""

    # Word 环节表的阶段列第 1-2 行、第 4-5 行是合并格，一个格只能写一个值
    for leader, follower in ((0, 1), (3, 4)):
        if len(steps) <= follower:
            continue
        first = steps[leader].get("阶段") or ""
        second = steps[follower].get("阶段") or ""
        if second and second != first:
            warnings.append(
                f"Word 环节表阶段列第 {leader + 1}-{follower + 1} 行是合并格，"
                f"第 {follower + 1} 行的阶段「{second}」未写入")
    if any((item.get("备注") or "").strip() for item in steps):
        warnings.append("Word 环节表备注列没有占位符，备注只保留在 Excel")

    # ---------------- 预见困难 ----------------
    difficulties = _truncate(list(book.get("困难") or []), DIFFICULTY_ROWS,
                             "预计困难", warnings)
    for seq in range(1, DIFFICULTY_ROWS + 1):
        item = difficulties[seq - 1] if seq <= len(difficulties) else {}
        data[f"预计困难_{seq}"] = item.get("预计困难") or ""
        data[f"解决方案_{seq}"] = item.get("解决方案") or ""

    # ---------------- 物资预算 ----------------
    materials = _truncate(list(book.get("物资") or []), MATERIAL_ROWS,
                          "物资", warnings)
    total = 0.0
    for seq in range(1, MATERIAL_ROWS + 1):
        item = materials[seq - 1] if seq <= len(materials) else {}
        subtotal = _subtotal(item) if item else ""
        data[f"物资名称_{seq}"] = item.get("物资名称") or ""
        data[f"物资规格_{seq}"] = item.get("数量\\规格") or ""
        data[f"物资单价_{seq}"] = _money(item.get("单价")) if item else ""
        data[f"物资小计_{seq}"] = subtotal
        if item:
            total += subtotal
    # 合计只算真正写进 Word 的行，避免合计与可见明细对不上
    data["合计金额"] = round(total, 2) if materials else ""
    if any((item.get("类别") or "").strip() for item in materials):
        warnings.append("Word 物资预算表的类别列没有占位符，类别只保留在 Excel")

    data["extra"] = {"warning": "；".join(warnings)}
    return data
