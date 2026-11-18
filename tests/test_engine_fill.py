"""engine P3：模板填充（docx / xlsx）。"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl
from docx import Document
from docx.table import _Cell

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import fill  # noqa: E402

TEMPLATES = Path("/srv/social-work/templates/library")
JUE_SUAN = TEMPLATES / "决算表.docx"
BAO_XIAO = TEMPLATES / "报销单.xlsx"


def _docx_text(path: Path) -> str:
    """抽取 docx 正文段落 + 表格 + 页眉页脚的全部文本，用于断言。"""
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for tc in row._tr.tc_lst:
                parts += [p.text for p in _Cell(tc, table).paragraphs]
    for section in doc.sections:
        for hf in (section.header, section.first_page_header, section.even_page_header,
                   section.footer, section.first_page_footer, section.even_page_footer):
            if hf.is_linked_to_previous:
                continue
            parts += [p.text for p in hf.paragraphs]
    return "\n".join(parts)


def test_scan_docx_placeholders():
    placeholders = fill.scan_placeholders(JUE_SUAN)
    assert "{{项目名称}}" in placeholders
    assert "{{活动名称}}" in placeholders
    assert placeholders == sorted(set(placeholders))


def test_scan_xlsx_placeholders():
    placeholders = fill.scan_placeholders(BAO_XIAO)
    assert "{{费用归属部门}}" in placeholders
    assert "{{人民币大写}}" in placeholders


def test_render_docx_fills_values(tmp_path):
    dst = tmp_path / "决算表-已填.docx"
    data = {"项目名称": "测试项目", "活动名称": "测试活动",
            "活动时间": "2026年11月10日"}
    result = fill.render_docx(JUE_SUAN, dst, data)

    text = _docx_text(dst)
    for value in data.values():
        assert value in text
    assert "{{项目名称}}" not in text
    assert result["filled"] >= 3
    # 模板没有被原地改：源文件里仍是占位符
    assert "{{项目名称}}" in _docx_text(JUE_SUAN)


def test_render_docx_missing_keeps_placeholder(tmp_path):
    dst = tmp_path / "决算表-缺字段.docx"
    result = fill.render_docx(JUE_SUAN, dst, {})

    assert result["filled"] == 0
    assert result["missing"]
    assert "项目名称" in result["missing"]
    # 缺字段不抛错，占位符原样留在输出里
    assert "{{项目名称}}" in _docx_text(dst)


def test_render_docx_none_and_empty_clears(tmp_path):
    dst = tmp_path / "决算表-清空.docx"
    result = fill.render_docx(JUE_SUAN, dst,
                              {"项目名称": None, "活动名称": ""})
    text = _docx_text(dst)
    assert "{{项目名称}}" not in text
    assert "{{活动名称}}" not in text
    assert result["filled"] >= 2


def test_render_is_idempotent(tmp_path):
    first = tmp_path / "第一次.docx"
    second = tmp_path / "第二次.docx"
    data = {"项目名称": "测试项目", "活动名称": "测试活动",
            "活动时间": "2026年11月10日"}

    fill.render_docx(JUE_SUAN, first, data)
    result = fill.render_docx(first, second, data)

    assert result["filled"] == 0          # 已经没有可填的占位符
    assert _docx_text(second) == _docx_text(first)
    assert "测试项目" in _docx_text(second)


def test_render_xlsx_keeps_style_and_formula(tmp_path):
    dst = tmp_path / "报销单-已填.xlsx"
    data = {"费用归属部门": "温江柳城", "填表日期": "2026年11月18日",
            "人民币大写": "壹仟陆佰陆拾贰元整"}
    result = fill.render_xlsx(BAO_XIAO, dst, data)

    wb = openpyxl.load_workbook(str(dst))
    sheet = wb.worksheets[0]
    assert "温江柳城" in sheet["A3"].value
    assert "2026年11月18日" in sheet["B3"].value
    assert sheet["C13"].value == "壹仟陆佰陆拾贰元整"
    assert sheet["C12"].value == "=SUM(C6:J30)"     # 公式保留
    assert result["filled"] >= 3
    wb.close()


def test_render_dispatches_by_suffix(tmp_path):
    dst = tmp_path / "out.docx"
    result = fill.render(JUE_SUAN, dst, {"项目名称": "测试项目"})
    assert dst.is_file()
    assert "测试项目" in _docx_text(dst)
    assert result["filled"] >= 1


def test_render_rejects_unknown_suffix(tmp_path):
    bad = tmp_path / "模板.txt"
    bad.write_text("{{项目名称}}", encoding="utf-8")
    try:
        fill.render(bad, tmp_path / "out.txt", {})
    except ValueError:
        pass
    else:
        raise AssertionError("未知后缀应当抛 ValueError")
