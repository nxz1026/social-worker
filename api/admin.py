"""用户管理（仅 admin）：增删改查、会话踢出、审计查看。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import hash_pw, user_projects
from .deps import db, project_short, require_admin
from .shared.audit import audit
from .shared.parse import as_bool
from .state import _PROJECT_ACCESS

from engine import projects as _projects

router = APIRouter()


@router.get("/api/admin/users")
def admin_users(conn=Depends(db), user=Depends(require_admin)):
    """用户列表：真实姓名 + 项目权限明细（{项目名: readonly/write}）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, real_name, role, enabled, created_at "
            "FROM users ORDER BY id")
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "username": r[1], "real_name": r[2] or "",
            "role": r[3], "enabled": r[4], "created_at": str(r[5]),
            # admin 靠角色恒有全部权限，这里同样给出配置里的全部项目
            "projects": user_projects(conn, r[0], r[3]),
        })
    return {"items": out}

def valid_role(role: str) -> str:
    if role not in ("admin", "editor", "viewer"):
        raise HTTPException(400, "角色非法")
    return role

def _canonical_project(name: str) -> str:
    """项目授权统一存中文项目名（目录名）；传 short 时尽量归一到目录名。"""
    raw = str(name or "").strip()
    if not raw:
        return ""
    try:
        for item in _projects.list_projects():
            pname = str(item.get("name") or "")
            if raw in (pname, project_short(item)):
                return pname
    except Exception:
        pass
    return raw

def _projects_payload(raw) -> dict[str, str]:
    """projects 入参归一：dict{项目: readonly/write}；旧 list 按全 write 兼容。"""
    if raw is None:
        return {}
    if isinstance(raw, list):
        items = [(p, "write") for p in raw]
    elif isinstance(raw, dict):
        items = list(raw.items())
    else:
        raise HTTPException(400, "projects 必须是对象或数组")
    out: dict[str, str] = {}
    for name, access in items:
        key = _canonical_project(name)
        if not key:
            continue
        acc = str(access or "write").strip().lower()
        if acc not in _PROJECT_ACCESS:
            raise HTTPException(400, f"项目权限非法：{access}（只能 readonly / write）")
        out[key] = acc
    return out

def _save_projects(cur, uid: int, projects: dict[str, str]) -> None:
    """覆盖式写项目授权：先清空该用户的旧授权，再逐条插入。"""
    cur.execute("DELETE FROM user_projects WHERE user_id=%s", (uid,))
    for name, access in projects.items():
        cur.execute(
            "INSERT INTO user_projects(user_id, project, access) VALUES(%s,%s,%s)",
            (uid, name, access))

@router.post("/api/admin/users")
def admin_create_user(body: dict, request: Request, conn=Depends(db),
                      user=Depends(require_admin)):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    real_name = str(body.get("real_name") or "").strip()
    role = valid_role(body.get("role") or "viewer")
    projects = _projects_payload(body.get("projects"))
    if not username or not password:
        raise HTTPException(400, "用户名或密码为空")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            raise HTTPException(409, "用户名已存在")
        cur.execute(
            "INSERT INTO users(username, password_hash, role, real_name) "
            "VALUES(%s,%s,%s,%s) RETURNING id",
            (username, hash_pw(password), role, real_name),
        )
        uid = cur.fetchone()[0]
        _save_projects(cur, uid, projects)
    conn.commit()
    audit(conn, user["id"], "create_user", target=username,
          detail={"role": role, "real_name": real_name, "projects": projects},
          ip=request.client.host)
    return {"ok": True, "id": uid}

@router.patch("/api/admin/users/{uid}")
def admin_update_user(uid: int, body: dict, request: Request, conn=Depends(db),
                      user=Depends(require_admin)):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE id = %s", (uid,))
        if not cur.fetchone():
            raise HTTPException(404, "用户不存在")
        if "real_name" in body:
            cur.execute("UPDATE users SET real_name=%s WHERE id=%s",
                        (str(body["real_name"] or "").strip(), uid))
        if "role" in body:
            new_role = valid_role(body["role"])
            # 不能把自己降级：降完就没有 admin 能进管理页（审计发现的实际事故）
            if uid == user["id"] and new_role != "admin":
                raise HTTPException(400, "不能修改自己的管理员角色")
            cur.execute("UPDATE users SET role=%s WHERE id=%s", (new_role, uid))
        if "enabled" in body:
            new_enabled = as_bool(body["enabled"])
            # 不能禁用自己：禁用后 Web 与 API 都进不来，只能改库恢复
            if uid == user["id"] and not new_enabled:
                raise HTTPException(400, "不能禁用自己")
            cur.execute("UPDATE users SET enabled=%s WHERE id=%s",
                        (new_enabled, uid))
        if "password" in body and body["password"]:
            cur.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                        (hash_pw(body["password"]), uid))
            # 改密后旧 token 必须立即失效，否则泄漏的会话还能用满 12 小时
            cur.execute("DELETE FROM sessions WHERE user_id=%s", (uid,))
        if "projects" in body:
            _save_projects(cur, uid, _projects_payload(body["projects"]))
    conn.commit()
    audit(conn, user["id"], "update_user", target=str(uid),
          detail=body, ip=request.client.host)
    return {"ok": True}

@router.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, request: Request, conn=Depends(db),
                      user=Depends(require_admin)):
    """硬删用户：外键 ON DELETE CASCADE 会连带清掉 api_keys/sessions/user_projects。

    两条硬约束：不能删自己；不能删掉最后一个 admin（否则没人能再进管理页）。
    """
    if uid == user["id"]:
        raise HTTPException(400, "不能删除自己")
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE id = %s", (uid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "用户不存在")
        if row[0] == "admin":
            cur.execute("SELECT count(*) FROM users WHERE role = 'admin'")
            if int(cur.fetchone()[0]) <= 1:
                raise HTTPException(400, "不能删除最后一个 admin")
        cur.execute("DELETE FROM users WHERE id = %s", (uid,))
        removed = cur.rowcount
    conn.commit()
    audit(conn, user["id"], "delete_user", target=str(uid),
          detail={"removed": removed}, ip=request.client.host)
    return {"ok": True, "id": uid, "removed": bool(removed)}

@router.delete("/api/admin/users/{uid}/sessions")
def admin_revoke_sessions(uid: int, request: Request, conn=Depends(db),
                          user=Depends(require_admin)):
    """踢掉某个用户的全部会话（改密、离职、token 泄漏时用）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE id=%s", (uid,))
        if not cur.fetchone():
            raise HTTPException(404, "用户不存在")
        cur.execute("DELETE FROM sessions WHERE user_id=%s", (uid,))
        removed = cur.rowcount
    conn.commit()
    audit(conn, user["id"], "revoke_sessions", target=str(uid),
          detail={"removed": removed}, ip=request.client.host)
    return {"ok": True, "removed": removed}

@router.get("/api/admin/audit")
def admin_audit(limit: int = 100, conn=Depends(db), user=Depends(require_admin)):
    limit = max(1, min(int(limit), 500))
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.id, u.username, a.action, a.project, a.target, a.detail, a.ip, a.ts
               FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
               ORDER BY a.id DESC LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()
    return {"items": [dict(zip(
        ["id", "username", "action", "project", "target", "detail", "ip", "ts"], r))
        for r in rows]}
