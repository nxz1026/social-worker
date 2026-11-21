"""engine.markdown：docx / xlsx 转 Markdown 冒烟测试。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine.markdown import docx_to_md, file_to_md, xlsx_to_md  # noqa: E402

# 项目资料默认在生产目录；测试机没有时用 SOCIAL_WORK_PROJECTS 指到别处。
PROJECTS = Path(os.environ.get("SOCIAL_WORK_PROJECTS",
                               "/srv/social-work/projects"))
ACTIVITY = PROJECTS / "惠民社区保障资金项目" / "活动" / "11-共治行动-第一期"
DOCX = ACTIVITY / "01-计划书-共治行动.docx"
XLSX = ACTIVITY / "21-报销单.xlsx"


@pytest.mark.skipif(not DOCX.is_file(), reason="样例 docx 不在本机")
def test_docx_to_md_keeps_text_and_table():
    md = docx_to_md(DOCX)
    assert "共治行动" in md
    assert "|" in md, "表格应转成 Markdown 表格"


@pytest.mark.skipif(not XLSX.is_file(), reason="样例 xlsx 不在本机")
def test_xlsx_to_md_keeps_total_row():
    md = xlsx_to_md(XLSX)
    assert "合计金额" in md


def test_docx_missing_file_raises(tmp_path):
    with pytest.raises(RuntimeError):
        docx_to_md(tmp_path / "不存在.docx")


def test_xlsx_missing_file_raises(tmp_path):
    with pytest.raises(RuntimeError):
        xlsx_to_md(tmp_path / "不存在.xlsx")


def test_file_to_md_unsupported_returns_empty(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")
    assert file_to_md(path) == ""
