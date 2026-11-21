"""engine P3：模板填充（docx / xlsx）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import openpyxl
import pytest
from docx import Document
from docx.table import _Cell

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import activity, fill  # noqa: E402

TEMPLATES = Path(os.environ.get("SOCIAL_WORK_TEMPLATES",
                                "/srv/social-work/templates/library"))
JUE_SUAN = TEMPLATES / "决算表.docx"
BAO_XIAO = TEMPLATES / "报销单.xlsx"

# 只给依赖真实模板的用例加 skip；用 tmp_path 自造文件的用例仍然要跑。
requires_jue_suan = pytest.mark.skipif(
    not JUE_SUAN.is_file(), reason=f"缺少真实模板 {JUE_SUAN}")
requires_bao_xiao = pytest.mark.skipif(
    not BAO_XIAO.is_file(), reason=f"缺少真实模板 {BAO_XIAO}")


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


@requires_jue_suan
def test_scan_docx_placeholders():
    placeholders = fill.scan_placeholders(JUE_SUAN)
    assert "{{项目名称}}" in placeholders
    assert "{{活动名称}}" in placeholders
    assert placeholders == sorted(set(placeholders))


@requires_bao_xiao
def test_scan_xlsx_placeholders():
    placeholders = fill.scan_placeholders(BAO_XIAO)
    assert "{{费用归属部门}}" in placeholders
    assert "{{人民币大写}}" in placeholders


@requires_jue_suan
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


@requires_jue_suan
def test_render_docx_missing_keeps_placeholder(tmp_path):
    dst = tmp_path / "决算表-缺字段.docx"
    result = fill.render_docx(JUE_SUAN, dst, {})

    assert result["filled"] == 0
    assert result["missing"]
    assert "项目名称" in result["missing"]
    # 缺字段不抛错，占位符原样留在输出里
    assert "{{项目名称}}" in _docx_text(dst)


@requires_jue_suan
def test_render_docx_none_and_empty_clears(tmp_path):
    dst = tmp_path / "决算表-清空.docx"
    result = fill.render_docx(JUE_SUAN, dst,
                              {"项目名称": None, "活动名称": ""})
    text = _docx_text(dst)
    assert "{{项目名称}}" not in text
    assert "{{活动名称}}" not in text
    assert result["filled"] >= 2


@requires_jue_suan
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


@requires_bao_xiao
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


@requires_jue_suan
def test_render_dispatches_by_suffix(tmp_path):
    dst = tmp_path / "out.docx"
    result = fill.render(JUE_SUAN, dst, {"项目名称": "测试项目"})
    assert dst.is_file()
    assert "测试项目" in _docx_text(dst)
    assert result["filled"] >= 1


def _table_rows_text(path: Path, table_idx: int) -> list[list[str]]:
    """按物理单元格返回 docx 表格每行的文本，便于按行断言。"""
    doc = Document(str(path))
    table = doc.tables[table_idx]
    return [[_Cell(tc, table).text for tc in row._tr.tc_lst]
            for row in table.rows]


@requires_jue_suan
def test_render_docx_row_index_list_values(tmp_path):
    """带序号的占位符按列表逐行取值：第一行横幅/10，第二行 KT板/120。"""
    dst = tmp_path / "决算表-多行.docx"
    data = {
        "物品名称": ["横幅", "KT板"],
        "单价": [10, 120],
        "规格": ["3m×0.8m", "2m×1m"],
        "金额": [10, 120],
        "备注": ["含安装", ""],
    }
    result = fill.render_docx(JUE_SUAN, dst, data)

    rows = _table_rows_text(dst, 0)
    assert rows[1][1] == "横幅"
    assert rows[1][2] == "10"
    assert rows[2][1] == "KT板"
    assert rows[2][2] == "120"
    assert rows[1][4] == "10"
    assert result["filled"] > 0


@requires_jue_suan
def test_render_docx_row_index_out_of_range_blank(tmp_path):
    """列表比表格行数短时，超出部分填空，不抛错。"""
    dst = tmp_path / "决算表-短列表.docx"
    result = fill.render_docx(JUE_SUAN, dst, {"物品名称": ["横幅"]})

    rows = _table_rows_text(dst, 0)
    assert rows[1][1] == "横幅"
    assert rows[2][1] == ""
    assert rows[6][1] == ""
    # 超出长度算已填充，不进 missing
    assert "物品名称_2" not in result["missing"]


@requires_jue_suan
def test_render_docx_indexed_key_wins_over_base(tmp_path):
    """精确的 字段_1 优先于无序号 key。"""
    dst = tmp_path / "决算表-精确key.docx"
    fill.render_docx(JUE_SUAN, dst, {"物品名称_1": "定制横幅", "物品名称": "通用"})

    rows = _table_rows_text(dst, 0)
    assert rows[1][1] == "定制横幅"
    assert rows[2][1] == "通用"


@requires_jue_suan
def test_render_docx_without_index_key_still_fills(tmp_path):
    """无序号 key 兼容旧用法：所有数据行填同一个值。"""
    dst = tmp_path / "决算表-兼容.docx"
    result = fill.render_docx(JUE_SUAN, dst, {"物品名称": "横幅"})

    rows = _table_rows_text(dst, 0)
    assert [rows[ri][1] for ri in range(1, 7)] == ["横幅"] * 6
    assert "物品名称_1" not in result["missing"]


@requires_bao_xiao
def test_render_xlsx_row_index_list_values(tmp_path):
    """xlsx 同样按序号取列表值，超出长度填空。"""
    dst = tmp_path / "报销单-多行.xlsx"
    fill.render_xlsx(BAO_XIAO, dst,
                     {"内容摘要": ["横幅", "KT板"], "报销金额": [10, 120]})

    wb = openpyxl.load_workbook(str(dst))
    sheet = wb.worksheets[0]
    assert sheet["B6"].value == "横幅"
    assert sheet["C6"].value == "10"
    assert sheet["B7"].value == "KT板"
    assert sheet["C7"].value == "120"
    assert sheet["B8"].value in (None, "")   # 超出长度填空（openpyxl 存空串读回是 None）
    wb.close()


def test_build_data_expands_list_extra(tmp_path):
    """extra 里的列表值展开成 字段_1、字段_2……，并保留原列表。"""
    data = activity.build_data(
        "不存在的项目", str(tmp_path),
        extra={"内容摘要": ["横幅", "KT板"]},
    )

    assert data["内容摘要"] == ["横幅", "KT板"]
    assert data["内容摘要_1"] == "横幅"
    assert data["内容摘要_2"] == "KT板"
    assert "内容摘要_3" not in data


def test_render_rejects_unknown_suffix(tmp_path):
    bad = tmp_path / "模板.txt"
    bad.write_text("{{项目名称}}", encoding="utf-8")
    try:
        fill.render(bad, tmp_path / "out.txt", {})
    except ValueError:
        pass
    else:
        raise AssertionError("未知后缀应当抛 ValueError")
