#!/usr/bin/env python3
"""删除权限与 API Key scope 的回归测试。

2026-11-23：api_keys.scopes 列此前从未被读取，标了 readonly 的 key
实际拥有 owner 全部权限。这里把该行为钉死。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

from api import apikey  # noqa: E402
from api.deps import require_delete, require_write  # noqa: E402


def _key(scopes):
    return {"id": 1, "username": "u", "role": "admin", "projects": {},
            "auth_type": "api_key", "key_id": 9, "scopes": scopes}


def _session(role="admin"):
    return {"id": 1, "username": "u", "role": role, "projects": {},
            "auth_type": "session"}


def test_readonly_key_detected():
    assert apikey.is_readonly_key(_key(["readonly"])) is True
    assert apikey.is_readonly_key(_key([])) is False      # 空 = 继承 owner
    assert apikey.is_readonly_key(_session()) is False    # 会话无此概念
    assert apikey.is_readonly_key(None) is False


def test_readonly_key_cannot_write():
    with pytest.raises(HTTPException) as exc:
        require_write(_key(["readonly"]))
    assert exc.value.status_code == 403
    assert "只读" in str(exc.value.detail)


def test_readonly_key_cannot_delete():
    with pytest.raises(HTTPException) as exc:
        require_delete(_key(["readonly"]))
    assert exc.value.status_code == 403
    assert "不能删除" in str(exc.value.detail)


def test_empty_scope_key_inherits_owner():
    """空 scopes 是历史行为：继承 owner 角色，不受影响。"""
    assert require_write(_key([]))["role"] == "admin"
    assert require_delete(_key([]))["role"] == "admin"


def test_session_writer_allowed():
    assert require_write(_session())["role"] == "admin"
    assert require_delete(_session())["role"] == "admin"


def test_viewer_cannot_delete():
    with pytest.raises(HTTPException) as exc:
        require_delete(_session("viewer"))
    assert exc.value.status_code == 403


def test_read_scopes_handles_none_and_error():
    """读库失败不该让请求挂掉，按空处理（继承 owner）。"""
    class BadConn:
        def cursor(self):
            raise RuntimeError("db down")
    assert apikey._read_scopes(1, BadConn()) == []
