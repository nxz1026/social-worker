#!/usr/bin/env python3
"""社工文档服务 API（FastAPI）。

认证：session token（PG sessions 表，Header: x-token）
权限：role(admin/editor/viewer) + user_projects 项目隔离
审计：写操作落 audit_log
归档：调 scripts/archive.py
检索：调 scripts/cognee.py search
"""
from __future__ import annotations

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import psycopg
import yaml
from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

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
from engine import checks as _checks  # noqa: E402
from engine import coding as _coding  # noqa: E402
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
            """SELECT u.id, u.username, u.role, u.enabled, s.expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = %s""",
            (token,),
        )
        row = cur.fetchone()
    if not row or not row[3]:
        return None
    if row[4] < datetime.now(TZ):
        return None
    return {"id": row[0], "username": row[1], "role": row[2]}

def user_projects(conn, user_id: int, role: str) -> list[str]:
    if role == "admin":
        return list(ALL_PROJECTS)
    with conn.cursor() as cur:
        cur.execute("SELECT project FROM user_projects WHERE user_id = %s", (user_id,))
        return [r[0] for r in cur.fetchall()]

# ---------- 审计 ----------
# 审计明细里绝不能出现口令 / token / 个案正文；只保留排查需要的字段。
_SENSITIVE_DETAIL_KEYS = {
    "password", "passwd", "pwd", "token", "api_key", "apikey", "secret",
    "context", "content",
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

# ---------- 依赖 ----------
def current_user(request: Request, conn=Depends(db)):
    token = request.headers.get("x-token")   # 只走 header，避免 token 进 URL/日志
    if not token:
        raise HTTPException(401, "未登录")
    u = get_user(conn, token)
    if not u:
        raise HTTPException(401, "登录已过期")
    u["projects"] = user_projects(conn, u["id"], u["role"])
    return u

def require_write(user=Depends(current_user)):
    if user["role"] not in ("admin", "editor"):
        raise HTTPException(403, "无写权限（需 editor/admin）")
    return user

def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "需 admin")
    return user

def check_project(user, project: str):
    if project not in user["projects"]:
        raise HTTPException(403, f"无项目 {project} 权限")

def scopes_of(user, project: str = "") -> list[str]:
    """本次请求允许访问的项目集合。

    零项目账号返回空列表 —— 绝不能兜底成 `["huadu"]` 或「不过滤」。
    调用方必须把空列表当成「没有数据」，而不是「全部数据」。
    """
    if project:
        if project not in user["projects"]:
            raise HTTPException(403, f"无项目 {project} 权限")
        return [project]
    return list(user["projects"])

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
    return {"username": user["username"], "role": user["role"],
            "projects": user["projects"]}

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
        if e.get("project") not in scopes:
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
        if e.get("project") not in scopes:
            continue
        ds = _dataset_for(str(e.get("project") or ""))
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
    check_project(user, e.get("project"))
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
    check_project(user, str(e.get("project") or ""))
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
        check_project(user, project)
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

        archived_project = str(receipt.get("project") or "")
        if archived_project and archived_project not in user["projects"]:
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
    check_project(user, e.get("project"))
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
        check_project(user, e.get("project"))
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
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, role, enabled, created_at FROM users ORDER BY id")
        rows = cur.fetchall()
    out = []
    for r in rows:
        with conn.cursor() as cur:
            cur.execute("SELECT project FROM user_projects WHERE user_id = %s", (r[0],))
            projs = [x[0] for x in cur.fetchall()]
        out.append({"id": r[0], "username": r[1], "role": r[2],
                    "enabled": r[3], "created_at": str(r[4]), "projects": projs})
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

@app.post("/api/admin/users")
def admin_create_user(body: dict, request: Request, conn=Depends(db),
                      user=Depends(require_admin)):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = _valid_role(body.get("role") or "viewer")
    projects = body.get("projects") or []
    if not username or not password:
        raise HTTPException(400, "用户名或密码为空")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            raise HTTPException(409, "用户名已存在")
        cur.execute(
            "INSERT INTO users(username, password_hash, role) VALUES(%s,%s,%s) RETURNING id",
            (username, hash_pw(password), role),
        )
        uid = cur.fetchone()[0]
        for p in projects:
            if p in ALL_PROJECTS:
                cur.execute("INSERT INTO user_projects(user_id, project) VALUES(%s,%s)",
                            (uid, p))
    conn.commit()
    audit(conn, user["id"], "create_user", target=username,
          detail={"role": role, "projects": projects}, ip=request.client.host)
    return {"ok": True, "id": uid}

@app.patch("/api/admin/users/{uid}")
def admin_update_user(uid: int, body: dict, request: Request, conn=Depends(db),
                      user=Depends(require_admin)):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE id = %s", (uid,))
        if not cur.fetchone():
            raise HTTPException(404, "用户不存在")
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
            cur.execute("DELETE FROM user_projects WHERE user_id=%s", (uid,))
            for p in body["projects"]:
                if p in ALL_PROJECTS:
                    cur.execute("INSERT INTO user_projects(user_id, project) VALUES(%s,%s)",
                                (uid, p))
    conn.commit()
    audit(conn, user["id"], "update_user", target=str(uid),
          detail=body, ip=request.client.host)
    return {"ok": True}

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
    """admin 看全部；其余账号按 user_projects 与 project.short 的对应关系过滤。"""
    if user["role"] == "admin":
        return True
    return _project_short(item) in scopes_of(user)


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
    short = str((data.get("project") or {}).get("short") or name)
    if user["role"] != "admin" and short not in scopes_of(user):
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
    """写权限：admin/editor；非 admin 还要求该项目在授权范围内。"""
    if user["role"] not in ("admin", "editor"):
        raise HTTPException(403, "无写权限（需 editor/admin）")
    if user["role"] != "admin" and meta["short"] not in scopes_of(user):
        raise HTTPException(403, f"无项目 {meta['name']} 权限")


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
    short = str((data.get("project") or {}).get("short") or name)
    if user["role"] != "admin" and short not in scopes_of(user):
        raise HTTPException(403, f"无项目 {name} 权限")
    try:
        act_dir = _projects.activity_dir(name, dirname)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not act_dir.is_dir():
        raise HTTPException(404, f"活动目录不存在：{dirname}")
    return _checks.check(act_dir)


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

    merged = _activity.build_data(name, dirname, extra=extra)
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
    facts = _activity.build_data(name, dirname)
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

    facts = _activity.build_data(name, dirname)
    try:
        generated = _llm.write(task, facts, style=style)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except _llm.LLMError as exc:
        raise HTTPException(502, f"LLM 生成失败：{exc}")

    merged = _activity.build_data(name, dirname, extra=generated)
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
