#!/usr/bin/env python3
"""管理界面的文件名规则护栏：项目推断只看文件名。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault("SOCIALWORK_DB_DSN",
                      "host=127.0.0.1 port=5432 dbname=socialwork user=admin password=x")

import app  # noqa: E402


def test_suggest_project_uses_filename_rules(tmp_path):
    f = tmp_path / "2026-11-11_惠民_活动计划书.docx"
    f.write_bytes(b"")
    project, why = app._suggest_project(f)
    assert project == "huimin"
    assert why == "文件名判定"

