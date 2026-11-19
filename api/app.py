#!/usr/bin/env python3
"""社工文档服务 API（FastAPI）。

认证：session token（PG sessions 表，Header: x-token）
权限：role(admin/editor/viewer) + user_projects 项目隔离
审计：写操作落 audit_log
归档：调 scripts/archive.py
检索：调 scripts/cognee.py search
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import mimetypes
import os
import posixpath
import re
import secrets
import socket
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import psycopg
import yaml
from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import apikey as _apikey
from settings import SETTINGS
import status as status_mod

ROOT = SETTINGS.root
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
# engine 是仓库根下的包，不在 scripts 里，单独把仓库根加进 sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import name_parse as _np  # noqa: E402
from engine import activities as _activities  # noqa: E402
from engine import activity as _activity  # noqa: E402
from engine import caption as _caption  # noqa: E402
from engine import checks as _checks  # noqa: E402
from engine import coding as _coding  # noqa: E402
from engine import invoice as _invoice  # noqa: E402
from engine import project_checks as _project_checks  # noqa: E402
from engine import readme as _readme  # noqa: E402
from engine import cognee_index as _cognee_index  # noqa: E402
from engine import fill as _fill  # noqa: E402
from engine import ingest as _ingest  # noqa: E402
from engine import llm as _llm  # noqa: E402
from engine import projects as _projects  # noqa: E402
PG_DSN = SETTINGS.db_dsn
TZ = timezone(timedelta(hours=SETTINGS.timezone_offset_hours))
TOKEN_TTL_HOURS = SETTINGS.token_ttl_hours
ALL_PROJECTS = SETTINGS.projects

app = FastAPI(title="社工文档服务", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATES_DIR = SETTINGS.templates_dir
TYPE_MAP = {
    "个案记录": "case-record", "家访提纲": "home-visit-outline",
    "服务计划": "service-plan", "活动方案": "activity-plan",
    "项目申报": "project-application", "评估报告": "evaluation-report",
}

def llm_chat(prompt: str, max_tokens: int | None = None) -> tuple[str, str]:
    """调上游 LLM。三个参数：base_url / api_key / model_id。

    优先级：环境变量 > config/api.yaml 的 llm 段 > 服务端 .env。
    返回 (正文, finish_reason)。
    """
    max_tokens = max_tokens or int(SETTINGS.llm["max_tokens"])
    env: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")

    def pick(key: str, *env_names: str) -> str:
        for name in env_names:
            val = os.environ.get(name)
            if val:
                return val
        val = SETTINGS.llm.get(key)
        if val:
            return str(val)
        for name in env_names:
            if env.get(name):
                return env[name]
        return ""

    base_url = pick("base_url", "LLM_BASE_URL", "LLM_ENDPOINT")
    api_key = pick("api_key", "LLM_API_KEY")
    model_id = pick("model_id", "LLM_MODEL_ID", "LLM_MODEL").split("/")[-1]
    if not base_url or not api_key or not model_id:
        raise RuntimeError(
            "LLM 未配置：在 config/api.yaml 的 llm 段填 base_url / api_key / model_id，"
            "或设置环境变量 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_ID")

    body = {"model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(SETTINGS.llm["temperature"]),
            "max_tokens": max_tokens}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(
                req, timeout=int(SETTINGS.llm["timeout_seconds"])) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            raise TimeoutError(str(exc))
        raise

    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    finish = str(choice.get("finish_reason") or "")
    if not content:
        usage = data.get("usage") or {}
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        raise RuntimeError(
            f"上游没有返回正文（finish_reason={finish}, reasoning_tokens={reasoning}）")
    return content, finish

# ---------- DB ----------
def db():
    conn = psycopg.connect(PG_DSN)
    try:
        yield conn
    finally:
        conn.close()

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

# ---------- 审计 ----------
# 审计明细里绝不能出现口令 / token / 个案正文；只保留排查需要的字段。
_SENSITIVE_DETAIL_KEYS = {
    "password", "passwd", "pwd", "token", "api_key", "apikey", "key",
    "secret", "context", "content",
}

def _safe_detail(detail):
    """剔敏后的审计明细。口令/token/正文一律替换，不落库。"""
    if not isinstance(detail, dict):
        return detail
    out = {}
    for key, value in detail.items():
        if str(key).lower() in _SENSITIVE_DETAIL_KEYS:
            out[key] = "<redacted>"
        elif isinstance(value, str) and len(value) > 300:
            out[key] = value[:300] + "…"
        else:
            out[key] = value
    return out

def audit(conn, user_id, action, project=None, target=None, detail=None, ip=None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO audit_log(user_id, action, project, target, detail, ip)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (user_id, action, project, target,
             json.dumps(_safe_detail(detail), ensure_ascii=False) if detail else None, ip),
        )
    conn.commit()

# ---------- 统一错误 ----------
class ApiError(HTTPException):
    """机器可读的业务错误。

    /api/sw/* 用 {"error": {code, message, hint}}；其余端点仍回 {"detail": ...}，
    保持 Web 界面原有契约不变。
    """

    def __init__(self, status: int, code: str, message: str, hint: str = ""):
        super().__init__(status_code=status, detail=message)
        self.code = code
        self.message = message
        self.hint = hint


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    if request.url.path.startswith("/api/sw/"):
        return JSONResponse(status_code=exc.status_code, content={
            "error": {"code": exc.code, "message": exc.message, "hint": exc.hint}})
    # 非 v1 端点：沿用 FastAPI 默认形状，避免破坏现有 Web 前端
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.message})


# /api/sw/* 里由 FastAPI 抛出的普通 HTTPException（多为认证失败）也统一成错误码；
# 其它路径继续走 FastAPI 默认 {"detail": ...}，Web 界面行为不变。
_SW_STATUS_CODE = {
    400: "VALIDATION", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
    404: "NOT_FOUND", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE",
    500: "INTERNAL", 502: "UPSTREAM_FAILED", 504: "TIMEOUT",
}


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/sw/"):
        return JSONResponse(status_code=exc.status_code, content={
            "error": {
                "code": _SW_STATUS_CODE.get(exc.status_code, "ERROR"),
                "message": str(exc.detail),
                "hint": "",
            }})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """参数校验失败：/api/sw/* 也给出 VALIDATION 错误码。"""
    if request.url.path.startswith("/api/sw/"):
        errors = exc.errors()
        message = "；".join(
            f"{'.'.join(str(x) for x in item.get('loc') or [])}: {item.get('msg')}"
            for item in errors) or "参数校验失败"
        return JSONResponse(status_code=422, content={
            "error": {"code": "VALIDATION", "message": message, "hint": ""}})
    return JSONResponse(status_code=422,
                        content={"detail": jsonable_encoder(exc.errors())})


# ---------- 依赖 ----------
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
    key = _bearer_token(request)
    if key:
        if not key.startswith(_apikey.KEY_PREFIX):
            raise HTTPException(401, "认证失败：不是有效的 API Key")
        u = _apikey.verify(conn, key)
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
    if user["role"] != "admin":
        raise HTTPException(403, "需 admin")
    return user


_ALIAS_CACHE: dict[str, dict[str, list[str]]] = {}


def _cached_project_aliases() -> dict[str, list[str]]:
    """项目别名表（config/project_aliases.yaml），按 ROOT 缓存。

    别名 key 是历史数据里的 huimin / huadu，值是「惠民 / 花都」等中文关键词；
    用它把旧的英文项目键和真实项目目录名互相对应。
    """
    key = str(ROOT)
    if key not in _ALIAS_CACHE:
        try:
            _ALIAS_CACHE[key] = _project_alias_maps()[0]
        except Exception:
            _ALIAS_CACHE[key] = {}
    return _ALIAS_CACHE[key]


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
            short = _project_short(item)
            if raw in (name, short):
                keys.update({name, short})
                break
    except Exception:
        pass
    return {k for k in keys if k}


def _project_access(user, project: str) -> str | None:
    """返回该项目对当前用户的 access（readonly/write）；无权限返回 None。"""
    projects = user.get("projects") or {}
    for key in _project_keys(project):
        access = projects.get(key)
        if access:
            return str(access)
    return None


def _legacy_project_name(value: str) -> str:
    """把历史台账 / 归档回执里的 huimin、huadu 归一到中文项目名。

    只用于读旧数据：user_projects 授权键本身只存项目名或 short，
    不再经过这里的别名兜底。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    # 已经是项目名 / short：直接归一
    try:
        for item in _projects.list_projects():
            name = str(item.get("name") or "")
            if raw in (name, _project_short(item)):
                return name
    except Exception:
        pass
    # 旧别名：config/project_aliases.yaml 里 huimin → 惠民、huadu → 花都
    group = _cached_project_aliases().get(raw) or []
    if group:
        try:
            for item in _projects.list_projects():
                name = str(item.get("name") or "")
                short = _project_short(item)
                if any(word in name or word in short for word in group):
                    return name
        except Exception:
            pass
    return raw


def can_write(user, project: str) -> bool:
    """该项目是否可写。admin 恒 True。"""
    if user.get("role") == "admin":
        return True
    return _project_access(user, project) == "write"


def check_project(user, project: str, *, write: bool = False):
    """项目存在性之外的授权检查：读权限 + （可选）写权限。

    admin 恒通过；其它账号必须在 user_projects 授权范围内。
    readonly 项目调写操作 → 403 READONLY。
    """
    if user.get("role") == "admin":
        return
    access = _project_access(user, project)
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
        if user.get("role") != "admin" and _project_access(user, project) is None:
            raise ApiError(403, "FORBIDDEN", f"无项目 {project} 权限")
        return [project]
    return list((user.get("projects") or {}).keys())

# ---------- 台账 / Cognee / 状态辅助 ----------
COGNEE_BASE = "http://127.0.0.1:8000"
_COGNEE_CACHE: dict[str, tuple[float, list[dict] | None]] = {}
_COGNEE_TTL = 15

def _manifest_file() -> Path:
    return ROOT / "state" / "manifest.yaml"

class ManifestCorrupt(RuntimeError):
    """台账内容无法解析。"""

def _load_manifest() -> dict:
    """读台账。不存在返回空结构；解析失败抛 ManifestCorrupt。"""
    p = _manifest_file()
    if not p.is_file():
        return {"files": {}}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise ManifestCorrupt(str(exc))
    if not isinstance(data, dict):
        raise ManifestCorrupt("台账根节点不是映射")
    data.setdefault("files", {})
    return data

def _save_manifest(manifest: dict) -> None:
    p = _manifest_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)

@contextlib.contextmanager
def _manifest_lock():
    """非阻塞文件锁，拿不到直接报错（不做排队）。"""
    lock = ROOT / "state" / ".manifest.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise HTTPException(409, "台账正被其他进程写入，请稍后重试")
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def _update_manifest(mutator):
    """锁内读-改-写台账。"""
    try:
        with _manifest_lock():
            data = _load_manifest()
            out = mutator(data)
            _save_manifest(data)
            return out
    except ManifestCorrupt as exc:
        raise HTTPException(500, f"台账损坏，请先修复：{exc}")

def _normalize_rel(rel: str) -> str:
    """把台账里的路径归一成相对 ROOT 的 posix 形式；空值返回空串。"""
    rel = str(rel or "").strip().replace("\\", "/")
    if not rel:
        return ""
    p = Path(rel)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            return ""
    return posixpath.normpath(rel).lstrip("./")

def _entry_md_path(e: dict) -> Path | None:
    """台账条目的 md 路径；没有就返回 None。"""
    rel = _normalize_rel(e.get("md") or e.get("md_path") or "")
    return (ROOT / rel) if rel else None

def _normalize_rel(rel: str) -> str:
    """台账路径归一成相对 ROOT 的 posix 形式；空值返回空串。"""
    rel = str(rel or "").strip().replace("\\", "/")
    if not rel:
        return ""
    p = Path(rel)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            return ""
    return posixpath.normpath(rel).lstrip("./")


def _entry_md_path(e: dict) -> Path | None:
    """台账条目的 md 路径；没有就返回 None。"""
    rel = _normalize_rel(e.get("md") or e.get("md_path") or "")
    return (ROOT / rel) if rel else None


def _manifest_entry(token: str) -> dict | None:
    e = (_load_manifest().get("files") or {}).get(token)
    return e if isinstance(e, dict) else None

def _cognee_key() -> str:
    p = ROOT / ".cognee-key"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

def _cognee_get(path: str, key: str, timeout: float = 6.0):
    req = urllib.request.Request(COGNEE_BASE + path, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _dataset_for(project: str) -> str:
    try:
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8")) or {}
        return str((cfg.get("datasets") or {}).get(project, project))
    except Exception:
        return project

def _dataset_entries(dataset: str) -> list[dict] | None:
    """Cognee 数据集条目；取不到返回 None（状态 unknown，不是「没索引」）。"""
    key = _cognee_key()
    if not key:
        return None
    now = time.time()
    cached = _COGNEE_CACHE.get(dataset)
    if cached and now - cached[0] < _COGNEE_TTL:
        return cached[1]
    try:
        datasets = _cognee_get("/api/v1/datasets", key)
        if not isinstance(datasets, list):
            return None
        ds = next((d for d in datasets
                   if isinstance(d, dict) and str(d.get("name")) == dataset), None)
        if not ds or not ds.get("id"):
            return None
        data = _cognee_get(f"/api/v1/datasets/{ds['id']}/data", key)
        if isinstance(data, dict):
            data = data.get("data") or data.get("items") or []
        if not isinstance(data, list):
            return None
        _COGNEE_CACHE[dataset] = (now, data)
        return data
    except Exception:
        return None

def _feishu_template() -> str:
    return status_mod.load_feishu_url_template(ROOT / "feishu-flow.yaml")

def _roster(project: str = "") -> dict[str, str]:
    # 部署根目录是 entities.yaml；仓库里是 config/entities.yaml（模板）
    roster = status_mod.load_roster(ROOT / "entities.yaml")
    if not roster:
        roster = status_mod.load_roster(ROOT / "config" / "entities.yaml")
    if project:
        roster.update(status_mod.load_roster(
            ROOT / "sources" / "projects" / project / "entities.yaml"))
    return roster

ROSTER_TYPES = ("projects", "orgs", "persons", "places")

def _roster_summary(project: str = "") -> dict:
    """名册覆盖度：每类几个名字 + 哪几类还是空的（关系页据此提示补名册）。"""
    roster = _roster(project)
    counts: dict[str, int] = {}
    for kind in roster.values():
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "counts": {k: counts.get(k, 0) for k in ROSTER_TYPES},
        "missing": [k for k in ROSTER_TYPES if not counts.get(k)],
        "path": "entities.yaml",
        "rebuild": "python3 scripts/convert-md.py --relink-only",
    }

_SUGGEST_CACHE: dict[tuple[str, float, int], tuple[str, str]] = {}


def _suggest_project(path: Path) -> tuple[str, str]:
    """按文件名推荐项目：命中 project_aliases.yaml 就返回，判不了返回空。"""
    try:
        st = path.stat()
        key = (str(path), st.st_mtime, st.st_size)
        if key in _SUGGEST_CACHE:
            return _SUGGEST_CACHE[key]
        aliases, labels = _project_alias_maps()
        info = _np.parse(path.name, aliases, labels)
        out = (str(info.get("unit") or ""), "文件名判定" if info.get("unit") else "")
        if len(_SUGGEST_CACHE) > 500:
            _SUGGEST_CACHE.clear()
        _SUGGEST_CACHE[key] = out
        return out
    except Exception:
        return "", ""


def _audit_rows(date: str | None = None) -> list[dict]:
    day = date or datetime.now(TZ).strftime("%Y-%m-%d")
    p = ROOT / "state" / "audit" / f"{day}.jsonl"
    rows: list[dict] = []
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    return rows

# ---------- 登录 ----------
@app.post("/api/login")
def login(request: Request, body: dict, conn=Depends(db)):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "用户名或密码为空")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, password_hash, role, enabled FROM users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
    if not row or not verify_pw(password, row[1]):
        raise HTTPException(401, "用户名或密码错误")
    if not row[3]:
        raise HTTPException(403, "账号已禁用")
    token = create_session(conn, row[0], request.client.host,
                           request.headers.get("user-agent", ""))
    audit(conn, row[0], "login", ip=request.client.host)
    return {"token": token, "username": username, "role": row[2]}

@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"id": user["id"], "username": user["username"],
            "real_name": user.get("real_name", ""),
            "role": user["role"], "projects": user["projects"]}

@app.post("/api/logout")
def logout(request: Request, user=Depends(current_user), conn=Depends(db)):
    """退出登录：服务端删 session，旧 token 立即失效（不是只清前端 localStorage）。"""
    token = request.headers.get("x-token", "")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
    conn.commit()
    audit(conn, user["id"], "logout", ip=request.client.host)
    return {"ok": True}

# ---------- 文档 ----------
@app.get("/api/docs")
def list_docs(project: str = "", category: str = "", q: str = "",
              user=Depends(current_user)):
    scopes = scopes_of(user, project)
    if not scopes:
        return {"total": 0, "items": []}
    manifest = _load_manifest()
    out = []
    for token, e in (manifest.get("files") or {}).items():
        if not isinstance(e, dict) or e.get("status") == "deleted":
            continue
        if _legacy_project_name(str(e.get("project") or "")) not in scopes:
            continue
        if category and e.get("category") != category:
            continue
        if q and q.lower() not in (e.get("name") or "").lower():
            continue
        out.append({
            "token": token,
            "name": e.get("name"),
            "project": e.get("project"),
            "category": e.get("category"),
            "md": e.get("md"),
            "original": e.get("original"),
            "status": e.get("status"),
            "archived_at": e.get("archived_at"),
            "detected_project": e.get("detected_project"),
        })
    return {"total": len(out), "items": out}

@app.get("/api/docs/health")
def docs_health(project: str = "", user=Depends(current_user)):
    """列表用的批量健康状态（source/md/feishu/index），按项目取一次 Cognee。"""
    scopes = scopes_of(user, project)
    if not scopes:
        return {"total": 0, "items": {}}
    manifest = _load_manifest()
    url_tpl = _feishu_template()
    entries_by_ds: dict[str, list[dict] | None] = {}
    for p in scopes:
        ds = _dataset_for(p)
        if ds not in entries_by_ds:
            entries_by_ds[ds] = _dataset_entries(ds)
    out: dict[str, dict] = {}
    for token, e in (manifest.get("files") or {}).items():
        if not isinstance(e, dict) or e.get("status") == "deleted":
            continue
        entry_project = _legacy_project_name(str(e.get("project") or ""))
        if entry_project not in scopes:
            continue
        ds = _dataset_for(entry_project)
        st = status_mod.doc_status(
            ROOT, token, e, entries_by_ds.get(ds), url_tpl,
            roster={},                 # 列表不显示关系，不必读名册
            include_relations=False,
        )
        out[token] = {
            "source": st["source"]["state"],
            "md": st["md"]["state"],
            "index": st["index"]["state"],
        }
    return {"total": len(out), "items": out}

@app.get("/api/docs/{token}")
def get_doc(token: str, user=Depends(current_user)):
    manifest = _load_manifest()
    e = (manifest.get("files") or {}).get(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    if e.get("status") == "deleted":
        raise HTTPException(404, "文档已删除")
    check_project(user, _legacy_project_name(str(e.get("project") or "")))
    content = ""
    md_path = _entry_md_path(e)
    if md_path and md_path.is_file():
        content = md_path.read_text(encoding="utf-8")
    return {"token": token, "name": e.get("name"), "project": e.get("project"),
            "category": e.get("category"), "content": content,
            "original": e.get("original"),
            "detected_project": e.get("detected_project")}
@app.get("/api/docs/{token}/source")
def download_source(token: str, request: Request,
                    user=Depends(current_user), conn=Depends(db)):
    """下载原件。按项目鉴权，逐次写审计。"""
    e = _manifest_entry(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    if e.get("status") == "deleted":
        raise HTTPException(404, "文档已删除")
    check_project(user, _legacy_project_name(str(e.get("project") or "")))
    rel = _normalize_rel(str(e.get("original") or ""))
    if not rel:
        raise HTTPException(404, "台账没有原件路径")
    path = (ROOT / rel).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError:
        raise HTTPException(400, "原件路径越界")
    if not path.is_file():
        raise HTTPException(404, "原件不存在")
    audit(conn, user["id"], "download", e.get("project"), e.get("name"),
          {"path": rel}, request.client.host)
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, filename=e.get("name") or path.name)

def _parse_archive_receipt(stdout: str) -> dict:
    """archive.py --json 在最后一行输出 `__RESULT__ {json}`。"""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("__RESULT__ "):
            try:
                data = json.loads(line[len("__RESULT__ "):])
                return data if isinstance(data, dict) else {}
            except ValueError:
                return {}
    return {}

def _archive_timeout() -> int:
    return int(SETTINGS.tasks["archive_timeout_seconds"])

def _scan_timeout() -> int:
    """archive.py 内部等 scan-inbox 的秒数，比外层短一点，让它先自己收尾。"""
    return max(30, _archive_timeout() - 15)

def run_archive(cmd: list[str], timeout: float) -> tuple[int | None, str, str, bool]:
    """跑 archive.py，返回 (returncode, stdout, stderr, timed_out)。

    超时要杀**整个进程组**：archive.py → scan-inbox.py → reconcile.py/cognify
    是一条链，subprocess.run 的 timeout 只杀第一层，孙子进程会变孤儿继续跑。
    """
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except OSError:
                pass
            try:
                out, err = proc.communicate(timeout=10)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            out, err = proc.communicate()
        return None, out, err, True

def _find_by_sha256(sha: str) -> tuple[str, dict] | None:
    """按内容 hash 找已归档条目（上传幂等用）。"""
    try:
        manifest = _load_manifest()
    except HTTPException:
        return None
    for tok, rec in (manifest.get("files") or {}).items():
        if (isinstance(rec, dict) and rec.get("sha256") == sha
                and rec.get("status") in {"done", "pending", "pending_review"}):
            return tok, rec
    return None

# 图片 / PDF 经 API 上传时必须规范命名，否则拒收（服务端不再替客户端 OCR/识图）
_API_OCR_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".pdf"}

def _project_alias_maps() -> tuple[dict, dict]:
    """项目别名（config/project_aliases.yaml）+ 项目标签（feishu-flow.yaml）。"""
    aliases: dict[str, list[str]] = {}
    p = ROOT / "config" / "project_aliases.yaml"
    if p.is_file():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                aliases = {str(k): [str(w) for w in (v or []) if w]
                           for k, v in data.items() if isinstance(v, list)}
        except Exception:
            aliases = {}
    labels = {str(k): str(v.get("label"))
              for k, v in (_flow().get("projects") or {}).items()
              if isinstance(v, dict) and v.get("label")}
    return aliases, labels

def check_api_upload_name(filename: str) -> str:
    """图片/PDF 命名护栏：缺日期 / 项目名称 / 图片描述任一 → 返回拒收原因。

    只从文件名取信息（name_parse），不读正文、不 OCR、不调视觉模型；
    合规返回空串。文档类（docx/xlsx/md 等）不在此列。
    """
    ext = Path(filename or "").suffix.lower()
    if ext not in _API_OCR_EXTS:
        return ""
    aliases, labels = _project_alias_maps()
    info = _np.parse(filename, aliases, labels)
    missing = [label for key, label in
               (("date", "日期"), ("unit", "项目名称"), ("content", "图片描述"))
               if not info.get(key)]
    if not missing:
        return ""
    return ("文件名不规范（缺少：" + "、".join(missing) + "）。"
            "图片/PDF 经 API 上传必须按「日期-项目名称-图片描述」规范命名"
            "（例：2026-11-05-花都-爱邻议事会第一期活动签到表.jpg）；"
            "请客户端自行 OCR 并规范命名后重新上传，服务端不再对未规范命名的图片/PDF 做 OCR/识图。")

@app.post("/api/docs")
def upload_doc(
    request: Request,
    file: UploadFile = File(...),
    project: str = Form(""),
    category: str = Form(""),
    user=Depends(require_write),
    conn=Depends(db),
):
    """上传一个文件并归档。

    同步返回（客户端要 token），但**不在上传链路里重建索引**：归档只做
    落 inbox → 分类 → 转 md → 写台账 → 飞书归档，索引交给 15 分钟一次的
    reconcile 定时器。以前每传 1 张都跑一次全库对账，HTTP 响应被拖到 200–600 秒。
    端点用普通 def：Starlette 会丢线程池，不会像 async def 里 subprocess.run 那样
    把事件循环占死。

    图片 / PDF 先过命名护栏（check_api_upload_name）：缺日期 / 项目名称 / 图片描述
    一律 400 拒收，让客户端自行 OCR + 规范命名，服务端不再替它 OCR/识图。
    """
    if project:
        # 上传属于写操作：readonly 项目必须拒绝
        check_project(user, project, write=True)
    elif set(user["projects"]) != set(ALL_PROJECTS):
        # 自动判项目会把文件写进用户没权限的项目；没有全部项目权限时必须显式指定
        raise HTTPException(400, "请指定项目（project）：当前账号没有全部项目权限，不能自动判定归属")

    category = (category or "").strip()
    categories = [str(c) for c in (_flow().get("categories") or [])]
    if category and categories and category not in categories:
        raise HTTPException(400, f"未知类别 {category}（可选：{'、'.join(categories)}）")

    filename = Path(file.filename or "upload.bin").name
    name_error = check_api_upload_name(filename)
    if name_error:
        raise HTTPException(400, name_error)
    max_mb = int(SETTINGS.upload.get("max_mb", 50))
    max_bytes = max_mb * 1024 * 1024
    tmpdir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmpdir, filename)
    size = 0
    digest = hashlib.sha256()
    try:
        with open(tmp_path, "wb") as f:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f"文件超过 {max_mb} MB 限制")
                digest.update(chunk)
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "空文件")
        sha = digest.hexdigest()
        # 本地来源的台账 key 由内容 hash 决定，归档前就能算出来，超时也能给客户端一个可查的 token
        token_hint = f"local-{sha[:16]}"

        # 幂等：同一份内容已经归档过 → 直接回同一个 token，不产生第二份档案
        dup = _find_by_sha256(sha)
        if dup:
            dup_token, rec = dup
            audit(conn, user["id"], "upload_duplicate", rec.get("project"), rec.get("name"),
                  {"sha256": sha, "token": dup_token, "size": size},
                  request.client.host)
            return {"ok": True, "token": dup_token, "duplicate": True,
                    "project": rec.get("project"), "category": rec.get("category"),
                    "name": rec.get("name"), "status": rec.get("status"),
                    "md": rec.get("md")}

        # 受理即写审计：之后无论超时、断连还是服务重启，都能查到「谁在什么时候传了什么」
        audit(conn, user["id"], "upload_accepted", project, filename,
              {"sha256": sha, "size": size, "token": token_hint, "category": category},
              request.client.host)

        cmd = ["python3", str(SCRIPTS / "archive.py"), "--file", tmp_path, "--json",
               "--no-cognify", "--only-self", "--scan-timeout", str(_scan_timeout())]
        if project:
            cmd += ["--project", project]
        if category:
            cmd += ["--category", category]
        rc, out, err, timed_out = run_archive(cmd, _archive_timeout())
        receipt = _parse_archive_receipt(out)

        # rc=6 是 archive.py 自己等 scan-inbox 超时（它杀完进程组后返回），
        # 和外层超时是同一件事，都给 202 + 可查询的 token
        if timed_out or rc == 6:
            audit(conn, user["id"], "create", project or receipt.get("project"), token_hint,
                  {"returncode": None, "timeout": True, "sha256": sha, "size": size,
                   "category": category}, request.client.host)
            return JSONResponse(status_code=202, content={
                "ok": False, "accepted": True, "token": token_hint, "status": "unknown",
                "message": ("归档超时，已终止整个进程组。用该 token 查 "
                            "/api/docs/{token}/status；同一内容重复提交不会重复建档"),
            })

        audit(conn, user["id"], "create", project or receipt.get("project"),
              receipt.get("token") or filename,
              {"returncode": rc, "size": size, "sha256": sha,
               "token": receipt.get("token"), "archived_project": receipt.get("project"),
               "category": receipt.get("category")},
              request.client.host)
        if rc != 0:
            raise HTTPException(500, f"归档失败：{(out or err)[-300:]}")
        if not receipt.get("ok"):
            raise HTTPException(500, f"归档结果未通过校验：{(out or err)[-300:]}")

        archived_project = _legacy_project_name(str(receipt.get("project") or ""))
        if archived_project and user.get("role") != "admin" \
                and _project_access(user, archived_project) is None:
            raise HTTPException(403, f"归档到无权限项目 {archived_project}，请联系管理员处理")
        return {"ok": True, **receipt, "output": (out or "")[-800:]}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@app.patch("/api/docs/{token}")
def update_doc(token: str, body: dict, request: Request,
               user=Depends(require_write), conn=Depends(db)):
    manifest = _load_manifest()
    e = (manifest.get("files") or {}).get(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    check_project(user, _legacy_project_name(str(e.get("project") or "")), write=True)
    content = body.get("content")
    if content is None:
        raise HTTPException(400, "缺 content")
    md_path = _entry_md_path(e)
    if not md_path or not md_path.is_file():
        raise HTTPException(400, "该文档无 md，无法修改")
    md_path.write_text(content, encoding="utf-8")
    # 改内容后触发索引重建；失败不阻断保存，但要把结果告诉调用方（不再静默 pass）
    reconcile_rc, reconcile_note = 0, ""
    try:
        proc = subprocess.run(
            ["python3", str(SCRIPTS / "reconcile.py"), "--pg-dsn", PG_DSN, "--no-notify"],
            cwd=str(ROOT), capture_output=True, text=True,
            timeout=int(SETTINGS.tasks["reconcile_timeout_seconds"]))
        reconcile_rc = proc.returncode
        if proc.returncode != 0:
            reconcile_note = (proc.stdout or proc.stderr)[-300:]
    except subprocess.TimeoutExpired:
        reconcile_rc = 124
        reconcile_note = "索引重建超时，15 分钟定时器会补"
    audit(conn, user["id"], "update", e.get("project"), e.get("name"),
          {"md": e.get("md")}, request.client.host)
    return {"ok": True, "reconcile": {"returncode": reconcile_rc,
                                      "warning": reconcile_note}}

@app.delete("/api/docs/{token}")
def delete_doc(token: str, request: Request, user=Depends(require_admin),
               conn=Depends(db), purge: bool = False):
    """删除文档。默认 trash（下架，可恢复）；purge=true 连 sources 原件一起彻底删。"""
    manifest = _load_manifest()
    e = (manifest.get("files") or {}).get(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    # 待确认条目 project 为空（不属于任何项目），admin 端点直接放行；
    # 否则 check_project("") 会因 admin 的 projects 不含空串而 403
    if e.get("project"):
        check_project(user, _legacy_project_name(str(e.get("project") or "")))
    mode = "purge" if purge else "trash"
    try:
        proc = subprocess.run(
            ["python3", str(SCRIPTS / "delete.py"), token, "--mode", mode, "--yes"],
            cwd=str(ROOT), capture_output=True, text=True,
            timeout=int(SETTINGS.tasks["delete_timeout_seconds"]))
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "删除超时（后台可能仍在跑，请稍后查回收站）")
    audit(conn, user["id"], "delete", e.get("project"), e.get("name"),
          {"mode": mode, "returncode": proc.returncode}, request.client.host)
    if proc.returncode != 0:
        raise HTTPException(500, f"删除失败：{proc.stdout[-300:]}")
    return {"ok": True, "mode": mode, "output": proc.stdout[-500:]}

# ---------- 文档操作：关系 / 飞书 / 索引 / 待确认 / 回收站 ----------
def _flow() -> dict:
    p = ROOT / "feishu-flow.yaml"
    if not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def _lark(args: list[str], timeout: int = 300) -> dict:
    """调 lark-cli（与 reconcile / scan-inbox 同一套用法）；失败抛 502。"""
    proc = subprocess.run(
        ["lark-cli"] + args + ["--as", "user", "--format", "json"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    out = proc.stdout or ""
    starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
    if proc.returncode != 0 or not starts:
        raise HTTPException(502, "飞书接口失败：" + (proc.stderr or out).strip()[:300])
    try:
        return json.loads(out[min(starts):])
    except ValueError:
        raise HTTPException(502, "飞书返回无法解析")

def _pending_bases() -> list[Path]:
    return [ROOT / "sources" / "_待确认",
            ROOT / "vault-huadu" / "md" / "_待确认",
            ROOT / "vault-huimin" / "md" / "_待确认"]

def _safe_pending_path(rel: str) -> Path:
    """把请求里的相对路径限制在 `_待确认` 目录内（防路径越界）。"""
    raw = (rel or "").strip()
    if not raw:
        raise HTTPException(400, "缺 path")
    p = (ROOT / raw).resolve()
    for base in _pending_bases():
        try:
            p.relative_to(base.resolve())
            return p
        except ValueError:
            continue
    raise HTTPException(400, "只能操作 _待确认 目录里的文件")

def _trash_file(path: Path) -> str:
    """移到 `sources/.trash/<日期>/<sources 内相对路径>`，返回相对路径。

    ⚠️ 2026-11-09 修：原来只保留文件名 ⇒ `_待确认/其他/a.docx` 丢掉「其他」这层，
       恢复时会被挪到 `sources/a.docx`，等于换了位置。现在保留相对路径，
       `trash_restore` 能按原样放回去。
    """
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        rel = path.resolve().relative_to(ROOT.resolve())
        sub = rel.parts[1:] if rel.parts[:1] == ("sources",) else (path.name,)
    except ValueError:
        sub = (path.name,)
    dest = ROOT / "sources" / ".trash" / day / Path(*sub)
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        n += 1
    shutil.move(str(path), str(dest))
    return dest.relative_to(ROOT).as_posix()

def _run_reconcile(dataset: str, timeout: int) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["python3", str(SCRIPTS / "reconcile.py"), "--dataset", dataset,
             "--pg-dsn", PG_DSN, "--no-notify"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "对账超时（改动已落盘，15 分钟定时器会补）")
    _COGNEE_CACHE.pop(dataset, None)
    return proc
def search_datasets(query: str, datasets: list[str],
                    qtype: str | None = None) -> tuple[bool, str, bool]:
    """按数据集逐个检索并合并。cognee 一次只认一个数据集名。

    qtype 为空时沿用 cognee 的 auto（含日期会走 TEMPORAL，返回合成答案而非素材）；
    生成文书要的是可引用的原文片段，因此固定传 CHUNKS。
    返回 (ok, 合并文本, 是否全部超时)。
    """
    import re
    oks, outs, timeouts = [], [], 0
    for ds in datasets:
        # 选项在前、`--` 后再放 query：避免以 `-` 开头的检索词被 argparse 当成参数
        cmd = ["python3", str(SCRIPTS / "cognee.py"), "search", "--dataset", ds]
        if qtype:
            cmd += ["--type", qtype]
        cmd += ["--", query]
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                                  text=True,
                                  timeout=int(SETTINGS.search["timeout_seconds"]))
        except subprocess.TimeoutExpired:
            timeouts += 1
            continue
        oks.append(proc.returncode == 0)
        text = re.sub(r"<[^>]+>", "", proc.stdout[-int(SETTINGS.search["snippet_chars"]):]).strip()
        if text:
            outs.append(text)
    all_timeout = bool(datasets) and timeouts == len(datasets)
    return (any(oks) if oks else True), "\n\n".join(outs), all_timeout


@app.get("/api/search")
def search(q: str, project: str = "", user=Depends(current_user)):
    datasets = scopes_of(user, project)
    if not datasets:
        raise HTTPException(403, "当前账号没有可检索的项目")
    ok, out, all_timeout = search_datasets(q, datasets)
    if all_timeout:
        # BUG-05：检索超时按无结果返回，不拖死调用方
        return {"ok": True, "output": "", "note": "检索超时，已按无结果返回"}
    return {"ok": ok, "output": out, "datasets": datasets}

@app.post("/api/generate")
def generate(body: dict, request: Request, user=Depends(current_user),
             conn=Depends(db)):
    doc_type = body.get("type") or ""
    context = body.get("context") or ""
    tpl_key = TYPE_MAP.get(doc_type)
    if not tpl_key:
        raise HTTPException(400, f"不支持的文书类型：{doc_type}")
    tpl_path = TEMPLATES_DIR / f"{tpl_key}.md"
    if not tpl_path.is_file():
        raise HTTPException(500, f"模板缺失：{tpl_path}")
    template = tpl_path.read_text(encoding="utf-8")

    # 检索素材：显式 project 优先，否则合并全部授权项目；零项目直接拒绝
    project = (body.get("project") or "").strip()
    datasets = scopes_of(user, project)
    if not datasets:
        raise HTTPException(403, "当前账号没有可用的项目")
    search_out = ""
    if context:
        _, search_out, _ = search_datasets(context, datasets, qtype="CHUNKS")
        search_out = search_out[-int(SETTINGS.search["material_chars"]):]

    prompt = f"""你是社工文书生成助手。根据以下模板和素材，生成一份{doc_type}。

## 模板
{template}

## 素材（知识库检索结果，引用时标注来源）
{search_out or "（无检索素材）"}

## 用户要求
{context or "（无额外要求）"}

要求：
- 严格按模板结构填充
- 引用素材中的政策与案例，标注来源
- 不编造政策文号、统计数据、案例细节
- 正文 ≤14,000 字
- 直接输出文书正文，不要额外说明"""

    import traceback
    try:
        content, finish = llm_chat(prompt)
    except (TimeoutError, socket.timeout) as exc:
        traceback.print_exc()
        raise HTTPException(504, f"生成超时（LLM 未在限时内返回）：{exc}")
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(500, f"生成失败：{exc}")
    audit(conn, user["id"], "generate", target=doc_type,
          detail={"context": context[:200], "project": project,
                  "finish_reason": finish},
          ip=request.client.host)
    return {"ok": True, "content": content, "truncated": finish == "length",
            "finish_reason": finish}
@app.get("/api/admin/users")
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

def _valid_role(role: str) -> str:
    if role not in ("admin", "editor", "viewer"):
        raise HTTPException(400, "角色非法")
    return role

def _as_bool(value) -> bool:
    """严格布尔解析：字符串 "false" 不能当 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise HTTPException(400, "enabled 必须是布尔值")

_PROJECT_ACCESS = ("readonly", "write")

def _canonical_project(name: str) -> str:
    """项目授权统一存中文项目名（目录名）；传 short 时尽量归一到目录名。"""
    raw = str(name or "").strip()
    if not raw:
        return ""
    try:
        for item in _projects.list_projects():
            pname = str(item.get("name") or "")
            if raw in (pname, _project_short(item)):
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

@app.post("/api/admin/users")
def admin_create_user(body: dict, request: Request, conn=Depends(db),
                      user=Depends(require_admin)):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    real_name = str(body.get("real_name") or "").strip()
    role = _valid_role(body.get("role") or "viewer")
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

@app.patch("/api/admin/users/{uid}")
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
            cur.execute("UPDATE users SET role=%s WHERE id=%s",
                        (_valid_role(body["role"]), uid))
        if "enabled" in body:
            cur.execute("UPDATE users SET enabled=%s WHERE id=%s",
                        (_as_bool(body["enabled"]), uid))
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

@app.delete("/api/admin/users/{uid}")
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

@app.delete("/api/admin/users/{uid}/sessions")
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

@app.get("/api/admin/audit")
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

# ---------- 项目/活动 ----------
def _project_short(item: dict) -> str:
    """项目 yaml 的 short（= 目录名）；没配 short 时退回目录名。"""
    data = item.get("data") or {}
    proj = data.get("project") if isinstance(data, dict) else None
    if not isinstance(proj, dict):
        return str(item.get("name") or "")
    return str(proj.get("short") or item.get("name") or "")


def _project_allowed(user, item: dict) -> bool:
    """admin 看全部；其余账号按 user_projects 授权（项目名 / short 均可命中）。"""
    if user["role"] == "admin":
        return True
    return _project_access(user, str(item.get("name") or "")) is not None


@app.get("/api/projects")
def api_list_projects(user=Depends(current_user)):
    """列全部项目。admin 看到全部；editor/viewer 按授权项目过滤。"""
    items = [p for p in _projects.list_projects() if _project_allowed(user, p)]
    return {"total": len(items), "items": items}


@app.get("/api/projects/{name}")
def api_get_project(name: str, user=Depends(current_user)):
    """单个项目主数据。没配置 yaml 的项目按 404 处理。"""
    item = next((p for p in _projects.list_projects() if p["name"] == name), None)
    if item is None or not item.get("has_config"):
        raise HTTPException(404, f"项目不存在：{name}")
    if not _project_allowed(user, item):
        raise HTTPException(403, f"无项目 {name} 权限")
    return item


@app.get("/api/projects/{name}/activities")
def api_list_activities(name: str, user=Depends(current_user)):
    """活动清单：计划拆出来的 / 磁盘已有的 / 缺的 / 多出来的。"""
    try:
        data = _projects.load_project(name)
    except FileNotFoundError:
        raise HTTPException(404, f"项目不存在：{name}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if user["role"] != "admin" and _project_access(user, name) is None:
        raise HTTPException(403, f"无项目 {name} 权限")

    planned = _activities.plan_activities(data)
    existing = _activities.list_existing_activities(Path(_projects.PROJECTS_ROOT) / name)
    planned_names = [a["dirname"] for a in planned]
    existing_set = set(existing)
    planned_set = set(planned_names)
    return {
        "planned": planned,
        "existing": existing,
        "missing": [d for d in planned_names if d not in existing_set],
        "extra": [d for d in existing if d not in planned_set],
    }

# ---------- engine P2：编号生成 / 活动目录 ----------
def _project_meta_or_404(name: str) -> dict:
    """取项目元数据：目录 / yaml 缺失按 404，yaml 损坏按 400。"""
    try:
        meta = _projects.project_meta(name)
    except FileNotFoundError:
        raise HTTPException(404, f"项目不存在：{name}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not meta["has_config"]:
        raise HTTPException(404, f"项目没有 yaml：{name}")
    return meta


def _require_project_write(user, meta: dict) -> None:
    """写权限：admin/editor；非 admin 还要求该项目 access=write。"""
    if user["role"] not in ("admin", "editor"):
        raise HTTPException(403, "无写权限（需 editor/admin）")
    # 同时认目录名与 short，历史授权数据两种写法都能命中
    check_project(user, meta["name"], write=True)


def _existing_codes(meta: dict) -> list[str]:
    """本项目已有编号：project.code + 以编号命名的活动目录（解析不了的自动跳过）。"""
    codes = [meta["code"]] if meta.get("code") else []
    project_dir = Path(_projects.PROJECTS_ROOT) / meta["name"]
    codes.extend(_activities.activity_dirs(project_dir).keys())
    return codes


def _project_year(name: str) -> int:
    """立项年份取 project.period.start 的年份，缺失时退回当前年。"""
    try:
        data = _projects.load_project(name)
    except (FileNotFoundError, ValueError):
        return datetime.now(TZ).year
    period = data.get("period") if isinstance(data.get("period"), dict) else {}
    start = str(period.get("start") or "")
    if len(start) >= 4 and start[:4].isdigit():
        return int(start[:4])
    return datetime.now(TZ).year


def _act_year(act_month: int, act_year: int | None = None) -> int:
    """活动年：显式传了就用，否则按活动月与项目周期推断当前年。

    简单规则：不传时取当前年份。跨年活动由调用方显式传 act_year。
    """
    if act_year:
        return int(act_year)
    return datetime.now(TZ).year


@app.get("/api/projects/{name}/code")
def api_project_code(name: str, act_type: int, act_month: int,
                     act_year: int = 0, serial: int = 0,
                     user=Depends(current_user)):
    """生成一个项目编号。act_year=0 表示取当前年；serial=0 表示自动取下一个流水号。"""
    meta = _project_meta_or_404(name)
    if user["role"] != "admin" and meta["short"] not in scopes_of(user):
        raise HTTPException(403, f"无项目 {name} 权限")
    establish_month = meta["establish_month"]
    if not establish_month:
        raise HTTPException(400, "项目没有配置 code_rules.establish_month")
    act_types = meta["act_types"]
    if act_types and act_type not in set(act_types.values()):
        raise HTTPException(400, f"活动类型号 {act_type} 不在项目配置里")
    year = _project_year(name)
    act_year = _act_year(act_month, act_year or None)
    if not serial:
        serial = _coding.next_serial(_existing_codes(meta), act_type=act_type,
                                     year=year, act_year=act_year)
    try:
        code = _coding.make_code(year=year, establish_month=establish_month,
                                 act_type=act_type, act_month=act_month,
                                 serial=serial, act_year=act_year)
        parsed = _coding.parse_code(code)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"code": code, "parsed": parsed}


@app.post("/api/projects/{name}/activities/split")
def api_split_activities(name: str, body: dict, user=Depends(current_user)):
    """按 indicators 建活动目录。body: {"dry_run": true/false}，默认只预演。"""
    meta = _project_meta_or_404(name)
    _require_project_write(user, meta)
    try:
        data = _projects.load_project(name)
    except FileNotFoundError:
        raise HTTPException(404, f"项目不存在：{name}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    planned = _activities.plan_activities(data)
    planned_names = [a["dirname"] for a in planned]
    project_dir = Path(_projects.PROJECTS_ROOT) / name

    if bool(body.get("dry_run", True)):
        existing = set(_activities.list_existing_activities(project_dir))
        return {
            "dry_run": True,
            "would_create": [d for d in planned_names if d not in existing],
            "would_skip": [d for d in planned_names if d in existing],
        }

    created = _activities.create_activity_dirs(project_dir, planned)
    created_set = set(created)
    return {
        "dry_run": False,
        "created": created,
        "skipped": [d for d in planned_names if d not in created_set],
    }


@app.patch("/api/projects/{name}/activities/{dirname}")
def api_rename_activity(name: str, dirname: str, body: dict,
                        user=Depends(current_user)):
    """重命名活动目录。body: {"new_name": "..."}。"""
    meta = _project_meta_or_404(name)
    _require_project_write(user, meta)
    new_name = str(body.get("new_name") or "").strip()
    if not new_name:
        raise HTTPException(400, "new_name 不能为空")
    project_dir = Path(_projects.PROJECTS_ROOT) / name
    try:
        _activities.rename_activity_dir(project_dir, dirname, new_name)
    except FileNotFoundError:
        raise HTTPException(404, f"活动目录不存在：{dirname}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "old": dirname, "new": new_name}

@app.get("/api/projects/{name}/activities/{dirname}/check")
def api_check_activity(name: str, dirname: str, user=Depends(current_user)):
    """15 项核对。返回 check() 的结果。权限同其它 activity 端点。"""
    try:
        data = _projects.load_project(name)
    except FileNotFoundError:
        raise HTTPException(404, f"项目不存在：{name}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if user["role"] != "admin" and _project_access(user, name) is None:
        raise HTTPException(403, f"无项目 {name} 权限")
    try:
        act_dir = _projects.activity_dir(name, dirname)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not act_dir.is_dir():
        raise HTTPException(404, f"活动目录不存在：{dirname}")
    return _checks.check(act_dir)


@app.post("/api/projects/{name}/activities/{dirname}/readme")
def api_regen_readme(name: str, dirname: str, user=Depends(require_write)):
    """手动重生成单个活动的 README。内容没变时 written=False。"""
    require_write(user)
    _activity_meta_or_404(name, dirname, user, write=True)
    try:
        written = _readme.write_activity_readme(name, dirname)
    except FileNotFoundError:
        raise HTTPException(404, f"活动目录不存在：{dirname}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "written": written,
            "path": _readme.readme_filename(dirname=dirname)}


@app.post("/api/readme/refresh")
def api_refresh_all_readme(user=Depends(require_admin)):
    """全量刷新 README。只读目录 + 写 md，同步执行即可。"""
    require_admin(user)
    result = _readme.refresh_all()
    return {"written": len(result["written"]), "skipped": result["skipped"]}


@app.get("/api/projects/{name}/check")
def api_check_project(name: str, user=Depends(current_user)):
    """项目级 19 项核对：依据《项目管理目录.docx》，扫四个资料子目录。"""
    meta = _project_meta_or_404(name)
    if user["role"] != "admin" and meta["short"] not in scopes_of(user):
        raise HTTPException(403, f"无项目 {name} 权限")
    return _project_checks.check_project(Path(_projects.PROJECTS_ROOT) / name)


@app.patch("/api/projects/{name}/activities/{dirname}/check")
def api_set_na(name: str, dirname: str, body: dict,
               user=Depends(require_write)):
    """标记活动核对项不适用。body: {"not_applicable": [15, 3]}。

    只改 activity.yaml 的 not_applicable 字段，其余字段原样保留。
    """
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)
    raw = body.get("not_applicable") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        raise HTTPException(400, "not_applicable 必须是数组")

    nos: set[int] = set()
    for value in raw:
        if isinstance(value, bool):
            raise HTTPException(400, f"核对序号非法：{value}")
        try:
            no = int(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"核对序号非法：{value}")
        if no < 1 or no > 15:
            raise HTTPException(400, f"核对序号超出范围 1-15：{no}")
        nos.add(no)

    data = _ingest.read_activity_yaml(act_dir)
    data["not_applicable"] = sorted(nos)
    path = act_dir / _ingest.ACTIVITY_YAML
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "not_applicable": sorted(nos)}



# ---------- Web 管理界面：项目管理 / 活动文件 / 材料生成 / 同步 / 仪表盘 ----------
def _load_script(name: str):
    """按仓库 scripts/ 的路径加载脚本模块。

    部署根的 scripts/ 可能还是旧版（例如 sync_feishu 没有 ensure/push），
    新端点要用仓库里的实现，所以显式按文件路径加载，避免被 sys.path 抢先。
    """
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_sw_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_feishu_sync(project: str) -> list[dict]:
    """后台任务：飞书同步。单独成函数，测试可 monkeypatch。"""
    return _load_script("sync_feishu").sync(project)


def _run_cognee_index(project: str) -> dict:
    """后台任务：Cognee 入库。单独成函数，测试可 monkeypatch。"""
    return _load_script("index_cognee").run(ROOT, project)


_SCRIPT_CACHE: dict[str, object] = {}


def _script(name: str):
    """加载并缓存 scripts/<name>.py，同一进程内只 exec 一次。"""
    module = _SCRIPT_CACHE.get(name)
    if module is None:
        module = _load_script(name)
        _SCRIPT_CACHE[name] = module
    return module


def _template_library() -> Path:
    """模板库目录：<ROOT>/templates/library（唯一模板来源）。"""
    return ROOT / "templates" / "library"


def _find_template(name: str) -> Path:
    """按模板名（不含扩展名）在模板库里找 docx/xlsx。

    名字非法（带路径分隔符 / 通配符 / ..）→ 400；找不到 → 404。
    """
    raw = str(name or "").strip()
    if (not raw or "/" in raw or "\\" in raw or ".." in raw
            or any(ch in raw for ch in "*?[]")):
        raise HTTPException(400, f"模板名非法：{raw}")
    lib = _template_library()
    hits = sorted(p for p in lib.glob(f"{raw}.*")
                  if p.is_file() and p.suffix.lower() in (".docx", ".xlsx"))
    if not hits:
        raise HTTPException(404, f"模板不存在：{raw}")
    return hits[0]


def _activity_meta_or_404(name: str, dirname: str, user, write: bool = False):
    """定位活动目录并校验权限，返回 (活动目录, 项目元数据)。

    读：admin 或项目在授权范围内；写：admin/editor 且项目在授权范围内。
    活动目录不存在 → 404，目录名非法 → 400。
    """
    meta = _project_meta_or_404(name)
    if write:
        _require_project_write(user, meta)
    elif user["role"] != "admin" and meta["short"] not in scopes_of(user):
        raise HTTPException(403, f"无项目 {name} 权限")
    try:
        act_dir = _projects.activity_dir(name, dirname)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not act_dir.is_dir():
        raise HTTPException(404, f"活动目录不存在：{dirname}")
    return act_dir, meta


def _safe_rel_path(base: Path, rel: str, *, allow_empty: bool = False) -> Path:
    """把请求里的相对路径限制在 base 目录内。

    先拒绝绝对路径与任何 `..` 段，再归一化并做 resolve 越界检查；
    这样 `../`、`a/../../b`、绝对路径都回 400，不会被 normpath 悄悄抹平。
    """
    raw = str(rel or "").strip().replace("\\", "/")
    if not raw:
        if allow_empty:
            return base.resolve()
        raise HTTPException(400, "缺 path")
    if raw.startswith("/"):
        raise HTTPException(400, "路径必须是相对路径")
    if any(seg == ".." for seg in raw.split("/")):
        raise HTTPException(400, "路径不能包含 ..")
    norm = _normalize_rel(raw)
    if not norm:
        raise HTTPException(400, "路径非法")
    path = (base / norm).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(400, "路径越界")
    return path


def _activity_yaml_data(act_dir: Path) -> dict:
    """读 activity.yaml；解析失败按空 dict 处理，详情页不因此 500。"""
    try:
        return _ingest.read_activity_yaml(act_dir)
    except ValueError:
        return {}


def _count_files(directory: Path) -> int:
    """目录下（递归）文件数，跳过隐藏目录（.trash 等）。"""
    total = 0
    for p in directory.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(directory)
        if any(part.startswith(".") for part in rel.parts):
            continue
        total += 1
    return total


def _file_item(path: Path) -> dict:
    """文件/目录条目：name / size / mtime / is_dir。"""
    st = path.stat()
    return {
        "name": path.name,
        "size": 0 if path.is_dir() else st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, TZ).isoformat(),
        "is_dir": path.is_dir(),
    }


def _activity_mtime(act_dir: Path) -> float:
    """活动目录的最近改动时间：取目录本身与所有文件 mtime 的最大值。"""
    latest = act_dir.stat().st_mtime
    for p in act_dir.rglob("*"):
        if p.is_file():
            latest = max(latest, p.stat().st_mtime)
    return latest


@app.post("/api/projects")
def api_create_project(body: dict, request: Request,
                       user=Depends(require_admin), conn=Depends(db)):
    """建项目：目录 + 方案/协议/报告/活动/其他 + 最小 yaml。

    body: {name, short, code_rules?}。目录已存在 → 409。
    """
    require_admin(user)
    name = str(body.get("name") or "").strip()
    try:
        target = _projects._project_dir(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if target.exists():
        raise HTTPException(409, f"项目目录已存在：{name}")

    short = str(body.get("short") or name).strip() or name
    rules = body.get("code_rules") or {}
    if not isinstance(rules, dict):
        raise HTTPException(400, "code_rules 必须是对象")

    target.mkdir(parents=True)
    for sub in ("方案", "协议", "报告", "活动", "其他"):
        (target / sub).mkdir()

    yaml_path = target / f"{name}.yaml"
    text = yaml.safe_dump({"project": {"short": short, "code_rules": rules}},
                          allow_unicode=True, sort_keys=False)
    tmp = yaml_path.with_name(yaml_path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(yaml_path)

    audit(conn, user["id"], "create_project", target=name,
          detail={"short": short, "code_rules": rules}, ip=request.client.host)
    return {"ok": True, "name": name, "short": short,
            "path": str(target), "yaml": str(yaml_path)}


@app.put("/api/projects/{name}")
def api_update_project(name: str, body: dict, request: Request,
                       user=Depends(require_admin), conn=Depends(db)):
    """更新项目 yaml：body 是完整 yaml dict，原子写。

    用 safe_dump 整体覆盖，**不保留原文件注释与字段顺序**；
    需要留注释的改动请直接改磁盘上的 yaml。
    """
    require_admin(user)
    try:
        target = _projects._project_dir(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    yaml_path = _projects._yaml_in(target)
    if yaml_path is None:
        raise HTTPException(404, f"项目没有 yaml：{name}")
    if not isinstance(body, dict):
        raise HTTPException(400, "body 必须是 yaml 对象")

    text = yaml.safe_dump(body, allow_unicode=True, sort_keys=False)
    tmp = yaml_path.with_name(yaml_path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(yaml_path)

    audit(conn, user["id"], "update_project", target=name,
          detail={"keys": sorted(body)}, ip=request.client.host)
    return {"ok": True, "name": name, "yaml": str(yaml_path)}


@app.get("/api/projects/{name}/activities/{dirname}")
def api_activity_detail(name: str, dirname: str, user=Depends(current_user)):
    """活动详情：编号 / 状态 / 顶层文件 / 子目录文件数 / 15 项核对。"""
    act_dir, _meta = _activity_meta_or_404(name, dirname, user)
    act = _activity_yaml_data(act_dir)

    files: list[dict] = []
    dirs: list[dict] = []
    for p in sorted(act_dir.iterdir(), key=lambda x: x.name):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            dirs.append({"name": p.name, "count": _count_files(p)})
        elif p.is_file():
            files.append(_file_item(p))

    return {
        "dirname": dirname,
        "code": str(act.get("code") or ""),
        "status": str(act.get("status") or ""),
        "files": files,
        "dirs": dirs,
        "check": _checks.check(act_dir),
    }


@app.get("/api/projects/{name}/activities/{dirname}/files")
def api_activity_files(name: str, dirname: str, sub: str = "",
                       user=Depends(current_user)):
    """列活动目录（或 sub 子目录）下的一级条目，按名称排序。"""
    act_dir, _meta = _activity_meta_or_404(name, dirname, user)
    base = _safe_rel_path(act_dir, sub, allow_empty=True)
    if not base.is_dir():
        raise HTTPException(404, f"目录不存在：{sub}")
    return [_file_item(p) for p in sorted(base.iterdir(), key=lambda x: x.name)
            if not p.name.startswith(".")]


# 活动目录上传：允许的扩展名（与文档库一致，另加压缩包）
_ACTIVITY_UPLOAD_EXTS = {
    ".docx", ".xlsx", ".doc", ".xls", ".pdf",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".txt", ".md", ".zip", ".7z",
}
# 文件名里不允许出现的字符（路径分隔符与 Windows 保留字符）
_ACTIVITY_BAD_NAME_CHARS = set('/\\:*?"<>|')
_ACTIVITY_NAME_MAX = 120


def _activity_sub_dir(act_dir: Path, sub: str) -> Path:
    """把 sub 解析成活动目录内的目标子目录，逐级校验必须已存在。

    sub 为空表示活动根目录；多级用 `/` 分隔（如 13-其他佐证/知识库规范命名照片）。
    含 `..`、`.`、空段、反斜杠、绝对路径、空字节一律 400；任一级不存在也 400。
    """
    raw = str(sub or "").strip()
    base = act_dir.resolve()
    if not raw:
        return base
    if "\x00" in raw:
        raise HTTPException(400, "子目录路径含空字节")
    if "\\" in raw:
        raise HTTPException(400, "子目录路径不能包含反斜杠")
    if raw.startswith("/") or Path(raw).is_absolute():
        raise HTTPException(400, "子目录必须是相对路径")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise HTTPException(400, "子目录路径非法（不能包含 ..、. 或空段）")
    cur = act_dir
    for part in parts:
        cur = cur / part
        if not cur.is_dir():
            raise HTTPException(400, f"子目录不存在：{part}")
    path = cur.resolve()
    try:
        path.relative_to(base)
    except ValueError:
        raise HTTPException(400, "子目录越界")
    return path


def _activity_upload_filename(filename: str) -> str:
    """校验上传文件名：扩展名、长度、隐藏文件、非法字符。返回原文件名。"""
    raw = str(filename or "")
    if not raw.strip():
        raise HTTPException(400, "缺文件名")
    if raw.startswith("."):
        raise HTTPException(400, "不允许上传隐藏文件（文件名不能以 . 开头）")
    if len(raw) > _ACTIVITY_NAME_MAX:
        raise HTTPException(400, f"文件名超过 {_ACTIVITY_NAME_MAX} 字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise HTTPException(400, "文件名含控制字符")
    if any(ch in _ACTIVITY_BAD_NAME_CHARS for ch in raw):
        raise HTTPException(400, '文件名不能包含 / \\ : * ? " < > |')
    ext = Path(raw).suffix.lower()
    if ext not in _ACTIVITY_UPLOAD_EXTS:
        raise HTTPException(400, f"不允许的扩展名：{ext or '（无扩展名）'}")
    return raw


def _check_activity_code_owner(name: str, act_dir: Path) -> None:
    """activity.yaml 有 code 时，编号前缀必须属于本项目。

    前缀 = CDYJR + 立项年 + 三位立项月（如花都 CDYJR2026005）。
    期望值优先取项目 yaml 的 code_rules；但实际数据里 yaml 的立项月可能与
    既有编号不一致（惠民 yaml 写 6、既有活动编号却是 CDYJR2026008），
    因此再并入「同项目其他活动」的既有前缀作为事实来源。
    两边都对不上，说明活动目录是从别的项目混进来的，直接 400。
    """
    code = str(_activity_yaml_data(act_dir).get("code") or "").strip()
    if not code:
        return
    m = re.match(r"^([A-Za-z0-9]+)-[cC]-", code)
    if not m:
        raise HTTPException(400, f"activity.yaml 的 code 格式无法识别：{code}")
    actual = m.group(1).upper()

    expected: set[str] = set()
    month = _projects.project_meta(name).get("establish_month")
    if month:
        expected.add(f"{_coding.ORG_PREFIX}{_project_year(name)}{int(month):03d}".upper())
    project_dir = Path(_projects.PROJECTS_ROOT) / name
    for other_name, other_dir in _activities.activity_dirs(project_dir).items():
        if other_name == act_dir.name:
            continue
        other_code = str(_activity_yaml_data(other_dir).get("code") or "").strip()
        om = re.match(r"^([A-Za-z0-9]+)-[cC]-", other_code)
        if om:
            expected.add(om.group(1).upper())

    if not expected:
        # 项目没配立项月、也没有其他活动可参照，无从判断，不阻断
        return
    if actual not in expected:
        raise HTTPException(
            400, f"活动编号不属于本项目：期望前缀 {'/'.join(sorted(expected))}，"
                 f"实际 {actual}（{code}）")


def _activity_numbering_warning(act_dir: Path, filename: str) -> str:
    """文件名以 NN- 开头且对应核对项已 ok 时，返回重复提示（不阻断上传）。"""
    m = re.match(r"^(\d{2})-", filename)
    if not m:
        return ""
    prefix = m.group(1) + "-"
    names = [(item["no"], item["name"]) for item in _checks.CHECKLIST
             if prefix in (item.get("prefixes") or [item.get("prefix")])]
    if not names:
        return ""
    status = {it["no"]: it["status"] for it in _checks.check(act_dir).get("items") or []}
    hit = next((label for no, label in names if status.get(no) == "ok"), "")
    if not hit:
        return ""
    return f"核对项「{hit}」已齐，再上传 {prefix} 开头的文件会重复"


@app.post("/api/projects/{name}/activities/{dirname}/upload")
def api_activity_upload(
    name: str, dirname: str,
    file: UploadFile = File(...),
    sub: str = Form(""),            # 目标子目录（空 = 活动根）
    user=Depends(require_write),
    conn=Depends(db),
):
    """往活动目录上传文件。返回 {path, size, sha256}。

    校验链：写权限 → 项目/活动目录存在 → sub 逐级存在 → 文件名规范 →
    编号归属 → 同名冲突 → 流式落盘并限大小。文件名以 NN- 开头且该核对项
    已齐时，仍返回 200，但附带 warning 提醒会重复。
    """
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)
    target_dir = _activity_sub_dir(act_dir, sub)
    filename = _activity_upload_filename(file.filename or "")
    _check_activity_code_owner(name, act_dir)
    # 先按上传前的磁盘状态判断是否重复，落盘后该前缀必然已齐
    warning = _activity_numbering_warning(act_dir, filename)

    dest = target_dir / filename
    if dest.exists():
        raise HTTPException(409, f"同名文件已存在：{filename}（请改名或先删除）")

    max_mb = int(SETTINGS.upload.get("max_mb", 50))
    max_bytes = max_mb * 1024 * 1024
    digest = hashlib.sha256()
    size = 0
    try:
        # "xb" 独占创建：并发同名上传时只有一个能成功，其余走 409
        with open(dest, "xb") as fh:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f"文件超过 {max_mb} MB 限制")
                digest.update(chunk)
                fh.write(chunk)
    except FileExistsError:
        raise HTTPException(409, f"同名文件已存在：{filename}（请改名或先删除）")
    except HTTPException:
        dest.unlink(missing_ok=True)      # 超限 / 中断不留半截文件
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    sha = digest.hexdigest()
    rel = dest.relative_to(act_dir.resolve()).as_posix()
    audit(conn, user["id"], "upload_activity_file", project=name, target=rel,
          detail={"activity": dirname, "size": size, "sha256": sha})
    out = {"path": rel, "size": size, "sha256": sha}
    if warning:
        out["warning"] = warning
    return out


@app.get("/api/projects/{name}/activities/{dirname}/download")
def api_activity_download(name: str, dirname: str, path: str,
                          user=Depends(current_user)):
    """下载活动内文件。path 相对活动目录，越界 → 400。"""
    act_dir, _meta = _activity_meta_or_404(name, dirname, user)
    target = _safe_rel_path(act_dir, path)
    if not target.is_file():
        raise HTTPException(404, f"文件不存在：{path}")
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, filename=target.name)


@app.delete("/api/projects/{name}/activities/{dirname}/files")
def api_activity_delete_file(name: str, dirname: str, body: dict, request: Request,
                             user=Depends(require_write), conn=Depends(db)):
    """删除活动内文件：移到 <活动目录>/.trash/<时间戳>/ 下，不真删。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)
    target = _safe_rel_path(act_dir, body.get("path"))
    if not target.is_file():
        raise HTTPException(404, f"文件不存在：{body.get('path')}")

    rel = target.relative_to(act_dir.resolve())
    if rel.parts and rel.parts[0] == ".trash":
        raise HTTPException(400, "不能删除回收站里的文件")

    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    dest = act_dir / ".trash" / stamp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        n += 1
    shutil.move(str(target), str(dest))

    audit(conn, user["id"], "delete_activity_file", project=name,
          target=rel.as_posix(),
          detail={"activity": dirname, "trash": dest.relative_to(act_dir).as_posix()},
          ip=request.client.host)
    return {"ok": True, "path": rel.as_posix(),
            "trash": dest.relative_to(act_dir).as_posix()}


@app.get("/api/templates")
def api_templates(user=Depends(current_user)):
    """列模板库里的 docx/xlsx 及占位符。"""
    lib = _template_library()
    out: list[dict] = []
    if not lib.is_dir():
        return out
    for p in sorted(lib.iterdir(), key=lambda x: x.name):
        if not p.is_file() or p.suffix.lower() not in (".docx", ".xlsx"):
            continue
        try:
            placeholders = _fill.scan_placeholders(p)
        except Exception:
            # 单个模板损坏不该让整个列表 500
            placeholders = []
        out.append({"name": p.stem,
                    "kind": p.suffix.lower().lstrip("."),
                    "placeholders": placeholders})
    return out


def _activity_require_code(act_dir: Path) -> None:
    """模板填充前置条件：activity.yaml 必须有 code。"""
    code = str(_activity_yaml_data(act_dir).get("code") or "").strip()
    if not code:
        raise HTTPException(400, "activity.yaml 缺 code，无法生成材料")


def _fill_output(act_dir: Path, body: dict, tpl: Path) -> Path:
    """解析输出路径：默认「模板名.扩展名」，限制在活动目录内。"""
    default = f"{tpl.stem}{tpl.suffix}"
    out_rel = str(body.get("output") or default).strip()
    dst = _safe_rel_path(act_dir, out_rel)
    if not dst.suffix:
        dst = dst.with_name(dst.name + tpl.suffix)
    return dst


@app.post("/api/projects/{name}/activities/{dirname}/fill")
def api_activity_fill(name: str, dirname: str, body: dict, request: Request,
                      user=Depends(require_write), conn=Depends(db)):
    """模板填充：build_data → 合并 data → render → 写到活动目录。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)
    _activity_require_code(act_dir)

    tpl = _find_template(body.get("template"))
    extra = body.get("data") or {}
    if not isinstance(extra, dict):
        raise HTTPException(400, "data 必须是对象")
    dst = _fill_output(act_dir, body, tpl)

    merged = _activity.build_data(name, dirname, extra=extra, user=user)
    try:
        result = _fill.render(tpl, dst, merged)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    rel = dst.relative_to(act_dir.resolve()).as_posix()
    audit(conn, user["id"], "fill_template", project=name, target=rel,
          detail={"activity": dirname, "template": tpl.stem,
                  "filled": result["filled"]}, ip=request.client.host)
    return {"path": rel, "filled": result["filled"],
            "missing": result["missing"]}


@app.post("/api/projects/{name}/activities/{dirname}/llm")
def api_activity_llm(name: str, dirname: str, body: dict, request: Request,
                     user=Depends(require_write), conn=Depends(db)):
    """LLM 生成：提取 facts → llm.write()，结果直接返回，不落盘。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)

    task = str(body.get("task") or "").strip()
    if not task:
        raise HTTPException(400, "缺 task")
    style = str(body.get("style") or "")
    facts = _activity.build_data(name, dirname, user=user)
    try:
        result = _llm.write(task, facts, style=style)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except _llm.LLMError as exc:
        raise HTTPException(502, f"LLM 生成失败：{exc}")

    audit(conn, user["id"], "llm_generate", project=name, target=task,
          detail={"activity": dirname, "style": style}, ip=request.client.host)
    return {"task": task, "style": style, "result": result}


@app.post("/api/projects/{name}/activities/{dirname}/llm-fill")
def api_activity_llm_fill(name: str, dirname: str, body: dict, request: Request,
                          user=Depends(require_write), conn=Depends(db)):
    """LLM 生成 + 落盘：llm.write() → 合并进 build_data() → fill.render()。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)

    task = str(body.get("task") or "").strip()
    if not task:
        raise HTTPException(400, "缺 task")
    tpl = _find_template(body.get("template"))
    style = str(body.get("style") or "")
    dst = _fill_output(act_dir, body, tpl)

    facts = _activity.build_data(name, dirname, user=user)
    try:
        generated = _llm.write(task, facts, style=style)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except _llm.LLMError as exc:
        raise HTTPException(502, f"LLM 生成失败：{exc}")

    merged = _activity.build_data(name, dirname, extra=generated, user=user)
    try:
        result = _fill.render(tpl, dst, merged)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    rel = dst.relative_to(act_dir.resolve()).as_posix()
    audit(conn, user["id"], "llm_fill", project=name, target=rel,
          detail={"activity": dirname, "task": task, "template": tpl.stem},
          ip=request.client.host)
    return {"path": rel, "filled": result["filled"],
            "missing": result["missing"], "generated": generated}


# ---------- B2：报销校验 / 图注 / 满意度 ----------
# 缩略图缓存目录：放 ROOT/state 下，不落进项目目录（避免 Syncthing 同步缓存）
_THUMB_DIR = "thumbs"
_THUMB_WIDTH = 320


def _photo_thumb(path: Path) -> str:
    """生成/复用缩略图，返回 data URI；PIL 缺失或解码失败返回空串。

    照片原件动辄 4~5 MB，25 张就是 200 MB，不能直接塞进浏览器；
    这里按「路径 + mtime」做键缓存 320px 宽 JPEG，前端拿 data URI 直接显示。
    """
    try:
        from PIL import Image
    except ImportError:
        return ""

    try:
        stat = path.stat()
        key = hashlib.sha1(f"{path.resolve()}|{stat.st_mtime_ns}".encode()).hexdigest()
        target = ROOT / "state" / _THUMB_DIR / f"{key}.jpg"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(path) as img:
                img.thumbnail((_THUMB_WIDTH, _THUMB_WIDTH * 4))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                tmp = target.with_suffix(".tmp")
                img.save(tmp, "JPEG", quality=72)
                tmp.replace(target)
        return "data:image/jpeg;base64," + base64.b64encode(
            target.read_bytes()).decode()
    except Exception:
        return ""            # 损坏图片 / 只读挂载都不该让整个列表 500


@app.post("/api/projects/{name}/activities/{dirname}/invoice/scan")
def api_invoice_scan(name: str, dirname: str, user=Depends(require_write)):
    """扫发票 + 三表校验，返回 engine.invoice.cross_check() 的结果。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)
    return _invoice.cross_check(act_dir)


@app.post("/api/projects/{name}/activities/{dirname}/invoice/report")
def api_invoice_report(name: str, dirname: str, body: dict,
                       user=Depends(require_write)):
    """生成发票明细表，返回 {path, count, total}。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)

    items = _invoice.scan_invoices(act_dir)
    report = _script("invoice_report")
    out_rel = str(body.get("output")
                  or f"{_invoice.INVOICE_SUBDIR}/{report.DETAIL_NAME}").strip()
    dst = _safe_rel_path(act_dir, out_rel)
    if not dst.suffix:
        dst = dst.with_name(dst.name + ".xlsx")

    template = _template_library() / "发票明细表.xlsx"
    if not template.is_file():
        raise HTTPException(400, f"发票明细表模板不存在：{template}")
    report.DETAIL_TEMPLATE = template
    try:
        report.render_detail(items, dst)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))

    total = round(sum(item["amount"] for item in items
                      if item.get("amount") is not None), 2)
    return {"path": dst.relative_to(act_dir.resolve()).as_posix(),
            "count": len(items), "total": total}


@app.get("/api/projects/{name}/activities/{dirname}/photos")
def api_activity_photos(name: str, dirname: str, sub: str = _caption.PHOTO_SUBDIR,
                        user=Depends(current_user)):
    """列照片：文件名 / 大小 / mtime / 缩略图 / 是否程序默认选中。

    缩略图是 data URI（认证只走 x-token header，`<img src>` 带不了 header）。
    """
    act_dir, _meta = _activity_meta_or_404(name, dirname, user)
    folder = _safe_rel_path(act_dir, sub)
    if not folder.is_dir():
        return []

    default = {path.relative_to(folder).as_posix()
               for path in _caption.pick_photos(act_dir, sub)}
    files = [path for path in sorted(folder.rglob("*"))
             if path.is_file() and not path.name.startswith(".")
             and path.suffix.lower() in _caption.IMAGE_EXTS]
    # 单张 4~5 MB，逐张解码要 6 秒；4 线程压到 2 秒左右（PIL 解码会放 GIL）
    with ThreadPoolExecutor(max_workers=4) as pool:
        thumbs = list(pool.map(_photo_thumb, files))

    out: list[dict] = []
    for path, thumb in zip(files, thumbs):
        rel = path.relative_to(folder).as_posix()
        stat = path.stat()
        out.append({
            "name": rel,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, TZ).isoformat(),
            "picked": rel in default,
            "thumb": thumb,
        })
    return out


@app.post("/api/projects/{name}/activities/{dirname}/captions")
def api_activity_captions(name: str, dirname: str, body: dict,
                          user=Depends(require_write)):
    """生成图注：picked 为空时由程序按文件名挑图。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)

    sub = str(body.get("sub") or _caption.PHOTO_SUBDIR).strip()
    style = str(body.get("style") or "")
    picked = body.get("picked") or []
    if not isinstance(picked, list):
        raise HTTPException(400, "picked 必须是文件名数组")

    folder = _safe_rel_path(act_dir, sub)
    if picked:
        photos: list[Path] = []
        for raw in picked:
            target = _safe_rel_path(folder, str(raw).strip())
            if target.is_file() and target.suffix.lower() in _caption.IMAGE_EXTS:
                photos.append(target)
        if not photos:
            raise HTTPException(400, "勾选的照片都不存在")
    else:
        photos = _caption.pick_photos(act_dir, sub)
    if not photos:
        raise HTTPException(404, f"没有可用的照片：{sub}")

    facts = _activity.build_data(name, dirname, user=user)
    try:
        return _caption.write_captions(photos, facts, style=style)
    except _llm.LLMError as exc:
        raise HTTPException(502, f"图注生成失败：{exc}")


@app.post("/api/projects/{name}/activities/{dirname}/captions/fill")
def api_captions_fill(name: str, dirname: str, body: dict,
                      user=Depends(require_write)):
    """把图注填进模板的 {{图注_N}}，返回 {path, filled, missing}。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)

    raw = body.get("captions")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "captions 不能为空")
    captions = [{"file": str(item.get("file") or ""),
                 "caption": str(item.get("caption") or "").strip()}
                for item in raw if isinstance(item, dict)]
    captions = [item for item in captions if item["caption"]]
    if not captions:
        raise HTTPException(400, "captions 里没有有效图注")

    tpl = _find_template(body.get("template"))
    dst = _fill_output(act_dir, body, tpl)
    try:
        result = _fill.render(tpl, dst, _caption.as_fill_data(captions))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return {"path": dst.relative_to(act_dir.resolve()).as_posix(),
            "filled": result["filled"], "missing": result["missing"]}


@app.post("/api/projects/{name}/activities/{dirname}/satisfaction/list")
def api_sat_list(name: str, dirname: str, user=Depends(require_write)):
    """扫问卷照片，生成/更新 scores.json（保留已录分数）。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)
    entries, _path = _script("satisfaction").write_scores(act_dir)
    return entries


@app.get("/api/projects/{name}/activities/{dirname}/satisfaction")
def api_sat_get(name: str, dirname: str, user=Depends(current_user)):
    """读 scores.json；没有文件返回空列表。"""
    act_dir, _meta = _activity_meta_or_404(name, dirname, user)
    return _script("satisfaction").load_scores(act_dir)


@app.put("/api/projects/{name}/activities/{dirname}/satisfaction")
def api_sat_put(name: str, dirname: str, body: dict,
                user=Depends(require_write)):
    """写回 scores.json：body.scores = [{file, score, satisfied}]。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)

    scores = body.get("scores")
    if not isinstance(scores, list):
        raise HTTPException(400, "scores 必须是数组")
    entries: list[dict] = []
    for item in scores:
        if not isinstance(item, dict):
            raise HTTPException(400, "scores 的每一项必须是对象")
        file = str(item.get("file") or "").strip()
        if not file:
            raise HTTPException(400, "每条分数都要有 file")
        score = item.get("score")
        if score is not None and (isinstance(score, bool)
                                  or not isinstance(score, (int, float))):
            raise HTTPException(400, f"分数必须是数字：{file}")
        flag = item.get("satisfied")
        entries.append({"file": file, "score": score,
                        "satisfied": flag if isinstance(flag, bool) else None})

    path = _script("satisfaction").scores_path(act_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return {"ok": True, "count": len(entries),
            "path": path.relative_to(act_dir.resolve()).as_posix()}


@app.post("/api/projects/{name}/activities/{dirname}/satisfaction/fill")
def api_sat_fill(name: str, dirname: str, user=Depends(require_write)):
    """统计并填 05-满意度测评汇总表，返回统计结果 + 落盘路径。"""
    require_write(user)
    act_dir, _meta = _activity_meta_or_404(name, dirname, user, write=True)

    sat = _script("satisfaction")
    entries = sat.load_scores(act_dir)
    if not entries:
        raise HTTPException(400, "没有 scores.json，先点「扫描问卷」生成清单")
    stats = sat.summarize(entries)
    if not stats["份数"]:
        raise HTTPException(400, "一份分数都没录，无法统计")

    template = _template_library() / "满意度测评汇总表.docx"
    if not template.is_file():
        raise HTTPException(400, f"满意度汇总表模板不存在：{template}")
    sat.SUMMARY_TEMPLATE = template

    dst = act_dir / sat.SUMMARY_NAME
    try:
        result = sat.render_summary(stats, sat._facts(act_dir), dst)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))

    return {**stats,
            "path": dst.relative_to(act_dir.resolve()).as_posix(),
            "filled": result["filled"], "missing": result["missing"]}


def _sync_targets(project: str) -> list[str]:
    """同步范围：空 = 全部项目；指定项目必须存在。"""
    names = [p["name"] for p in _projects.list_projects()]
    if not project:
        return names
    if project not in names:
        raise HTTPException(404, f"项目不存在：{project}")
    return [project]


@app.post("/api/sync/feishu")
def api_sync_feishu(body: dict, background: BackgroundTasks, request: Request,
                    user=Depends(require_admin), conn=Depends(db)):
    """飞书同步。push 很慢，放后台跑，请求立即返回。"""
    require_admin(user)
    project = str(body.get("project") or "").strip()
    targets = _sync_targets(project)
    background.add_task(_run_feishu_sync, project)
    audit(conn, user["id"], "sync_feishu", target=project or "*",
          detail={"projects": targets}, ip=request.client.host)
    return {
        "started": True,
        "task": "飞书同步已在后台开始",
        # 目录与镜像结果要等后台跑完才知道，这里先给出范围
        "projects": [{"name": n, "created_dir": None, "pushed": None}
                     for n in targets],
    }


@app.post("/api/cognee/index")
def api_cognee_index(body: dict, background: BackgroundTasks, request: Request,
                     user=Depends(require_admin), conn=Depends(db)):
    """Cognee 入库。建图很慢，放后台跑，请求立即返回。"""
    require_admin(user)
    project = str(body.get("project") or "").strip()
    targets = _sync_targets(project)
    background.add_task(_run_cognee_index, project)
    audit(conn, user["id"], "cognee_index", target=project or "*",
          detail={"projects": targets}, ip=request.client.host)
    return {"started": True, "task": "Cognee 入库已在后台开始",
            "projects": targets}


@app.get("/api/cognee/datasets")
def api_cognee_datasets(user=Depends(current_user)):
    """Cognee 数据集列表 + 每个数据集的文档条数与图节点数。"""
    try:
        datasets = _cognee_index.list_datasets(ROOT)
    except Exception as exc:
        raise HTTPException(502, f"Cognee 不可用：{exc}")

    try:
        summary = _cognee_index.graph_summary(ROOT)
    except Exception:
        summary = {}          # 图还没建 / 接口暂不可用时节点数留 None

    items: list[dict] = []
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        did = str(ds.get("id") or "")
        try:
            count = _cognee_index.data_count(ROOT, did) if did else None
        except Exception:
            count = None
        nodes = (summary.get(did) or {}).get("numNodes")
        items.append({
            "id": did,
            "name": ds.get("name"),
            "count": count,
            "nodes": nodes,
            "created_at": ds.get("createdAt"),
        })
    return {"total": len(items), "items": items}


# ==========================================================================
# E1：/api/sw/* Agent 接口层
# ==========================================================================
SW_VERSION = "1.0"
# task 走 LLM 时默认落到哪个模板（与模板库文件名一致）
_SW_TASK_TEMPLATE = {
    "brief": "简报",
    "plan": "计划书",
    "summary": "总结报告",
    "supervision": "监督记录",
}


def _sw_project_meta(project: str) -> dict:
    """取项目元数据；项目不存在统一成机器可读错误。"""
    try:
        return _project_meta_or_404(project)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise ApiError(404, "PROJECT_NOT_FOUND", f"项目不存在：{project}",
                           "先调 GET /api/sw/projects 确认项目名")
        raise ApiError(400, "VALIDATION", str(exc.detail))


def _sw_activity_dir(project: str, activity: str, user, *, write: bool) -> tuple[Path, dict]:
    """定位活动目录并做项目级授权；错误转成 ApiError。"""
    meta = _sw_project_meta(project)
    if write:
        if user.get("role") not in ("admin", "editor"):
            raise ApiError(403, "FORBIDDEN", "无写权限（需 editor/admin）")
        check_project(user, meta["name"], write=True)
    else:
        check_project(user, meta["name"])
    try:
        act_dir = _projects.activity_dir(project, activity)
    except (ValueError, FileNotFoundError) as exc:
        raise ApiError(400, "VALIDATION", str(exc))
    if not act_dir.is_dir():
        raise ApiError(404, "ACTIVITY_NOT_FOUND", f"活动目录不存在：{activity}")
    return act_dir, meta


def _sw_template(name: str) -> Path:
    try:
        return _find_template(name)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise ApiError(404, "TEMPLATE_NOT_FOUND", str(exc.detail),
                           "调 GET /api/templates 查看可用模板")
        raise ApiError(400, "VALIDATION", str(exc.detail))


def _sw_preview(path: Path, limit: int = 500) -> str:
    """从生成好的 docx/xlsx 里抽前 limit 个字符，供 Agent 确认内容。"""
    try:
        suffix = path.suffix.lower()
        if suffix == ".docx":
            from docx import Document
            doc = Document(str(path))
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    line = " | ".join(c for c in cells if c)
                    if line:
                        parts.append(line)
            text = "\n".join(parts)
        elif suffix == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(str(path), data_only=True)
            parts = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    line = " | ".join(str(v) for v in row if v not in (None, ""))
                    if line:
                        parts.append(line)
            wb.close()
            text = "\n".join(parts)
        else:
            return ""
        return text[:limit]
    except Exception:
        return ""


def _sw_run_impl(body: dict, request: Request, conn, user) -> dict:
    """一步生成的核心实现，/run 与 /batch generate 共用。"""
    project = str(body.get("project") or "").strip()
    activity = str(body.get("activity") or "").strip()
    if not project or not activity:
        raise ApiError(400, "VALIDATION", "project 与 activity 必填")

    act_dir, meta = _sw_activity_dir(project, activity, user, write=True)

    template = str(body.get("template") or "").strip()
    task = str(body.get("task") or "").strip()
    if not template and not task:
        raise ApiError(400, "VALIDATION", "template 与 task 至少给一个")

    extra = body.get("data") or {}
    if not isinstance(extra, dict):
        raise ApiError(400, "VALIDATION", "data 必须是对象")
    style = str(body.get("style") or "")
    dry_run = bool(body.get("dry_run", False))

    # 生成材料的前置条件：活动必须有编号
    try:
        _activity_require_code(act_dir)
    except HTTPException as exc:
        raise ApiError(400, "CODE_MISSING", str(exc.detail),
                       "先在 activity.yaml 配置 code，或调 PATCH 活动接口补编号")

    if task:
        # task 走 LLM 时仍要落到某个模板；不传 template 就按任务给默认模板
        tpl_name = template or _SW_TASK_TEMPLATE.get(task, "")
        if not tpl_name:
            raise ApiError(400, "VALIDATION", f"未知 task：{task}",
                           "可选：" + "、".join(_SW_TASK_TEMPLATE))
        tpl = _sw_template(tpl_name)
    else:
        tpl = _sw_template(template)
    dst_rel = _fill_output(act_dir, body, tpl)
    generated: dict = {}
    self_check = None

    try:
        if task:
            facts = _activity.build_data(project, activity, user=user)
            generated = _llm.write(task, facts, style=style)
            self_check = (generated or {}).get("_check")
            merged = _activity.build_data(
                project, activity, extra={**generated, **extra}, user=user)
        else:
            merged = _activity.build_data(project, activity, extra=extra, user=user)
    except HTTPException:
        raise
    except ValueError as exc:
        raise ApiError(400, "VALIDATION", str(exc))
    except _llm.LLMError as exc:
        raise ApiError(502, "LLM_FAILED", f"LLM 生成失败：{exc}")
    except FileNotFoundError as exc:
        raise ApiError(404, "PROJECT_NOT_FOUND", str(exc))

    tmpdir: Path | None = None
    target = dst_rel
    if dry_run:
        # 预演：写到临时目录，拿到真实的 filled/missing 和预览后即删
        tmpdir = Path(tempfile.mkdtemp())
        target = tmpdir / dst_rel.name
    try:
        try:
            result = _fill.render(tpl, target, merged)
        except ValueError as exc:
            raise ApiError(400, "VALIDATION", str(exc))
        preview = _sw_preview(target)
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)

    rel = (Path("活动") / act_dir.name / dst_rel.name).as_posix()
    detail = {"activity": activity, "filled": result["filled"], "dry_run": dry_run}
    if template:
        detail["template"] = tpl.stem
    if task:
        detail["task"] = task
    audit(conn, user["id"], "sw_run", project=meta["name"], target=rel,
          detail=detail, ip=request.client.host)

    out = {
        "ok": True,
        "path": rel,
        "filled": result["filled"],
        "missing": result["missing"],
        "preview": preview,
        "dry_run": dry_run,
    }
    if task:
        out["self_check"] = self_check
        out["generated"] = {k: v for k, v in generated.items()
                            if not str(k).startswith("_")}
    return out


@app.get("/api/sw/capabilities")
def api_sw_capabilities():
    """能力自描述。不需要认证：Agent 先探能力，再拿 Key 干活。"""
    return {
        "version": SW_VERSION,
        "auth": {
            "header": "Authorization: Bearer sk-sw-xxx",
            "alt": "x-api-key: sk-sw-xxx",
            "issue": "POST /api/sw/keys (需 admin)",
        },
        "permissions": {
            "model": "项目级 readonly/write，admin 全权",
            "list_projects": "GET /api/sw/projects",
        },
        "endpoints": [
            {"method": "GET", "path": "/api/sw/projects", "desc": "列项目", "params": {}},
            {"method": "GET", "path": "/api/sw/projects/{p}/activities",
             "desc": "列活动", "params": {"p": "项目名"}},
            {"method": "GET", "path": "/api/sw/projects/{p}/activities/{a}",
             "desc": "活动详情", "params": {}},
            {"method": "POST", "path": "/api/sw/run", "desc": "一步生成文件",
             "params": {"project": "项目名", "activity": "活动目录名",
                        "template": "模板名（与 task 二选一）",
                        "task": "LLM 任务（与 template 二选一）",
                        "style": "文风", "data": "覆盖字段",
                        "output": "输出文件名", "dry_run": "只预演"}},
            {"method": "POST", "path": "/api/sw/batch", "desc": "批量操作",
             "params": {"action": "check|generate|upload", "project": "可选",
                        "activities": "可选", "params": "动作参数"}},
            {"method": "GET", "path": "/api/sw/tasks/{id}", "desc": "异步任务状态",
             "params": {}},
            {"method": "GET", "path": "/api/sw/search", "desc": "检索",
             "params": {"q": "关键词", "project": "可选", "limit": "默认 10"}},
            {"method": "GET", "path": "/api/sw/me", "desc": "当前身份与权限",
             "params": {}},
            {"method": "GET", "path": "/api/sw/keys", "desc": "列 API Key", "params": {}},
        ],
        "errors": {
            "PROJECT_NOT_FOUND": "项目不存在",
            "ACTIVITY_NOT_FOUND": "活动目录不存在",
            "FORBIDDEN": "无该项目权限",
            "READONLY": "该项目只读",
            "CODE_MISSING": "项目编号未配置",
            "TEMPLATE_NOT_FOUND": "模板不存在",
            "VALIDATION": "参数校验失败",
            "LLM_FAILED": "LLM 生成失败",
        },
    }


@app.get("/api/sw/projects")
def api_sw_projects(user=Depends(current_user)):
    """列当前 Key / 账号可见的项目，并标出 access。"""
    items = []
    for item in _projects.list_projects():
        if not _project_allowed(user, item):
            continue
        access = "write" if can_write(user, item["name"]) else "readonly"
        items.append({**item, "access": access})
    return {"total": len(items), "items": items}


@app.get("/api/sw/projects/{p}/activities")
def api_sw_activities(p: str, user=Depends(current_user)):
    """列活动（直接复用 Web 端点的数据结构）。"""
    try:
        return api_list_activities(p, user)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise ApiError(404, "PROJECT_NOT_FOUND", str(exc.detail))
        if exc.status_code == 403:
            raise ApiError(403, "FORBIDDEN", str(exc.detail))
        raise ApiError(400, "VALIDATION", str(exc.detail))


@app.get("/api/sw/projects/{p}/activities/{a}")
def api_sw_activity_detail(p: str, a: str, user=Depends(current_user)):
    """活动详情（直接复用 Web 端点的数据结构）。"""
    try:
        return api_activity_detail(p, a, user)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise ApiError(404, "ACTIVITY_NOT_FOUND", str(exc.detail))
        if exc.status_code == 403:
            raise ApiError(403, "FORBIDDEN", str(exc.detail))
        raise ApiError(400, "VALIDATION", str(exc.detail))


@app.get("/api/sw/me")
def api_sw_me(user=Depends(current_user)):
    """当前身份、角色、项目权限与认证方式。"""
    return {
        "username": user["username"],
        "real_name": user.get("real_name", ""),
        "role": user["role"],
        "projects": user.get("projects") or {},
        "auth_type": user.get("auth_type", "session"),
    }


@app.post("/api/sw/keys")
def api_create_key(body: dict, request: Request, conn=Depends(db),
                   user=Depends(require_admin)):
    """签发 API Key。明文只在本次响应里返回一次，不落库、不进审计。"""
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
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO api_keys(user_id, name, key_hash, key_prefix,
                                    scopes, expires_at)
               VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
            (row[0], name, digest, prefix,
             [str(x) for x in scopes] if scopes else None, expires_at),
        )
        key_id = cur.fetchone()[0]
    conn.commit()

    # 审计只记前缀和名字，明文 Key 绝不落库
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


@app.get("/api/sw/keys")
def api_list_keys(conn=Depends(db), user=Depends(current_user)):
    """admin 看全部；普通账号只看自己的。永不返回明文或完整哈希。"""
    with conn.cursor() as cur:
        if user["role"] == "admin":
            cur.execute(
                """SELECT k.id, k.user_id, u.username, u.real_name, k.name,
                          k.key_prefix, k.scopes, k.expires_at, k.created_at,
                          k.last_used_at, k.revoked_at
                   FROM api_keys k JOIN users u ON u.id = k.user_id
                   ORDER BY k.id DESC""")
        else:
            cur.execute(
                """SELECT k.id, k.user_id, u.username, u.real_name, k.name,
                          k.key_prefix, k.scopes, k.expires_at, k.created_at,
                          k.last_used_at, k.revoked_at
                   FROM api_keys k JOIN users u ON u.id = k.user_id
                   WHERE k.user_id = %s ORDER BY k.id DESC""", (user["id"],))
        rows = cur.fetchall()
    now = datetime.now(timezone.utc)
    items = []
    for r in rows:
        exp = r[7]
        expired = bool(exp and (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)) <= now)
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
        })
    return {"total": len(items), "items": items}


@app.delete("/api/sw/keys/{key_id}")
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


@app.post("/api/sw/run")
def api_sw_run(body: dict, request: Request, conn=Depends(db),
               user=Depends(current_user)):
    """一步生成：template 走模板填充，task 走 LLM 生成后填充。"""
    try:
        return _sw_run_impl(body, request, conn, user)
    except ApiError:
        raise
    except HTTPException as exc:
        # 兜底：引擎里仍有用 HTTPException 抛出的参数错误，统一成机器可读格式
        code = "VALIDATION" if exc.status_code < 500 else "INTERNAL"
        raise ApiError(exc.status_code, code, str(exc.detail))


def _sw_batch_projects(body: dict, user) -> list[dict]:
    """批量操作的项目范围：指定项目或全部授权项目。"""
    project = str(body.get("project") or "").strip()
    items = _projects.list_projects()
    if project:
        meta = _sw_project_meta(project)
        check_project(user, meta["name"])
        return [meta]
    return [it for it in items if _project_allowed(user, it)]


@app.post("/api/sw/batch")
def api_sw_batch(body: dict, request: Request, conn=Depends(db),
                 user=Depends(current_user)):
    """批量核对 / 生成 / 上传。每项独立执行，单项失败不影响其余项。"""
    action = str(body.get("action") or "").strip()
    if action not in ("check", "generate", "upload"):
        raise ApiError(400, "VALIDATION", "action 必须是 check / generate / upload")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise ApiError(400, "VALIDATION", "params 必须是对象")
    picked = body.get("activities") or []
    if picked and not isinstance(picked, list):
        raise ApiError(400, "VALIDATION", "activities 必须是数组")

    results: list[dict] = []
    for meta in _sw_batch_projects(body, user):
        name = str(meta["name"])
        try:
            act_dirs = _activities.list_existing_activities(
                Path(_projects.PROJECTS_ROOT) / name)
        except Exception:
            act_dirs = []
        targets = [str(x) for x in picked] if picked else act_dirs
        for activity in targets:
            row = {"activity": activity, "project": name}
            try:
                if action == "check":
                    act_dir, _meta = _sw_activity_dir(name, activity, user, write=False)
                    check = _checks.check(act_dir)
                    row.update(status="ok", detail={
                        "complete": check.get("complete"),
                        "issues": check.get("issues") or [],
                    })
                elif action == "generate":
                    payload = {**params, "project": name, "activity": activity}
                    out = _sw_run_impl(payload, request, conn, user)
                    row.update(status="ok", detail={
                        "path": out["path"], "filled": out["filled"],
                        "missing": out["missing"]})
                else:  # upload
                    act_dir, _meta = _sw_activity_dir(name, activity, user, write=True)
                    files = params.get("files") or []
                    if not isinstance(files, list) or not files:
                        raise ApiError(400, "VALIDATION", "upload 需要 params.files")
                    sub = _activity_sub_dir(act_dir, str(params.get("sub") or ""))
                    written = []
                    for item in files:
                        if not isinstance(item, dict):
                            raise ApiError(400, "VALIDATION", "files 的每一项必须是对象")
                        filename = _activity_upload_filename(item.get("name"))
                        content = base64.b64decode(str(item.get("content_base64") or ""))
                        dest = sub / filename
                        if dest.exists():
                            raise ApiError(409, "FILE_EXISTS", f"同名文件已存在：{filename}")
                        dest.write_bytes(content)
                        written.append(filename)
                    row.update(status="ok", detail={"uploaded": written})
            except ApiError as exc:
                row.update(status="failed", detail={
                    "code": exc.code, "message": exc.message})
            except HTTPException as exc:
                row.update(status="failed", detail={
                    "code": "VALIDATION", "message": str(exc.detail)})
            except Exception as exc:
                row.update(status="failed", detail={
                    "code": "INTERNAL", "message": str(exc)})
            results.append(row)

    ok = sum(1 for r in results if r["status"] == "ok")
    return {"action": action, "total": len(results), "ok": ok,
            "failed": len(results) - ok, "results": results}


@app.get("/api/sw/tasks/{task_id}")
def api_sw_task(task_id: str, user=Depends(current_user)):
    """异步任务状态（桩）。

    飞书同步 / Cognee 入库后续接真正的任务表；先给 Agent 一个稳定契约，
    避免以后改返回结构。
    """
    return {
        "id": task_id,
        "status": "done",
        "result": {"note": "异步任务尚未接入，当前为同步完成的占位状态"},
    }


@app.get("/api/sw/search")
def api_sw_search(q: str, project: str = "", limit: int = 10,
                  user=Depends(current_user)):
    """统一检索：文档台账 + Cognee 图谱。"""
    query = str(q or "").strip()
    if not query:
        raise ApiError(400, "VALIDATION", "q 不能为空")
    limit = max(1, min(int(limit or 10), 50))
    scopes = scopes_of(user, project)
    if not scopes:
        raise ApiError(403, "FORBIDDEN", "当前账号没有可检索的项目")

    results: list[dict] = []
    # 1) 文档台账：按文件名 / 分类 / 项目名做包含匹配，取 md 前 500 字做摘要
    manifest = _load_manifest()
    needle = query.lower()
    for token, entry in (manifest.get("files") or {}).items():
        if not isinstance(entry, dict) or entry.get("status") == "deleted":
            continue
        entry_project = _legacy_project_name(str(entry.get("project") or ""))
        if user.get("role") != "admin" and _project_access(user, entry_project) is None:
            continue
        haystack = " ".join(str(entry.get(k) or "") for k in
                            ("name", "category", "project", "original_name"))
        if needle not in haystack.lower():
            continue
        snippet = ""
        md = entry.get("md")
        if md:
            try:
                md_path = (ROOT / md).resolve()
                if md_path.is_file():
                    snippet = md_path.read_text(encoding="utf-8", errors="ignore")[:500]
            except Exception:
                snippet = ""
        results.append({
            "source": "document",
            "path": str(entry.get("original") or entry.get("md") or token),
            "snippet": snippet or str(entry.get("name") or ""),
            "score": 1.0 if needle in str(entry.get("name") or "").lower() else 0.6,
        })

    # 2) Cognee 图谱：检索失败不阻断文档结果
    try:
        ok, output, _timeout = search_datasets(query, scopes, qtype="CHUNKS")
        if ok and output:
            results.append({
                "source": "cognee",
                "path": ",".join(scopes),
                "snippet": output[:500],
                "score": 0.8,
            })
    except Exception:
        pass

    results.sort(key=lambda item: item.get("score") or 0, reverse=True)
    return {"query": query, "results": results[:limit]}


@app.get("/api/dashboard")
def api_dashboard(user=Depends(current_user)):
    """仪表盘：项目活动进度 + 最近 10 个有改动的活动。"""
    projects: list[dict] = []
    recent: list[dict] = []

    for item in _projects.list_projects():
        if not _project_allowed(user, item):
            continue
        name = str(item.get("name") or "")
        act_root = Path(_projects.PROJECTS_ROOT) / name / "活动"
        dirs = ([p for p in sorted(act_root.iterdir(), key=lambda x: x.name)
                 if p.is_dir() and not p.name.startswith(".")]
                if act_root.is_dir() else [])

        complete = 0
        for act_dir in dirs:
            try:
                ok = _checks.check(act_dir)["complete"]
            except Exception:
                ok = False
            if ok:
                complete += 1
            recent.append({
                "project": name,
                "activity": act_dir.name,
                "status": str(_activity_yaml_data(act_dir).get("status") or ""),
                "mtime": datetime.fromtimestamp(
                    _activity_mtime(act_dir), TZ).isoformat(),
            })

        projects.append({
            "name": name,
            "short": _project_short(item),
            "activity_total": len(dirs),
            "activity_complete": complete,
        })

    recent.sort(key=lambda x: x["mtime"], reverse=True)
    return {"projects": projects, "recent": recent[:10]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SETTINGS.server["host"], port=int(SETTINGS.server["port"]))
