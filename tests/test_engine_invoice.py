"""engine/invoice.py：文件名解析、扫描、三表金额校验。

测试自己造活动目录（docx / xlsx / 空 pdf），不依赖 /srv 上的真实资料。
"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
import pytest
from docx import Document

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import invoice  # noqa: E402


# --------------------------------------------------------------------------
# 造活动目录
# --------------------------------------------------------------------------

def _make_decision(act_dir: Path, total: str = "1662") -> None:
    """08-决算表.docx：表尾一行「合计 … 1662（元）」。"""
    doc = Document()
    doc.add_paragraph("决算表")
    table = doc.add_table(rows=2, cols=3)
    table.cell(0, 0).text = "物品名称"
    table.cell(0, 2).text = "金额（元）"
    table.cell(1, 0).text = "合计"
    table.cell(1, 2).text = f"{total}（元）"
    doc.save(str(act_dir / "08-决算表.docx"))


def _make_reimburse(act_dir: Path, total: float = 1662) -> None:
    """21-报销单.xlsx：「合计金额」行右侧是数值。"""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["A1"] = "费用报销单"
    sheet["A2"] = "合计金额"
    sheet["C2"] = total
    workbook.save(str(act_dir / "21-报销单.xlsx"))


def _make_invoices(act_dir: Path, names: list[str]) -> None:
    """12-发票/ 下放空 PDF（正文读不出来，只验证文件名解析）。"""
    folder = act_dir / "12-发票"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"%PDF-1.4\n")


@pytest.fixture
def act_dir(tmp_path: Path) -> Path:
    """一套三表一致的完整活动目录。"""
    act_dir = tmp_path / "11-共治行动-第一期"
    act_dir.mkdir()
    _make_decision(act_dir)
    _make_reimburse(act_dir)
    _make_invoices(act_dir, [
        "2026-11-13_电子发票_1190元_温江三圆广告制作部_26512000005078121196.pdf",
        "2026-11-13_电子发票_172元_美诗韵商贸部_劳保手套签字笔矿泉水_26512000005086188106.pdf",
        "2026-11-13_增值税发票_300元_刘佳_讲课费_26512000005080500076.pdf",
    ])
    return act_dir


# --------------------------------------------------------------------------
# 文件名解析
# --------------------------------------------------------------------------

def test_parse_standard_name_with_item(tmp_path: Path):
    """工单给的样例：文件名带事项段。"""
    path = tmp_path / ("2026-11-13_电子发票_160元_听澜集益_茶饮服务_"
                       "26512000005052287086.pdf")
    path.write_bytes(b"%PDF-1.4\n")

    result = invoice.parse_invoice_pdf(path)

    assert result["amount"] == 160.0
    assert result["merchant"] == "听澜集益"
    assert result["date"] == "2026-11-13"
    assert result["item"] == "茶饮服务"
    assert result["invoice_no"] == "26512000005052287086"
    assert result["source"] == "filename"
    assert result["parse_error"] == ""


def test_parse_name_without_item(tmp_path: Path):
    """文件名省略事项段时 item 为空，其余字段照常。"""
    path = tmp_path / ("2026-11-13_电子发票_1190元_温江三圆广告制作部_"
                       "26512000005078121196.pdf")
    path.write_bytes(b"%PDF-1.4\n")

    result = invoice.parse_invoice_pdf(path)

    assert result["amount"] == 1190.0
    assert result["merchant"] == "温江三圆广告制作部"
    assert result["item"] == ""


def test_parse_unrecognized_name_returns_none(tmp_path: Path):
    """文件名不规范且读不出正文：amount 为 None，带 parse_error。"""
    path = tmp_path / "微信图片_20261110131250_175_11.pdf"
    path.write_bytes(b"%PDF-1.4\n")

    result = invoice.parse_invoice_pdf(path)

    assert result["amount"] is None
    assert "parse_error" in result and result["parse_error"]


def test_scan_invoices_only_reads_invoice_folder(act_dir: Path):
    """只收 12-发票/ 下的 pdf 与图片，docx 清单不算发票。"""
    (act_dir / "12-发票" / "12-发票粘贴单及清单.docx").write_bytes(b"")
    (act_dir / "12-发票" / "2026-11-13_电子发票_50元_某商户_26512000005000000001.jpg").write_bytes(b"")

    items = invoice.scan_invoices(act_dir)

    assert len(items) == 4
    assert all(item["file"].endswith((".pdf", ".jpg")) for item in items)
    assert {item["amount"] for item in items} == {1190.0, 172.0, 300.0, 50.0}


def test_scan_invoices_missing_folder(tmp_path: Path):
    assert invoice.scan_invoices(tmp_path) == []


# --------------------------------------------------------------------------
# 三表校验
# --------------------------------------------------------------------------

def test_cross_check_consistent(act_dir: Path):
    result = invoice.cross_check(act_dir)

    assert result["决算"] == {"value": 1662.0, "source": "08-决算表.docx"}
    assert result["报销"] == {"value": 1662.0, "source": "21-报销单.xlsx"}
    assert result["发票"]["value"] == 1662.0
    assert result["发票"]["count"] == 3
    assert result["diff"] == {"决算-报销": 0.0, "报销-发票": 0.0}
    assert result["consistent"] is True
    assert result["issues"] == []


def test_cross_check_decision_differs(act_dir: Path):
    """决算 1600、报销 1662、发票 1662：只有决算-报销 不一致。"""
    _make_decision(act_dir, total="1600")

    result = invoice.cross_check(act_dir)

    assert result["决算"]["value"] == 1600.0
    assert result["diff"]["决算-报销"] == -62.0
    assert result["diff"]["报销-发票"] == 0.0
    assert result["consistent"] is False
    assert any("决算-报销" in issue for issue in result["issues"])


def test_cross_check_invoice_total_differs(act_dir: Path):
    """少一张发票：发票合计变小，报销-发票 不一致。"""
    (act_dir / "12-发票" / "2026-11-13_增值税发票_300元_刘佳_讲课费_26512000005080500076.pdf").unlink()

    result = invoice.cross_check(act_dir)

    assert result["发票"]["value"] == 1362.0
    assert result["diff"]["报销-发票"] == 300.0
    assert result["consistent"] is False


def test_cross_check_missing_reimburse(act_dir: Path):
    """报销单缺失：值 None、判不一致、问题里点名。"""
    (act_dir / "21-报销单.xlsx").unlink()

    result = invoice.cross_check(act_dir)

    assert result["报销"]["value"] is None
    assert result["diff"]["决算-报销"] is None
    assert result["consistent"] is False
    assert any("报销合计未取到" in issue for issue in result["issues"])


def test_cross_check_reimburse_formula_without_cache(tmp_path: Path):
    """合计是公式且没缓存值时返回 None，不去猜公式结果。"""
    act_dir = tmp_path / "活动"
    act_dir.mkdir()
    _make_decision(act_dir)
    _make_invoices(act_dir, [])

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["A2"] = "合计金额"
    sheet["C2"] = "=SUM(C3:C4)"
    sheet["C3"] = 100
    sheet["C4"] = 62
    workbook.save(str(act_dir / "21-报销单.xlsx"))

    result = invoice.cross_check(act_dir)

    assert result["报销"]["value"] is None


# --------------------------------------------------------------------------
# 硬约束：排除清单里的模板不参与校验与生成
# --------------------------------------------------------------------------

def _excluded_templates() -> list[str]:
    """读 config/excluded_templates.txt：去掉注释与空行。"""
    path = REPO / "config" / "excluded_templates.txt"
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            names.append(name)
    return names


def test_excluded_templates_never_referenced():
    """排除清单非空，且清单里的模板名不出现在发票校验链路中。"""
    excluded = _excluded_templates()
    assert excluded

    sources = [
        REPO / "engine" / "invoice.py",
        REPO / "scripts" / "invoice_report.py",
        REPO / "scripts" / "mark_placeholders.py",
    ]
    for name in excluded:
        stem = Path(name).stem
        for path in sources:
            assert stem not in path.read_text(encoding="utf-8"), (name, path)
