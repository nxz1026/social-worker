#!/usr/bin/env python3
"""engine.checks.find_first_file：兼容三种真实文件命名。

真实活动目录里同时存在：
  01-计划书-共治行动.docx          （序号前缀 + 关键词 + 活动名）
  01-20261105xx活动计划书.docx     （序号前缀 + 日期活动名 + 关键词）
  计划书.docx                      （Web 按模板原名生成，无前缀）
原来各脚本只 glob「01-*.docx」，第三种读不到。
"""
from __future__ import annotations

from pathlib import Path

from engine import checks


def _touch(d: Path, *names: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"x")
    return d


def test_prefixed_with_keyword(tmp_path):
    """规范命名：01-计划书-xx.docx。"""
    d = _touch(tmp_path / "act", "01-计划书-共治行动.docx", "06-总结报告.docx")
    assert checks.find_first_file(d, "活动计划").name == "01-计划书-共治行动.docx"
    assert checks.find_first_file(d, "总结报告").name == "06-总结报告.docx"


def test_prefixed_with_date_and_keyword(tmp_path):
    """前缀 + 日期活动名 + 关键词：01-20261105xx活动计划书.docx。"""
    d = _touch(tmp_path / "act", "01-20261105爱邻议事会第一期活动计划书.docx")
    assert checks.find_first_file(d, "活动计划").name.startswith("01-20261105")


def test_bare_name_without_prefix(tmp_path):
    """无前缀（Web 生成）：计划书.docx / 总结报告.docx。"""
    d = _touch(tmp_path / "act", "计划书.docx", "总结报告.docx", "决算表.docx")
    assert checks.find_first_file(d, "活动计划").name == "计划书.docx"
    assert checks.find_first_file(d, "总结报告").name == "总结报告.docx"
    assert checks.find_first_file(d, "决算表").name == "决算表.docx"


def test_prefixed_wins_over_bare(tmp_path):
    """两种都在时优先带前缀的（规范命名）。"""
    d = _touch(tmp_path / "act",
               "计划书.docx", "01-计划书-正式.docx")
    assert checks.find_first_file(d, "活动计划").name == "01-计划书-正式.docx"


def test_missing_returns_none(tmp_path):
    """都找不到 → None，不抛错。"""
    d = _touch(tmp_path / "act", "无关文件.docx")
    assert checks.find_first_file(d, "活动计划") is None


def test_missing_dir_returns_none(tmp_path):
    """目录不存在 → None。"""
    assert checks.find_first_file(tmp_path / "nope", "活动计划") is None


def test_unknown_item_returns_none(tmp_path):
    """清单里没有的项名 → None。"""
    d = _touch(tmp_path / "act", "计划书.docx")
    assert checks.find_first_file(d, "不存在的项") is None


def test_multi_prefix_item(tmp_path):
    """报销单有两个前缀（09- / 21-），两个都要认。"""
    d = _touch(tmp_path / "act", "21-报销单.xlsx")
    hit = checks.find_first_file(d, "报销单", exts=(".xlsx",))
    assert hit is not None and hit.name == "21-报销单.xlsx"
