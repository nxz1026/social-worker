"""计划书 Excel 填写表：生成结构 + 读回 + 转填充数据。

不依赖 /srv 的真实模板：用 python-docx 现造一份只有三张表头的迷你模板，
让 make_plan_xlsx 从表头生成 xlsx，再在临时文件上验证读写。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from docx import Document
from openpyxl import load_workbook

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from engine import plan_sheet  # noqa: E402
import make_plan_xlsx  # noqa: E402

HEADERS = (
    ("阶段", "时间", "内容", "所需物资", "人员", "备注"),
    ("预计困难", "解决方案"),
    ("类别", "物资名称", "数量\\规格", "单价（元）", "小计（元）"),
)


@pytest.fixture
def empty_book(tmp_path: Path) -> Path:
    """造迷你 Word 模板 → 生成一份全空的填写表。"""
    doc = Document()
    for headers in HEADERS:
        table = doc.add_table(rows=2, cols=len(headers))
        for col, header in enumerate(headers):
            table.cell(0, col).text = header
    template = tmp_path / "计划书.docx"
    doc.save(str(template))

    out = tmp_path / "计划书-填写表.xlsx"
    make_plan_xlsx.build(template, out)
    return out


def _save_rows(path: Path, steps, difficulties, materials) -> None:
    """往填写表里写数据；单价/小计故意按原样写，便于验证重算。"""
    workbook = load_workbook(str(path))
    sheet = workbook["环节安排"]
    for row, values in enumerate(steps, start=2):
        for col, value in enumerate(values, start=1):
            sheet.cell(row=row, column=col, value=value)

    sheet = workbook["预见困难"]
    for row, values in enumerate(difficulties, start=2):
        for col, value in enumerate(values, start=1):
            sheet.cell(row=row, column=col, value=value)

    sheet = workbook["物资预算"]
    for row, values in enumerate(materials, start=2):
        for col, value in enumerate(values, start=1):
            sheet.cell(row=row, column=col, value=value)

    workbook.save(str(path))
    workbook.close()


# --------------------------------------------------------------------------
# 生成的结构
# --------------------------------------------------------------------------

def test_generated_workbook_structure(empty_book: Path):
    workbook = load_workbook(str(empty_book))
    assert workbook.sheetnames == ["环节安排", "预见困难", "物资预算"]

    step = workbook["环节安排"]
    assert (step.max_row, step.max_column) == (21, 6)      # 表头 + 20 行
    assert [cell.value for cell in step[1]] == list(HEADERS[0])
    assert step.freeze_panes == "A2"
    assert step["A1"].font.bold is True
    validations = step.data_validations.dataValidation
    assert len(validations) == 1
    assert str(validations[0].sqref) == "A2:A21"
    for stage in ("准备阶段", "执行阶段", "总结归档"):
        assert stage in validations[0].formula1

    difficulty = workbook["预见困难"]
    assert (difficulty.max_row, difficulty.max_column) == (11, 2)   # 表头 + 10 行
    assert [cell.value for cell in difficulty[1]] == list(HEADERS[1])

    material = workbook["物资预算"]
    assert (material.max_row, material.max_column) == (22, 5)       # 表头 + 20 行 + 合计
    assert [cell.value for cell in material[1]] == list(HEADERS[2])
    assert material["D2"].number_format == "0.00"
    assert material["E2"].number_format == "0.00"
    assert material["E2"].value.startswith("=D2*")
    assert material["E22"].value == "=SUM(E2:E21)"
    assert material["A22"].value == "合计"


# --------------------------------------------------------------------------
# 数量解析（与 Excel 小计公式同口径）
# --------------------------------------------------------------------------

def test_quantity_uses_leading_number():
    assert plan_sheet._quantity("2张") == 2
    assert plan_sheet._quantity("5米") == 5
    assert plan_sheet._quantity("1.5公斤") == 1.5
    assert plan_sheet._quantity("两箱") == 1       # 取不出数字按 1 算
    assert plan_sheet._quantity(3) == 3


# --------------------------------------------------------------------------
# read_workbook
# --------------------------------------------------------------------------

def test_read_empty_workbook_returns_empty_lists(empty_book: Path):
    book = plan_sheet.read_workbook(empty_book)
    assert book == {"环节": [], "困难": [], "物资": [], "物资合计": 0}


def test_read_workbook_round_trip(empty_book: Path):
    _save_rows(
        empty_book,
        steps=[
            ("准备阶段", "9:00-9:30", "签到、宣读公约", "签到表、公约", "社工", ""),
            ("执行阶段", "9:30-11:00", "分组清理通道", "手套、垃圾袋", "志愿者", ""),
        ],
        difficulties=[("车主不配合", "提前张贴告知")],
        materials=[
            ("宣传", "横幅", "2条", 50, 999),       # 小计故意写错
            ("饮用水", "矿泉水", "3箱", 30, 0),
        ],
    )

    book = plan_sheet.read_workbook(empty_book)
    assert len(book["环节"]) == 2
    assert book["环节"][0]["阶段"] == "准备阶段"
    assert book["环节"][1]["所需物资"] == "手套、垃圾袋"
    assert book["困难"] == [{"预计困难": "车主不配合", "解决方案": "提前张贴告知"}]
    assert len(book["物资"]) == 2
    assert book["物资"][0]["物资名称"] == "横幅"
    assert book["物资"][0]["数量\\规格"] == "2条"
    assert book["物资"][0]["单价"] == 50
    # 小计不读 Excel 里写错的 999，按 2×50 重算
    assert book["物资"][0]["小计"] == 100
    assert book["物资"][1]["小计"] == 90
    assert book["物资合计"] == 190


def test_read_workbook_skips_rows_without_key_column(empty_book: Path):
    """只写了时间没写内容的环节、没写名称的物资都不收。"""
    _save_rows(
        empty_book,
        steps=[("", "9:00", "", "", "", "")],
        difficulties=[("", "只有方案")],
        materials=[("宣传", "", "2条", 50, 100)],
    )
    book = plan_sheet.read_workbook(empty_book)
    assert book["环节"] == []
    assert book["困难"] == []
    assert book["物资"] == []


# --------------------------------------------------------------------------
# to_fill_data
# --------------------------------------------------------------------------

def test_to_fill_data_recomputes_subtotal(empty_book: Path):
    _save_rows(
        empty_book,
        steps=[("准备阶段", "9:00", "签到", "签到表", "社工", "")],
        difficulties=[("下雨", "改到室内")],
        materials=[
            ("宣传", "横幅", "2条", 50, 999),       # Excel 显示 999
            ("饮用水", "矿泉水", "3箱", 30, 0),
        ],
    )
    data = plan_sheet.to_fill_data(plan_sheet.read_workbook(empty_book))

    assert data["环节阶段_1"] == "准备阶段"
    assert data["环节内容_1"] == "签到"
    assert data["预计困难_1"] == "下雨"
    assert data["解决方案_1"] == "改到室内"
    assert data["物资小计_1"] == 100
    assert data["物资小计_2"] == 90
    assert data["合计金额"] == 190
    # 模板只有 5 行：没写到的行补空串，避免留下原始占位符
    assert data["环节内容_2"] == ""
    assert data["预计困难_4"] == ""
    assert data["物资名称_5"] == ""


def test_to_fill_data_keeps_base_fields(empty_book: Path):
    """base 里的项目名/编号等不能被 Excel 数据覆盖。"""
    _save_rows(empty_book, steps=[], difficulties=[], materials=[])
    data = plan_sheet.to_fill_data(
        plan_sheet.read_workbook(empty_book),
        base={"项目名称": "惠民项目", "项目编号": "HM-001"},
    )
    assert data["项目名称"] == "惠民项目"
    assert data["项目编号"] == "HM-001"


def test_to_fill_data_truncates_twenty_steps(empty_book: Path):
    steps = [("", f"9:{index:02d}", f"环节{index}", "", "", "")
             for index in range(1, 21)]
    _save_rows(empty_book, steps=steps, difficulties=[], materials=[])

    book = plan_sheet.read_workbook(empty_book)
    assert len(book["环节"]) == 20

    data = plan_sheet.to_fill_data(book)
    assert data["环节内容_5"] == "环节5"
    assert "环节内容_6" not in data
    assert "20" in data["extra"]["warning"]
    assert "5 行" in data["extra"]["warning"]


def test_to_fill_data_truncates_extra_materials(empty_book: Path):
    materials = [("宣传", f"物资{i}", "2个", 10, 20) for i in range(1, 8)]
    _save_rows(empty_book, steps=[], difficulties=[], materials=materials)

    data = plan_sheet.to_fill_data(plan_sheet.read_workbook(empty_book))
    assert data["物资名称_5"] == "物资5"
    assert "物资名称_6" not in data
    # 合计只算真正写进 Word 的前 5 条
    assert data["合计金额"] == 100
    assert "物资共 7 条" in data["extra"]["warning"]
