"""账号与 API Key：密码哈希、会话、登录登出、Key 管理。"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request

from . import apikey as _apikey
from . import keysafe as _keysafe
from .deps import current_user, db, require_admin
from .errors import ApiError
from .shared.audit import audit
from . import state as _state
from .state import (TOKEN_TTL_HOURS, TZ, _LOGIN_FAILS, _LOGIN_FAILS_LOCK,
                    _LOGIN_FAIL_MAX, _LOGIN_FAIL_WINDOW)

router = APIRouter()


# ---------- 密码 ----------
def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_pw(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), h.encode())
    except Exception:
        return False

# ---------- 认证 ----------
def create_session(conn, user_id: int, ip: str, ua: str) -> str:
    token = secrets.token_urlsafe(32)
    exp = datetime.now(TZ) + timedelta(hours=TOKEN_TTL_HOURS)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions(user_id, token, expires_at, ip, ua) VALUES(%s,%s,%s,%s,%s)",
            (user_id, token, exp, ip, ua),
        )
    conn.commit()
    return token

def get_user(conn, token: str):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT u.id, u.username, u.role, u.enabled, s.expires_at, u.real_name
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = %s""",
            (token,),
        )
        row = cur.fetchone()
    if not row or not row[3]:
        return None
    if row[4] < datetime.now(TZ):
        return None
    return {"id": row[0], "username": row[1], "role": row[2],
            "real_name": row[5] or ""}

def user_projects(conn, user_id: int, role: str) -> dict[str, str]:
    """项目授权：{项目名: 'readonly'|'write'}。

    admin 返回配置里的全部项目（write）。普通账号查 user_projects.access；
    零项目返回空 dict，调用方必须当成「没有数据」，不能兜底成全部。
    """
    return _apikey.load_projects(conn, user_id, role)


# ---------- 登录限速 ----------
# 计数状态在 api.state，读写逻辑跟着登录端点放在这里。
def _client_ip(request: Request) -> str:
    """取客户端 IP；拿不到时用空串，避免 None 拼进 key。"""
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "")


def _prune_login_fails(now: float) -> None:
    """清掉窗口外的失败时间戳，防止字典无限增长。调用方必须持锁。"""
    cutoff = now - _LOGIN_FAIL_WINDOW
    for key in list(_LOGIN_FAILS):
        kept = [ts for ts in _LOGIN_FAILS[key] if ts > cutoff]
        if kept:
            _LOGIN_FAILS[key] = kept
        else:
            del _LOGIN_FAILS[key]


def _check_login_rate(key: str, now: float) -> None:
    """窗口内失败已达上限就抛 429；调用方在拿到 now 后、校验密码前调用。"""
    with _LOGIN_FAILS_LOCK:
        _prune_login_fails(now)
        recent = _LOGIN_FAILS.get(key, [])
        if len(recent) >= _LOGIN_FAIL_MAX:
            # 最早一次失败滚出窗口后才能重试，至少 1 秒，避免提示 0 秒
            retry_after = max(1, int(_LOGIN_FAIL_WINDOW - (now - min(recent))) + 1)
            raise ApiError(
                429, "TOO_MANY_ATTEMPTS",
                f"登录失败次数过多，请 {retry_after} 秒后再试",
                f"{_LOGIN_FAIL_WINDOW // 60} 分钟内同一账号 + IP 失败 "
                f"{_LOGIN_FAIL_MAX} 次会暂时锁定",
                retry_after=retry_after)


def _record_login_fail(key: str) -> None:
    with _LOGIN_FAILS_LOCK:
        _LOGIN_FAILS.setdefault(key, []).append(time.monotonic())


def _clear_login_fail(key: str) -> None:
    """成功登录清空该 key 的失败记录，避免正常用户被历史失败拖累。"""
    with _LOGIN_FAILS_LOCK:
        _LOGIN_FAILS.pop(key, None)


# ---------- 登录 / 会话端点 ----------
@router.post("/api/login")
def api_login(request: Request, body: dict, conn=Depends(db)):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "用户名或密码为空")
    ip = _client_ip(request)
    key = f"{username}|{ip}"
    now = time.monotonic()
    _check_login_rate(key, now)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, password_hash, role, enabled FROM users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
    if not row or not verify_pw(password, row[1]):
        _record_login_fail(key)
        raise HTTPException(401, "用户名或密码错误")
    if not row[3]:
        raise HTTPException(403, "账号已禁用")
    _clear_login_fail(key)
    token = create_session(conn, row[0], ip, request.headers.get("user-agent", ""))
    audit(conn, row[0], "login", ip=ip)
    return {"token": token, "username": username, "role": row[2]}

@router.get("/api/me")
def api_me(user=Depends(current_user)):
    return {"id": user["id"], "username": user["username"],
            "real_name": user.get("real_name", ""),
            "role": user["role"], "projects": user["projects"]}

@router.post("/api/logout")
def api_logout(request: Request, user=Depends(current_user), conn=Depends(db)):
    """退出登录：服务端删 session，旧 token 立即失效（不是只清前端 localStorage）。"""
    token = request.headers.get("x-token", "")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
    conn.commit()
    audit(conn, user["id"], "logout", ip=request.client.host)
    return {"ok": True}


# ---------- /api/sw 身份与 Key ----------
@router.get("/api/sw/me")
def api_sw_me(user=Depends(current_user)):
    """当前身份、角色、项目权限与认证方式。"""
    return {
        "username": user["username"],
        "real_name": user.get("real_name", ""),
        "role": user["role"],
        "projects": user.get("projects") or {},
        "auth_type": user.get("auth_type", "session"),
    }


@router.post("/api/sw/keys")
def api_create_key(body: dict, request: Request, conn=Depends(db),
                   user=Depends(require_admin)):
    """签发 API Key。

    明文同时用对称加密存一份（``secret_enc``），供管理员在用户管理页随时查看；
    审计仍只记前缀和名字，绝不把明文写进审计日志。
    """
    username = str(body.get("user") or user["username"]).strip()
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError(400, "VALIDATION", "name 必填")
    expires_days = body.get("expires_days")
    if expires_days is not None:
        try:
            expires_days = int(expires_days)
        except (TypeError, ValueError):
            raise ApiError(400, "VALIDATION", "expires_days 必须是整数或 null")
        if expires_days <= 0:
            raise ApiError(400, "VALIDATION", "expires_days 必须大于 0")

    with conn.cursor() as cur:
        cur.execute("SELECT id, role, enabled FROM users WHERE username = %s",
                    (username,))
        row = cur.fetchone()
    if not row or not row[2]:
        raise ApiError(400, "VALIDATION", f"用户不存在或已禁用：{username}")

    plain, digest, prefix = _apikey.generate_key()
    expires_at = (datetime.now(TZ) + timedelta(days=expires_days)
                  if expires_days else None)
    scopes = body.get("scopes")
    if scopes is not None and not isinstance(scopes, list):
        raise ApiError(400, "VALIDATION", "scopes 必须是数组或 null")
    # 加密失败不阻断签发：认证仍可用 key_hash，只是后台看不到明文
    try:
        secret_enc = _keysafe.encrypt(plain, _state.ROOT)
    except Exception:
        secret_enc = None
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO api_keys(user_id, name, key_hash, key_prefix,
                                    scopes, expires_at, secret_enc)
               VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (row[0], name, digest, prefix,
             [str(x) for x in scopes] if scopes else None, expires_at,
             secret_enc),
        )
        key_id = cur.fetchone()[0]
    conn.commit()

    # 审计只记前缀和名字，明文 Key 绝不进审计
    audit(conn, user["id"], "create_api_key", target=f"api_key:{key_id}",
          detail={"user": username, "name": name, "prefix": prefix,
                  "expires_at": str(expires_at) if expires_at else None},
          ip=request.client.host)
    return {
        "key": plain,
        "id": key_id,
        "name": name,
        "prefix": prefix,
        "user": username,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@router.get("/api/sw/keys")
def api_list_keys(conn=Depends(db), user=Depends(current_user)):
    """admin 看全部；普通账号只看自己的。

    ``secret`` 是解密后的完整明文；旧 Key（secret_enc 为 NULL）或密钥文件丢失时为
    null，前端提示「旧 Key，无法查看明文，请重新签发」。完整哈希永不返回。
    """
    with conn.cursor() as cur:
        if user["role"] == "admin":
            cur.execute(
                """SELECT k.id, k.user_id, u.username, u.real_name, k.name,
                          k.key_prefix, k.scopes, k.expires_at, k.created_at,
                          k.last_used_at, k.revoked_at, k.secret_enc
                   FROM api_keys k JOIN users u ON u.id = k.user_id
                   ORDER BY k.id DESC""")
        else:
            cur.execute(
                """SELECT k.id, k.user_id, u.username, u.real_name, k.name,
                          k.key_prefix, k.scopes, k.expires_at, k.created_at,
                          k.last_used_at, k.revoked_at, k.secret_enc
                   FROM api_keys k JOIN users u ON u.id = k.user_id
                   WHERE k.user_id = %s ORDER BY k.id DESC""", (user["id"],))
        rows = cur.fetchall()
    now = datetime.now(timezone.utc)
    items = []
    for r in rows:
        exp = r[7]
        expired = bool(exp and (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)) <= now)
        secret = None
        # 兼容单测里的旧 11 列假行；真实查询固定有第 12 列 secret_enc
        if len(r) > 11 and r[11]:
            try:
                secret = _keysafe.decrypt(r[11], _state.ROOT)
            except Exception:
                secret = None
        items.append({
            "id": r[0], "user_id": r[1], "user": r[2],
            "real_name": r[3] or "", "name": r[4],
            "prefix": r[5], "scopes": r[6],
            "expires_at": str(r[7]) if r[7] else None,
            "created_at": str(r[8]) if r[8] else None,
            "last_used_at": str(r[9]) if r[9] else None,
            "revoked_at": str(r[10]) if r[10] else None,
            "revoked": r[10] is not None,
            "active": r[10] is None and not expired,
            "secret": secret,
        })
    return {"total": len(items), "items": items}


@router.delete("/api/sw/keys/{key_id}")
def api_revoke_key(key_id: int, request: Request, conn=Depends(db),
                   user=Depends(current_user)):
    """吊销（软删，写 revoked_at）。只能吊销自己的；admin 可吊销任何。"""
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, name, revoked_at FROM api_keys WHERE id = %s",
                    (key_id,))
        row = cur.fetchone()
    if not row:
        raise ApiError(404, "KEY_NOT_FOUND", f"API Key 不存在：{key_id}")
    if user["role"] != "admin" and row[0] != user["id"]:
        raise ApiError(403, "FORBIDDEN", "只能吊销自己的 API Key")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
            (key_id,))
        changed = cur.rowcount
    conn.commit()
    audit(conn, user["id"], "revoke_api_key", target=f"api_key:{key_id}",
          detail={"name": row[1], "changed": changed}, ip=request.client.host)
    return {"ok": True, "id": key_id, "revoked": bool(changed)}
