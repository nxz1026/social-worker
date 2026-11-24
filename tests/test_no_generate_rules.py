#!/usr/bin/env python3
"""决算表/报销单不生成、支出证明不入库（用户 2026-11-24 要求）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", "/srv/social-work")
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")


def test_juesuan_and_baoxiao_not_generatable():
    """决算表与报销单必须人工按实际票据填，不进生成列表。"""
    from api.routes.misc import _NO_GENERATE
    assert "决算表" in _NO_GENERATE
    assert "报销单" in _NO_GENERATE
    # 支出证明单仍然可生成（只存档）
    assert "支出证明单" not in _NO_GENERATE


def test_templates_endpoint_marks_fillable():
    """端点返回 fillable 标记，前端据此过滤。"""
    from api.routes.misc import api_templates

    class FakeUser:
        id = 1
        role = "admin"

    class Req:
        client = type("C", (), {"host": "127.0.0.1"})

    items = api_templates(FakeUser())
    by_name = {t["name"]: t for t in items}
    if "决算表" in by_name:
        assert by_name["决算表"]["fillable"] is False
        assert by_name["决算表"]["reason"]
    if "报销单" in by_name:
        assert by_name["报销单"]["fillable"] is False
    # 其余模板默认可生成
    for name, t in by_name.items():
        if name not in ("决算表", "报销单"):
            assert t["fillable"] is True, f"{name} 应可生成"


def test_expense_certificate_excluded_from_index():
    """支出证明只存档，不入知识库。"""
    sys.path.insert(0, str(REPO / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "index_cognee_mod", REPO / "scripts" / "index_cognee.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["index_cognee_mod"] = mod
    spec.loader.exec_module(mod)

    assert "支出证明" in mod.INDEX_EXCLUDE_KEYWORDS
    base = Path("/tmp/x")
    assert mod._excluded(Path("/tmp/x/21-支出证明单.xlsx")) is True
    assert mod._excluded(Path("/tmp/x/21-报销单.xlsx")) is False
    # 整条可见性判断要串起来
    assert mod._visible(Path("/tmp/x/其他/21-支出证明单.xlsx"), base) is False
    assert mod._visible(Path("/tmp/x/其他/通讯录.docx"), base) is True
    # 隐藏目录仍然排除
    assert mod._visible(Path("/tmp/x/.trash/a.docx"), base) is False
