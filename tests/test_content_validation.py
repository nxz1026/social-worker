#!/usr/bin/env python3
"""文件存在 != 内容填了：决算表与满意度测评汇总表的内容校验。

2026-11-24 用户反馈：01-队伍招募与组建-第一期 的决算表是测试时自动生成的
空模板副本（残留 32 个 {{物品名称_N}} 占位符），满意度测评汇总表全空，
两者都被判成 ok。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from engine import checks


docx = pytest.importorskip("docx")


def _make_docx(path: Path, rows: list[list[str]]) -> Path:
    d = docx.Document()
    t = d.add_table(rows=len(rows), cols=len(rows[0]))
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            t.rows[ri].cells[ci].text = val
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    return path


def test_juesuan_with_placeholders_is_partial(tmp_path):
    """残留占位符 = 空模板副本，不能算齐备。"""
    _make_docx(tmp_path / "08-决算表.docx", [
        ["序号", "物品名称", "单价"],
        ["1", "{{物品名称_1}}", "{{单价_1}}"],
        ["2", "{{物品名称_2}}", "{{单价_2}}"],
    ])
    item = next(i for i in checks.check(tmp_path)["items"] if i["no"] == 7)
    assert item["status"] == "partial"
    assert "空模板" in item["detail"] or "占位符" in item["detail"]


def test_juesuan_filled_is_ok(tmp_path):
    _make_docx(tmp_path / "08-决算表.docx", [
        ["序号", "物品名称", "单价", "数量"],
        ["1", "横幅", "60", "2"],
        ["2", "KT板", "80", "4"],
    ])
    item = next(i for i in checks.check(tmp_path)["items"] if i["no"] == 7)
    assert item["status"] == "ok"
    assert "数据" in item["detail"]


def test_satisfaction_empty_is_partial(tmp_path):
    """满意度测评要等调查表 PDF 上传后再统计，空表是中间态不算齐备。"""
    _make_docx(tmp_path / "05-满意度测评汇总表.docx", [
        ["参与人数", "接受测评人数", "总得分", "满意率"],
        ["", "", "", ""],
    ])
    item = next(i for i in checks.check(tmp_path)["items"] if i["no"] == 4)
    assert item["status"] == "partial"
    assert "数值" in item["detail"] or "统计" in item["detail"]


def test_satisfaction_with_numbers_is_ok(tmp_path):
    _make_docx(tmp_path / "05-满意度测评汇总表.docx", [
        ["参与人数", "接受测评人数", "总得分", "满意率"],
        ["18", "18", "90", "100%"],
    ])
    item = next(i for i in checks.check(tmp_path)["items"] if i["no"] == 4)
    assert item["status"] == "ok"


def test_content_stats_handles_unreadable(tmp_path):
    """坏文件不该抛错，返回 (-1,-1) 由调用方按「无法解析」处理。"""
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    assert checks._content_stats(bad) == (-1, -1)


def test_content_stats_missing_file(tmp_path):
    assert checks._content_stats(tmp_path / "nope.docx") == (-1, -1)
