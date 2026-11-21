"""登录失败限速（审计 C-05）。

沿用仓库习惯：直接调用端点函数，conn 用最小 FakeConn。
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402
from api import auth as _auth  # noqa: E402
from api.auth import api_login  # noqa: E402
from api.state import _LOGIN_FAILS
from api.state import _LOGIN_FAILS_LOCK
from api.state import _LOGIN_FAIL_MAX  # noqa: E402


class Req:
    """login 只用到 request.client.host / request.headers。"""

    def __init__(self, ip: str = "203.0.113.9"):
        self.headers: dict = {}
        self.client = types.SimpleNamespace(host=ip)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self.conn.row


class FakeConn:
    def __init__(self, row=(1, "hash", "admin", True)):
        self.row = row

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    with _LOGIN_FAILS_LOCK:
        _LOGIN_FAILS.clear()
    monkeypatch.setattr(_auth, "verify_pw", lambda pw, h: pw == "good")
    monkeypatch.setattr(_auth, "create_session", lambda *a, **k: "tok-1")
    monkeypatch.setattr(_auth, "audit", lambda *a, **k: None)
    yield
    with _LOGIN_FAILS_LOCK:
        _LOGIN_FAILS.clear()


def _login(password: str, *, username: str = "admin", ip: str = "203.0.113.9"):
    return api_login(Req(ip), {"username": username, "password": password},
                     conn=FakeConn())


def test_11th_failure_returns_429():
    for _ in range(_LOGIN_FAIL_MAX):
        with pytest.raises(app.HTTPException) as exc:
            _login("bad")
        assert exc.value.status_code == 401

    with pytest.raises(app.ApiError) as exc:
        _login("bad")
    assert exc.value.status_code == 429
    assert exc.value.code == "TOO_MANY_ATTEMPTS"
    assert exc.value.retry_after and exc.value.retry_after > 0


def test_success_clears_failures():
    for _ in range(5):
        with pytest.raises(app.HTTPException):
            _login("bad")

    assert _login("good")["token"] == "tok-1"

    # 清零后又能连续失败 10 次而不被锁
    for _ in range(_LOGIN_FAIL_MAX):
        with pytest.raises(app.HTTPException) as exc:
            _login("bad")
        assert exc.value.status_code == 401


def test_different_ip_counts_independently():
    for _ in range(_LOGIN_FAIL_MAX):
        with pytest.raises(app.HTTPException):
            _login("bad", ip="203.0.113.9")

    with pytest.raises(app.ApiError) as exc:
        _login("bad", ip="203.0.113.9")
    assert exc.value.status_code == 429

    # 换 IP 不受影响
    assert _login("good", ip="198.51.100.7")["token"] == "tok-1"


def test_different_username_counts_independently():
    for _ in range(_LOGIN_FAIL_MAX):
        with pytest.raises(app.HTTPException):
            _login("bad", username="admin")

    assert _login("good", username="other")["token"] == "tok-1"
