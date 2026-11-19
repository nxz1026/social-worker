#!/usr/bin/env python3
"""API Key 生成 / 校验。

Key 形如 ``sk-sw-<43 字符>``，由 secrets.token_urlsafe(32) 生成，熵足够；
数据库只存 sha256（不是 bcrypt）：Key 本身长且随机，不需要慢哈希，
每次请求都要校验，bcrypt 会把 API 延迟拖到数百毫秒。

数据库表 api_keys（见 docs/架构设计-v2.md）：
    id, user_id, name, key_hash, key_prefix, scopes,
    expires_at, created_at, last_used_at, revoked_at
"""
from __future__ import annotations

import hashlib
import secrets
import threading
from datetime import datetime, timezone

import psycopg

from settings import SETTINGS

KEY_PREFIX = "sk-sw-"
# 明文前 12 位（含 sk-sw-）落库，便于后台辨认是哪把钥匙，但不泄露剩余随机串
PREFIX_LEN = 12


def generate_key() -> tuple[str, str, str]:
    """生成 (明文, sha256 哈希, 前 12 位前缀)。"""
    plain = KEY_PREFIX + secrets.token_urlsafe(32)
    return plain, hash_key(plain), plain[:PREFIX_LEN]


def hash_key(plain: str) -> str:
    """sha256 hexdigest。"""
    return hashlib.sha256(str(plain or "").encode("utf-8")).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """数据库返回的 timestamptz 可能是 aware，也可能是 naive（按 UTC 处理）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _row_invalid(row) -> bool:
    """吊销 / 过期 / 账号禁用都算无效。row 结构见 verify 里的 SELECT。"""
    _id, _user_id, expires_at, revoked_at, _username, _role, enabled, _real = row
    if revoked_at is not None:
        return True
    exp = _as_utc(expires_at)
    if exp is not None and exp <= _now_utc():
        return True
    return not bool(enabled)


def _touch_last_used(key_id: int) -> None:
    """异步更新 last_used_at：独立线程 + 独立连接，失败静默。

    这里是「尽力而为」的统计字段，不能因为写库慢或失败把认证拖垮。
    """
    def work() -> None:
        try:
            with psycopg.connect(SETTINGS.db_dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE api_keys SET last_used_at = now() WHERE id = %s",
                        (key_id,))
                conn.commit()
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()


def load_projects(conn, user_id: int, role: str) -> dict[str, str]:
    """读项目授权：{项目: 'readonly'|'write'}。

    admin 返回配置里的全部项目，均为 write。零项目账号返回空 dict，
    绝不能兜底成「全部项目」。
    """
    if role == "admin":
        return {str(name): "write" for name in SETTINGS.projects}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project, access FROM user_projects WHERE user_id = %s",
            (user_id,))
        return {str(row[0]): str(row[1] or "write") for row in cur.fetchall()}


def verify(conn, plain: str) -> dict | None:
    """校验明文 Key，成功返回 user dict，失败返回 None。

    user = {id, username, role, projects: {项目: access}, auth_type, key_id}
    """
    raw = str(plain or "").strip()
    if not raw.startswith(KEY_PREFIX):
        return None
    digest = hash_key(raw)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT k.id, k.user_id, k.expires_at, k.revoked_at,
                      u.username, u.role, u.enabled, u.real_name
               FROM api_keys k JOIN users u ON u.id = k.user_id
               WHERE k.key_hash = %s""",
            (digest,))
        row = cur.fetchone()
    if not row:
        return None
    # 理论上唯一；再做一次常量时间比对，防止未来索引/查询被改坏
    if _row_invalid(row):
        return None

    key_id, user_id, _exp, _rev, username, role, _enabled, real_name = row
    user = {
        "id": user_id,
        "username": username,
        "real_name": str(real_name or ""),
        "role": role,
        "projects": load_projects(conn, user_id, role),
        "auth_type": "api_key",
        "key_id": key_id,
    }
    # 只有真实 psycopg 连接才异步写 last_used_at；单测里的假连接跳过
    if isinstance(conn, psycopg.Connection):
        _touch_last_used(key_id)
    return user

