#!/usr/bin/env python3
"""认证链测试：补审计 #12 指出的缺口。

审计指出这几块完全没测：
  - hash_pw / verify_pw
  - get_user（session 路径）
  - create_session
  - 无效 API Key 不得回退到合法 x-token（最关键的不变量）
  - download_source 的路径越界校验

这里用真实数据库连接（socialwork 库），不 mock，因为要测的正是「认证行为」。
没有数据库时整体 skip，不假通过。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))

os.environ.setdefault("SOCIAL_WORK_ROOT", "/srv/social-work")
os.environ.setdefault(
    "SOCIALWORK_DB_DSN",
    "host=127.0.0.1 port=5432 dbname=socialwork user=admin password=tjfjyjr")

psycopg = pytest.importorskip("psycopg")

import app  # noqa: E402
import apikey  # noqa: E402


def _db():
    """连库；连不上就 skip（不用假的连接冒充真实行为）。"""
    try:
        return psycopg.connect(app.PG_DSN)
    except Exception as exc:                       # pragma: no cover
        pytest.skip(f"连不上数据库：{exc}")


# --------------------------------------------------------------------------
# 密码哈希
# --------------------------------------------------------------------------

def test_hash_pw_is_bcrypt_not_plaintext():
    h = app.hash_pw("correct-horse")
    assert h != "correct-horse"
    assert h.startswith("$2"), "bcrypt 哈希应以 $2 开头"


def test_hash_pw_same_input_different_salt():
    """bcrypt 每次加盐，同密码两次哈希必然不同，但都能校验通过。"""
    h1 = app.hash_pw("same-password")
    h2 = app.hash_pw("same-password")
    assert h1 != h2
    assert app.verify_pw("same-password", h1)
    assert app.verify_pw("same-password", h2)


def test_check_pw_rejects_wrong_password():
    h = app.hash_pw("right")
    assert not app.verify_pw("wrong", h)


def test_check_pw_bad_hash_returns_false():
    """损坏的哈希不能让整个登录流程抛 500。"""
    assert app.verify_pw("x", "not-a-bcrypt-hash") is False


# --------------------------------------------------------------------------
# session 路径
# --------------------------------------------------------------------------

def test_create_session_and_get_user_roundtrip():
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, role FROM users WHERE enabled ORDER BY id LIMIT 1")
            row = cur.fetchone()
        if not row:
            pytest.skip("库里没有可用用户")
        uid, username, role = row

        token = app.create_session(conn, uid, "127.0.0.1", "pytest")
        assert token and len(token) >= 32, "session token 应足够长"

        user = app.get_user(conn, token)
        assert user is not None, "刚创建的 session 必须能查到用户"
        assert user["id"] == uid
        assert user["username"] == username
        assert user["role"] == role

        # 用完清掉，避免污染 sessions 表
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token = %s", (token,))
        conn.commit()
    finally:
        conn.close()


def test_get_user_rejects_unknown_token():
    conn = _db()
    try:
        assert app.get_user(conn, "definitely-not-a-real-token") is None
    finally:
        conn.close()


def test_get_user_rejects_expired_session():
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE enabled ORDER BY id LIMIT 1")
            row = cur.fetchone()
        if not row:
            pytest.skip("库里没有可用用户")

        token = "pytest-expired-token"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions(user_id, token, expires_at) "
                "VALUES(%s, %s, now() - interval '1 hour')", (row[0], token))
        conn.commit()
        try:
            assert app.get_user(conn, token) is None, "过期 session 必须不认"
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE token = %s", (token,))
            conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 最关键的不变量：无效 Key 不得回退到 session
# --------------------------------------------------------------------------

def test_invalid_api_key_does_not_fall_back_to_session(monkeypatch):
    """带了无效 sk-sw- Key 时，即使同时带了合法 x-token 也必须 401。

    审计 #12 指出此前这条完全没有测试。回退会造成：Key 被吊销后
    浏览器 session 仍能通过，等于吊销不生效。
    """
    from fastapi import HTTPException

    class FakeReq:
        def __init__(self, headers):
            self.headers = headers

    conn = _db()
    try:
        # 构造一个合法的 session token
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE enabled ORDER BY id LIMIT 1")
            row = cur.fetchone()
        if not row:
            pytest.skip("库里没有可用用户")
        good_token = app.create_session(conn, row[0], "127.0.0.1", "pytest")

        req = FakeReq({
            "authorization": "Bearer sk-sw-this-is-not-valid-at-all",
            "x-token": good_token,          # 合法的 session 也在场
        })
        with pytest.raises(HTTPException) as exc:
            app.current_user(req, conn)
        assert exc.value.status_code == 401, "无效 Key + 合法 session 必须 401"

        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token = %s", (good_token,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# API Key 校验
# --------------------------------------------------------------------------

def test_verify_rejects_revoked_key():
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE enabled ORDER BY id LIMIT 1")
            row = cur.fetchone()
        if not row:
            pytest.skip("库里没有可用用户")
        uid = row[0]

        plain, digest, prefix = apikey.generate_key()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys(user_id, name, key_hash, key_prefix, revoked_at) "
                "VALUES(%s, %s, %s, %s, now()) RETURNING id",
                (uid, "pytest-revoked", digest, prefix))
            key_id = cur.fetchone()[0]
        conn.commit()
        try:
            assert apikey.verify(conn, plain) is None, "已吊销的 Key 必须失效"
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_keys WHERE id = %s", (key_id,))
            conn.commit()
    finally:
        conn.close()


def test_verify_rejects_expired_key():
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE enabled ORDER BY id LIMIT 1")
            row = cur.fetchone()
        if not row:
            pytest.skip("库里没有可用用户")
        uid = row[0]

        plain, digest, prefix = apikey.generate_key()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys(user_id, name, key_hash, key_prefix, expires_at) "
                "VALUES(%s, %s, %s, %s, now() - interval '1 day') RETURNING id",
                (uid, "pytest-expired", digest, prefix))
            key_id = cur.fetchone()[0]
        conn.commit()
        try:
            assert apikey.verify(conn, plain) is None, "过期 Key 必须失效"
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_keys WHERE id = %s", (key_id,))
            conn.commit()
    finally:
        conn.close()


def test_verify_accepts_valid_key_and_updates_last_used():
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE enabled ORDER BY id LIMIT 1")
            row = cur.fetchone()
        if not row:
            pytest.skip("库里没有可用用户")
        uid = row[0]

        plain, digest, prefix = apikey.generate_key()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys(user_id, name, key_hash, key_prefix) "
                "VALUES(%s, %s, %s, %s) RETURNING id",
                (uid, "pytest-valid", digest, prefix))
            key_id = cur.fetchone()[0]
        conn.commit()
        try:
            user = apikey.verify(conn, plain)
            assert user is not None
            assert user["id"] == uid
            assert user.get("auth_type") == "api_key"
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_keys WHERE id = %s", (key_id,))
            conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# download_source 路径越界
# --------------------------------------------------------------------------

def test_normalize_rel_strips_traversal():
    """`..` 必须被吃掉，归一结果不能带 `..` 段。

    端点里还有第二道防线（resolve 后 relative_to(ROOT)），这里只验证第一道。
    """
    assert ".." not in app._normalize_rel("../../etc/passwd")
    assert app._normalize_rel("../../etc/passwd") == "etc/passwd"
    assert app._normalize_rel("sources/a.docx") == "sources/a.docx"
    # 绝对路径越界归一成空串
    assert app._normalize_rel("/etc/passwd") == ""
    assert app._normalize_rel("") == ""


def test_download_source_rejects_absolute_outside():
    """绝对路径指向 ROOT 之外 → 归一为空 → 端点 404，不读文件。"""
    rel = app._normalize_rel("/etc/shadow")
    assert rel == "", "ROOT 之外的绝对路径必须归一为空"
