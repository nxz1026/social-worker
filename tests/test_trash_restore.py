#!/usr/bin/env python3
"""活动回收站：删除 → 列表 → 恢复（审计 PR-06）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

from api.shared.paths import safe_rel_path  # noqa: E402


def test_safe_rel_path_keeps_dot_dirs(tmp_path):
    """.trash 这类点开头的目录不能被 lstrip('./') 剥成 trash。"""
    assert safe_rel_path(tmp_path, ".trash/20260101-000000/a.txt").name == "a.txt"
    assert ".trash" in safe_rel_path(tmp_path, ".trash/x/a.txt").parts
    # 正常路径不受影响
    assert safe_rel_path(tmp_path, "./a/b.txt").name == "b.txt"
    # 越界仍然拒绝
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        safe_rel_path(tmp_path, "a/../b.txt")


def test_trash_layout_roundtrip(tmp_path):
    """按 assets 端点的规则摆一个 .trash 结构，验证列表与恢复的路径推导。"""
    act = tmp_path / "活动"
    stamp = "20260101-120000"
    src = act / ".trash" / stamp / "01-计划书.docx"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"doc")

    base = (act / ".trash").resolve()
    files = [p for p in base.rglob("*") if p.is_file()]
    assert len(files) == 1
    rel = files[0].relative_to(base)
    assert rel.parts[0] == stamp
    assert "/".join(rel.parts[1:]) == "01-计划书.docx"

    # 列表返回的 trash 带 .trash/ 前缀，恢复端点直接拿它回传
    trash_value = (Path(".trash") / rel).as_posix()
    assert trash_value == f".trash/{stamp}/01-计划书.docx"
    resolved = safe_rel_path(act, trash_value)
    assert resolved == files[0].resolve()
