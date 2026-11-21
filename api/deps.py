"""FastAPI 依赖：数据库连接、身份解析、权限校验。

这是安全边界，改动需完整回归。所有端点都经这里拿 user。
"""
from __future__ import annotations

import psycopg
from fastapi import Depends, HTTPException, Request

from .apikey import KEY_PREFIX, verify
from .errors import ApiError
from .state import PG_DSN

# engine 是仓库根下的包；app.py 在 import 本模块前已把仓库根放进 sys.path。
from engine import projects as _projects


# ---------- DB ----------
def db():
    conn = psycopg.connect(PG_DSN)
    try:
        yield conn
    finally:
        conn.close()


# ---------- 身份 ----------
def _bearer_token(request: Request) -> str:
    """从 Authorization: Bearer ... 或 x-api-key 里取 API Key 明文。"""
    auth = str(request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(request.headers.get("x-api-key") or "").strip()


def current_user(request: Request, conn=Depends(db)):
    """同时支持两种认证：

    1) API Key：Authorization: Bearer sk-sw-xxx / x-api-key: sk-sw-xxx
    2) Web 会话：x-token（保持不变）

    注意：只要请求带了 sk-sw- 开头的 Key，就不会再退回 x-token，
    避免一个错误的 Key 被同请求里的旧 session 悄悄放行。
    """
    # 延迟 import：auth 端点依赖本模块（Depends），本模块只在调用时用它的
    # session 查询函数，避免 deps / auth 在 import 期互相等待。
    from .auth import get_user, user_projects

    key = _bearer_token(request)
    if key:
        if not key.startswith(KEY_PREFIX):
            raise HTTPException(401, "认证失败：不是有效的 API Key")
        u = verify(conn, key)
        if not u:
            raise HTTPException(401, "认证失败：API Key 无效、已吊销或已过期")
        return u

    token = request.headers.get("x-token")   # 只走 header，避免 token 进 URL/日志
    if not token:
        raise HTTPException(401, "未登录")
    u = get_user(conn, token)
    if not u:
        raise HTTPException(401, "登录已过期")
    u["projects"] = user_projects(conn, u["id"], u["role"])
    u["auth_type"] = "session"
    return u


def require_write(user=Depends(current_user)):
    """全局写权限（editor/admin）。

    项目级细粒度（readonly/write）由各端点自己调 check_project(write=True)。
    """
    if user["role"] not in ("admin", "editor"):
        raise HTTPException(403, "无写权限（需 editor/admin）")
    return user


def require_admin(user=Depends(current_user)):
    if user.get("auth_type") == "api_key":
        raise HTTPException(
            403, "API Key 不能访问管理接口（用户管理与审计只允许 Web 登录会话）")
    if user["role"] != "admin":
        raise HTTPException(403, "需 admin")
    return user


# ---------- 项目权限 ----------
def project_short(item: dict) -> str:
    """项目 yaml 的 short（= 目录名）；没配 short 时退回目录名。"""
    data = item.get("data") or {}
    proj = data.get("project") if isinstance(data, dict) else None
    if not isinstance(proj, dict):
        return str(item.get("name") or "")
    return str(proj.get("short") or item.get("name") or "")


def _project_keys(project: str) -> set[str]:
    """一个项目可能的授权键：项目名（目录名）与 yaml 里的 project.short。

    不再做 huimin / huadu 这类历史英文键的双向兼容：user_projects 里存的就是
    中文项目名。传入 short 或项目名都能认，是为了调用方少踩坑。
    """
    raw = str(project or "").strip()
    if not raw:
        return set()
    keys = {raw}
    try:
        meta = _projects.project_meta(raw)
        keys.update({str(meta.get("name") or raw), str(meta.get("short") or raw)})
        return {k for k in keys if k}
    except (FileNotFoundError, ValueError):
        pass
    # 传入的可能是 short（或旧写法）：扫一遍项目列表，找回目录名与 short
    try:
        for item in _projects.list_projects():
            name = str(item.get("name") or "")
            short = project_short(item)
            if raw in (name, short):
                keys.update({name, short})
                break
    except Exception:
        pass
    return {k for k in keys if k}


def project_access(user, project: str) -> str | None:
    """返回该项目对当前用户的 access（readonly/write）；无权限返回 None。"""
    projects = user.get("projects") or {}
    for key in _project_keys(project):
        access = projects.get(key)
        if access:
            return str(access)
    return None


def can_write(user, project: str) -> bool:
    """该项目是否可写。admin 恒 True。"""
    if user.get("role") == "admin":
        return True
    return project_access(user, project) == "write"


def check_project(user, project: str, *, write: bool = False):
    """项目存在性之外的授权检查：读权限 + （可选）写权限。

    admin 恒通过；其它账号必须在 user_projects 授权范围内。
    readonly 项目调写操作 → 403 READONLY。
    """
    if user.get("role") == "admin":
        return
    access = project_access(user, project)
    if access is None:
        raise ApiError(403, "FORBIDDEN", f"无项目 {project} 权限")
    if write and access != "write":
        raise ApiError(403, "READONLY", f"项目 {project} 只读，不能执行写操作",
                       "向管理员申请该项目的 write 权限")


def scopes_of(user, project: str = "") -> list[str]:
    """本次请求允许访问的项目集合（项目名列表）。

    零项目账号返回空列表 —— 绝不能兜底成 `["huadu"]` 或「不过滤」。
    调用方必须把空列表当成「没有数据」，而不是「全部数据」。
    """
    if project:
        if user.get("role") != "admin" and project_access(user, project) is None:
            raise ApiError(403, "FORBIDDEN", f"无项目 {project} 权限")
        return [project]
    return list((user.get("projects") or {}).keys())


def require_project_write(user, meta: dict) -> None:
    """写权限：admin/editor；非 admin 还要求该项目 access=write。"""
    if user["role"] not in ("admin", "editor"):
        raise HTTPException(403, "无写权限（需 editor/admin）")
    # 同时认目录名与 short，历史授权数据两种写法都能命中
    check_project(user, meta["name"], write=True)
