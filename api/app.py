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
import io
import json
import mimetypes
import os
import re
import socket
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import yaml
from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

# 让脚本模式（python3 api/app.py）与包模式（import api.app）都能定位到
# 同目录的 apikey/settings，以及仓库根的 engine/scripts 包。
_API_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _API_DIR.parent
for _p in (str(_REPO_ROOT), str(_API_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apikey as _apikey  # noqa: E402
import keysafe as _keysafe  # noqa: E402
from settings import SETTINGS  # noqa: E402
import status as status_mod  # noqa: E402

from api import admin, auth  # noqa: E402
from api.deps import (can_write, check_project, current_user, db,  # noqa: E402
                      project_access, project_short, require_admin,
                      require_project_write, require_write, scopes_of)
from api.errors import ApiError  # noqa: E402
from api.shared.audit import audit  # noqa: E402
from api.shared.delivery import sw_delivery  # noqa: E402
from api.shared.parse import as_bool  # noqa: E402
from api.shared.domain import (  # noqa: E402
    activity_meta_or_404, activity_require_code, activity_sub_dir,
    activity_upload_filename, activity_yaml_data, find_template, project_allowed,
    project_meta_or_404, project_year, script, template_library)
from api.shared.paths import (  # noqa: E402
    fill_output, force_flag, normalize_rel, safe_rel_path)
from api.state import (  # noqa: E402
    ALL_PROJECTS, API_KEY_BLOCKED_PREFIX, COGNEE_BASE, PG_DSN, REPO_ROOT,
    ROOT, SCRIPTS, TEMPLATES_DIR, TYPE_MAP, TZ,
    _ALIAS_CACHE, _API_OCR_EXTS, _ARCHIVE_MAX_BYTES, _ARCHIVE_MAX_ENTRIES,
    _ARCHIVE_MAX_UNCOMPRESSED, _ARCHIVE_RATIO_LIMIT, _ARCHIVE_RATIO_MIN_BYTES,
    _COGNEE_CACHE, _COGNEE_TTL, _PHOTO_EXTS, _PHOTO_MAX_BYTES,
    _PHOTO_MAX_FILES, _PHOTO_MAX_PIXELS, _PHOTO_MAX_TOTAL_BYTES,
    _SCRIPT_CACHE, _SUGGEST_CACHE)

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
# engine 是仓库根下的包，不在 scripts 里
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
from engine import llm as _llm  # noqa: E402
from engine import photos as _photos  # noqa: E402
from engine import projects as _projects  # noqa: E402

app = FastAPI(title="社工文档服务", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 已拆出的模块用 APIRouter 定义路由，在这里挂上。
# 顺序无关；路径与方法是路由装饰器里写的，与拆分前完全一致。
from api import admin as _admin_module  # noqa: E402
from api import auth as _auth_module  # noqa: E402
from api.routes import docs as _r_docs  # noqa: E402
from api.routes import activities as _r_activities  # noqa: E402
from api.routes import projects as _r_projects  # noqa: E402
from api.routes import materials as _r_materials  # noqa: E402

app.include_router(_auth_module.router)
app.include_router(_admin_module.router)
app.include_router(_r_docs.router)
app.include_router(_r_projects.router)
app.include_router(_r_activities.router)
app.include_router(_r_materials.router)
from api.routes import assets as _r_assets  # noqa: E402
# 测试直接按名字调端点函数（tests/test_api_web.py），这里保留同名入口。
from api.routes.projects import (api_check_project, api_create_project,  # noqa: E402,F401
                                 api_get_project, api_list_activities,
                                 api_list_projects, api_project_code,
                                 api_update_project)

app.include_router(_r_assets.router)
# 测试的 autouse fixture 只 patch app.audit（7 个测试文件都这么做）。routes 里的
# 端点从 shared.audit 模块全局名取 audit，所以让 shared.audit.audit 与 app.audit
# 指向同一个对象：patch app.audit 后两处同时变成空操作，审计不会落库。
import api.shared.audit as _audit_mod  # noqa: E402
_REAL_AUDIT = _audit_mod.audit


def audit(*a, **k):
    """转发到真实审计函数；测试 patch app.audit 时这层也一起被替掉。"""
    return _REAL_AUDIT(*a, **k)


_audit_mod.audit = audit


@app.on_event("startup")
def _ensure_api_key_secret_column() -> None:
    """补 api_keys.secret_enc 列（幂等）。

    新建数据库或旧库升级时自动加列，避免部署后第一次查看 Key 报 SQL 错误。
    失败不阻断服务启动：认证只依赖 key_hash，明文展示可稍后修。
    """
    try:
        with psycopg.connect(PG_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS secret_enc text")
            conn.commit()
    except Exception:
        pass

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

# ---------- 统一错误 ----------
@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    body: dict = {"error": {"code": exc.code, "message": exc.message,
                            "hint": exc.hint}}
    headers: dict[str, str] = {}
    if exc.retry_after is not None:
        body["retry_after"] = exc.retry_after
        headers["Retry-After"] = str(exc.retry_after)
    if request.url.path.startswith("/api/sw/"):
        return JSONResponse(status_code=exc.status_code, content=body,
                            headers=headers)
    # 非 v1 端点：沿用 FastAPI 默认形状，避免破坏现有 Web 前端；
    # 限速时额外带上 error/retry_after，前端与 Agent 都能识别。
    content: dict = {"detail": exc.message}
    if exc.retry_after is not None:
        content["error"] = body["error"]
        content["retry_after"] = exc.retry_after
    return JSONResponse(status_code=exc.status_code, content=content,
                        headers=headers)


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


# ---------- 项目别名 ----------
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
            if raw in (name, project_short(item)):
                return name
    except Exception:
        pass
    # 旧别名：config/project_aliases.yaml 里 huimin → 惠民、huadu → 花都
    group = _cached_project_aliases().get(raw) or []
    if group:
        try:
            for item in _projects.list_projects():
                name = str(item.get("name") or "")
                short = project_short(item)
                if any(word in name or word in short for word in group):
                    return name
        except Exception:
            pass
    return raw


# ---------- 台账 / Cognee / 状态辅助 ----------
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

def _entry_md_path(e: dict) -> Path | None:
    """台账条目的 md 路径；没有就返回 None。"""
    rel = normalize_rel(e.get("md") or e.get("md_path") or "", root=ROOT)
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

# ---------- 文档（端点已拆到 api/routes/docs.py，此处只留共用辅助） ----------

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
    """把请求里的相对路径限制在 `_待确认` 目录内（防路径越界）。

    审计 #3：当前没有任何端点调用本函数（`_待确认` 相关端点已随旧流程删除）。
    保留原因：若将来恢复 `_待确认` 处理，路径防护必须走这里，不能裸拼 ROOT。
    恢复调用时务必补「路径越界返回 400」的测试。
    """
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


@app.post("/api/readme/refresh")
def api_refresh_all_readme(user=Depends(require_admin)):
    require_admin(user)
    result = _readme.refresh_all()
    return {"written": len(result["written"]), "skipped": result["skipped"]}


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

def _activity_mtime(act_dir: Path) -> float:
    """活动目录的最近改动时间：取目录本身与所有文件 mtime 的最大值。"""
    latest = act_dir.stat().st_mtime
    for p in act_dir.rglob("*"):
        if p.is_file():
            latest = max(latest, p.stat().st_mtime)
    return latest


@app.get("/api/templates")
def api_templates(user=Depends(current_user)):
    """列模板库里的 docx/xlsx 及占位符。"""
    lib = template_library()
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
        return project_meta_or_404(project)
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
        return find_template(name)
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

    # 生成材料的前置条件：活动必须有编号
    try:
        activity_require_code(act_dir)
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
    # dry_run 写到临时目录、不碰原文件，因此不参与覆盖检查
    dry_run = bool(body.get("dry_run", False))
    dst_rel = fill_output(act_dir, body, tpl, check_overwrite=not dry_run)
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
        # 交付完整性：本 task 该填的字段有没有真的落进模板。
        # self_check 只查文风用词与字数，查不出「生成了但没填进模板」这种丢内容问题。
        out["delivery"] = sw_delivery(result["missing"], task)
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
            {"method": "GET", "path": "/api/sw/tasks/{id}",
             "desc": "异步任务状态（未实现，返回 501）",
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
        if not project_allowed(user, item):
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
    return [it for it in items if project_allowed(user, it)]


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
                    # 补生成前置条件（审计 P1-7）：核对清单不含「有没有编号」，
                    # 但缺编号时 generate 必然失败，调用方据 check 无法预判。
                    issues = list(check.get("issues") or [])
                    try:
                        activity_require_code(act_dir)
                    except HTTPException:
                        issues.append({
                            "code": "CODE_MISSING",
                            "message": "activity.yaml 缺 code，无法生成材料",
                        })
                    row.update(status="ok", detail={
                        "complete": check.get("complete") and not issues,
                        "issues": issues,
                        "generatable": not any(
                            i.get("code") == "CODE_MISSING" for i in issues),
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
                    sub = activity_sub_dir(act_dir, str(params.get("sub") or ""))
                    written = []
                    for item in files:
                        if not isinstance(item, dict):
                            raise ApiError(400, "VALIDATION", "files 的每一项必须是对象")
                        filename = activity_upload_filename(item.get("name"))
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
    """异步任务状态。当前尚未接入任务表。"""
    raise ApiError(501, "TASKS_NOT_IMPLEMENTED",
                   "异步任务尚未接入",
                   "本机所有操作都是同步完成，无需轮询；此端点保留给未来长任务")


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
        if user.get("role") != "admin" and project_access(user, entry_project) is None:
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


# ---------- 活动照片批量上传（docs/照片上传协议.md） ----------
def _photo_shot_date(raw: str, *, required: bool = True) -> str:
    """校验拍摄日期：必填、YYYY-MM-DD、不晚于今天。返回原字符串。"""
    text = str(raw or "").strip()
    if not text:
        if required:
            raise ApiError(400, "VALIDATION", "拍摄日期必填",
                           "格式 YYYY-MM-DD，例如 2026-11-10")
        return datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ApiError(400, "VALIDATION", f"拍摄日期格式应为 YYYY-MM-DD：{text}")
    if day > datetime.now(TZ).date():
        raise ApiError(400, "VALIDATION", f"拍摄日期不能晚于今天：{text}")
    return text


def _check_photo_pixels(name: str, data: bytes) -> None:
    """只读图片头校验像素数，不解码整图。

    ``Image.open`` 只解析头；随后立刻 ``verify()`` 并关闭，避免把整张图
    decode 进内存（压缩比极高的小文件可以解出巨图，即解压炸弹）。
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            img.verify()
    except Exception:
        raise ApiError(400, "NOT_AN_IMAGE", f"图片无法解析：{name}",
                       "文件可能损坏，或扩展名与实际格式不符")
    if width * height > _PHOTO_MAX_PIXELS:
        raise ApiError(
            400, "IMAGE_TOO_LARGE",
            f"图片像素过大：{name}（{width}×{height}，上限 "
            f"{_PHOTO_MAX_PIXELS // 1_000_000} 百万像素）",
            "先缩小图片再上传")


def _photo_payloads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    """把上传文件读成 [(原文件名, 字节)]，顺带做张数/扩展名/大小/像素校验。"""
    if not files:
        raise ApiError(400, "VALIDATION", "没有收到照片文件")
    if len(files) > _PHOTO_MAX_FILES:
        raise ApiError(400, "TOO_MANY_FILES",
                       f"单次最多 {_PHOTO_MAX_FILES} 张，本次 {len(files)} 张",
                       "分批上传，或打 zip 走 /api/sw/photos/upload-archive")
    out: list[tuple[str, bytes]] = []
    total = 0
    for item in files:
        name = str(item.filename or "")
        suffix = Path(name).suffix.lower()
        if suffix not in _PHOTO_EXTS:
            raise ApiError(400, "NOT_AN_IMAGE",
                           f"不是图片：{name or '（无文件名）'}",
                           "只收 jpg/jpeg/png/gif/bmp/webp")
        data = item.file.read()
        if len(data) > _PHOTO_MAX_BYTES:
            raise ApiError(413, "FILE_TOO_LARGE",
                           f"单张超过 50 MB：{name}（{len(data) // 1024 // 1024} MB）")
        total += len(data)
        if total > _PHOTO_MAX_TOTAL_BYTES:
            raise ApiError(413, "PAYLOAD_TOO_LARGE",
                           f"单次上传总量超过 "
                           f"{_PHOTO_MAX_TOTAL_BYTES // 1024 // 1024} MB",
                           "分批上传，或打 zip 走 /api/sw/photos/upload-archive")
        _check_photo_pixels(name, data)
        out.append((name, data))
    return out


def _match_activity_dir(dirname: str, names: list[str]) -> str | None:
    """压缩包一级目录名 → 活动目录名：先精确匹配，再按前缀匹配。"""
    raw = str(dirname or "").strip().strip("/")
    if not raw:
        return None
    if raw in names:
        return raw
    hits = [n for n in names if n.startswith(raw)]
    return hits[0] if hits else None


def _project_level_suggestion(filename: str, project_dir: Path) -> dict | None:
    """不命中任何清单项时给一条改名建议。

    只做粗猜：把关键词逐个往文件名上试，命中最多关键词的那项优先；
    一个都猜不中就给通用格式，让调用方自己填清单项名。
    """
    name = str(filename or "")
    best, best_hits = None, 0
    for item in _project_checks.PROJECT_CHECKLIST:
        hits = sum(1 for kw in item["keywords"] if kw in name)
        if hits > best_hits:
            best, best_hits = item, hits
    stem = Path(name).stem
    suffix = Path(name).suffix or ".docx"
    stamp = datetime.now().strftime("%Y%m%d")
    if best:
        # 建议名 = {清单项关键词}-{原标题}-{日期}，关键词放最前便于自动匹配
        head = best["keywords"][0]
        return {
            "item": best["no"],
            "item_name": best["name"],
            "rename_to": f"{head}-{stem}-{stamp}{suffix}",
            "keywords": list(best["keywords"]),
        }
    return {
        "item": None,
        "item_name": None,
        "rename_to": f"清单项名称-{stem}-{stamp}{suffix}",
        "keywords": [],
        "hint": "文件名未命中任何清单项关键词，请按「清单项关键词-内容-日期」命名",
    }


@app.post("/api/sw/project-files/upload")
def api_sw_project_file_upload(
    project: str = Form(...),
    file: UploadFile = File(...),
    user=Depends(current_user),
    conn=Depends(db),
):
    """上传项目级文件（不属于任何活动的资料）。

    二元判断：命中清单项就正常入库并回报归到哪一项；不命中就落到 其他/，
    并返回规范命名建议。**不拒绝、不猜、不自动分类** —— 用户上传是为了
    存住和能搜到，认不出是什么不该挡人。
    """
    meta = _sw_project_meta(project)
    check_project(user, meta["name"], write=True)
    name = _sw_project_meta(meta["name"])  # 归一后重取，拿真实目录名
    project_dir = _projects._project_dir(name["name"])
    filename = activity_upload_filename(file.filename or "")

    root = project_dir
    # 命中清单项 → 落 报告/；否则落 其他/
    prefix_dirs = {"方案", "协议", "报告", "其他"}
    matched_item = None
    for item in _project_checks.PROJECT_CHECKLIST:
        if any(kw in filename for kw in item["keywords"]):
            matched_item = item
            break
    target_leaf = "报告" if matched_item else "其他"
    target_dir = root / target_leaf
    target_dir.mkdir(parents=True, exist_ok=True)

    dest = target_dir / filename
    if dest.exists():
        raise ApiError(409, "DUPLICATE_NAME", f"同名文件已存在：{filename}")

    max_mb = int(SETTINGS.upload.get("max_mb", 50))
    max_bytes = max_mb * 1024 * 1024
    size = 0
    try:
        with open(dest, "xb") as fh:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    dest.unlink(missing_ok=True)
                    raise ApiError(413, "FILE_TOO_LARGE", f"文件超过 {max_mb} MB")
                fh.write(chunk)
    except FileExistsError:
        raise ApiError(409, "DUPLICATE_NAME", f"同名文件已存在：{filename}")

    rel = dest.relative_to(root).as_posix()
    out = {
        "stored": rel,
        "size": size,
        "matched": None,
    }
    if matched_item:
        out["matched"] = {"item": matched_item["no"], "name": matched_item["name"]}
    else:
        out["suggestion"] = _project_level_suggestion(filename, project_dir)

    audit(conn, user["id"], "upload_project_file", project=meta["name"],
          target=rel, detail={"size": size, "matched": bool(matched_item)},
          ip="api")
    return out


@app.post("/api/sw/photos/precheck")
def api_sw_photos_precheck(body: dict, user=Depends(current_user)):
    """body: {project, activity, hashes: [sha256...]}

    只比对不落盘。返回 {new: int, duplicated: [hash...], duplicate_batch: str|None}
    """
    data = body if isinstance(body, dict) else {}
    project = str(data.get("project") or "").strip()
    activity = str(data.get("activity") or "").strip()
    if not project or not activity:
        raise ApiError(400, "VALIDATION", "project 与 activity 都必填")
    act_dir, _meta = _sw_activity_dir(project, activity, user, write=False)

    hashes = data.get("hashes") or []
    if not isinstance(hashes, list):
        raise ApiError(400, "VALIDATION", "hashes 必须是字符串数组")
    checked = _photos.dedup_check(_photos.load_meta(act_dir),
                                  [str(h) for h in hashes])

    # 带 shot_date + scene 时顺带算一次 L3，前端可在上传前就提示整批重复
    duplicate_batch = None
    shot_date = str(data.get("shot_date") or "").strip()
    if shot_date:
        try:
            _photos.target_name(shot_date, 1)
        except ValueError as exc:
            raise ApiError(400, "VALIDATION", str(exc))
        duplicate_batch = _photos.batch_duplicate(
            _photos.load_meta(act_dir), shot_date, str(data.get("scene") or ""))

    return {"new": len(checked["new"]), "duplicated": checked["duplicated"],
            "duplicate_batch": duplicate_batch}


@app.post("/api/sw/photos/upload")
def api_sw_photos_upload(
        project: str = Form(...), activity: str = Form(...),
        shot_date: str = Form(""), scene: str = Form(""), note: str = Form(""),
        allow_duplicate: bool = Form(False), compress: bool = Form(True),
        files: list[UploadFile] = File(...),
        user=Depends(current_user), conn=Depends(db)):
    """多文件批量上传。最多 200 张，单张 50MB；返回协议 §3.1 的结构。"""
    act_dir, meta = _sw_activity_dir(project, activity, user, write=True)
    day = _photo_shot_date(shot_date)
    payloads = _photo_payloads(files)

    result = _photos.save_photos(
        act_dir, payloads, day, scene=scene, note=note,
        uploaded_by=str(user.get("real_name") or user.get("username") or ""),
        compress=bool(compress), allow_duplicate=bool(allow_duplicate))

    if result.get("duplicate_batch"):
        raise ApiError(
            409, "DUPLICATE_BATCH",
            f"24 小时内已上传过同活动、同日期、同场景的批次：{result['duplicate_batch']}",
            "确认要再传一遍请带 allow_duplicate=true")

    if result["saved"]:
        audit(conn, user["id"], "upload_photos", project=meta["name"],
              target=result["batch_id"],
              detail={"activity": activity, "saved": result["saved"],
                      "skipped": len(result["skipped"]),
                      "shot_date": day, "scene": scene})

    return {
        "ok": True,
        "batch_id": result["batch_id"],
        "activity": activity,
        "received": len(payloads),
        "saved": result["saved"],
        "skipped": result["skipped"],
        "compressed": result["saved"] if compress else 0,
        "paths": result["paths"],
        "total_bytes": result["total_bytes"],
    }


@app.post("/api/sw/photos/upload-archive")
def api_sw_photos_upload_archive(
        project: str = Form(...), archive: UploadFile = File(...),
        shot_date: str = Form(""), allow_duplicate: bool = Form(False),
        user=Depends(current_user), conn=Depends(db)):
    """zip 上传。一级目录名 = 活动目录名（允许前缀匹配，如 '11' 匹配 '11-共治行动-第一期'）。"""
    meta = _sw_project_meta(project)
    if user.get("role") not in ("admin", "editor"):
        raise ApiError(403, "FORBIDDEN", "无写权限（需 editor/admin）")
    check_project(user, meta["name"], write=True)

    day = _photo_shot_date(shot_date, required=False)
    fname = str(archive.filename or "")
    if Path(fname).suffix.lower() != ".zip":
        raise ApiError(400, "ARCHIVE_UNSUPPORTED",
                       f"只支持 zip：{fname or '（无文件名）'}",
                       "7z/rar 请先解压，或改用 /api/sw/photos/upload")

    # 压缩包先落临时文件：边读边累计大小，避免把 1GB 一次性读进内存
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = Path(tmp.name)
    try:
        size = 0
        while True:
            chunk = archive.file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > _ARCHIVE_MAX_BYTES:
                raise ApiError(
                    413, "PAYLOAD_TOO_LARGE",
                    f"压缩包超过 {_ARCHIVE_MAX_BYTES // 1024 // 1024} MB",
                    "请拆分后分批上传")
            tmp.write(chunk)
        tmp.close()

        try:
            zf = zipfile.ZipFile(tmp_path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise ApiError(400, "ARCHIVE_UNSUPPORTED", f"压缩包无法读取：{exc}")

        act_root = Path(_projects.PROJECTS_ROOT) / meta["name"] / "活动"
        names = (sorted(p.name for p in act_root.iterdir() if p.is_dir())
                 if act_root.is_dir() else [])

        groups: dict[str, list[tuple[str, bytes]]] = {}
        unmatched: dict[str, int] = {}
        total_images = 0
        read_total = 0
        with zf:
            infos = zf.infolist()
            # 预算检查必须挡在任何 zf.read() 之前：中央目录已给出每条的解压后大小
            if len(infos) > _ARCHIVE_MAX_ENTRIES:
                raise ApiError(
                    400, "ARCHIVE_TOO_MANY_ENTRIES",
                    f"压缩包条目过多：{len(infos)} 个，上限 {_ARCHIVE_MAX_ENTRIES}",
                    "请按活动拆分压缩包")
            declared = sum(max(0, info.file_size) for info in infos)
            if declared > _ARCHIVE_MAX_UNCOMPRESSED:
                raise ApiError(
                    400, "ARCHIVE_TOO_LARGE",
                    f"压缩包解压后约 {declared // 1024 // 1024} MB，"
                    f"超过 {_ARCHIVE_MAX_UNCOMPRESSED // 1024 // 1024} MB",
                    "请按活动拆分压缩包")
            for info in infos:
                # 单条压缩比异常 + 体积不小 → 典型解压炸弹特征，直接拒绝
                if (info.file_size > _ARCHIVE_RATIO_MIN_BYTES
                        and info.compress_size > 0
                        and info.file_size / info.compress_size > _ARCHIVE_RATIO_LIMIT):
                    raise ApiError(
                        400, "ARCHIVE_SUSPICIOUS",
                        f"压缩包条目异常：{info.filename}"
                        f"（压缩比 {info.file_size // max(info.compress_size, 1)}:1）",
                        "疑似压缩炸弹，请检查文件后重新打包")

            for info in infos:
                if info.is_dir():
                    continue
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
                if not parts or parts[0] == "__MACOSX":
                    continue
                leaf = parts[-1]
                if leaf.startswith(".") or Path(leaf).suffix.lower() not in _PHOTO_EXTS:
                    continue
                if info.file_size > _PHOTO_MAX_BYTES:
                    raise ApiError(413, "FILE_TOO_LARGE",
                                   f"单张超过 50 MB：{info.filename}")
                total_images += 1
                dirname = parts[0] if len(parts) > 1 else ""
                matched = _match_activity_dir(dirname, names)
                if not matched:
                    key = dirname or "（根目录）"
                    unmatched[key] = unmatched.get(key, 0) + 1
                    continue
                data = zf.read(info)
                if len(data) > _PHOTO_MAX_BYTES:      # 声明大小可能造假，读后再核一次
                    raise ApiError(413, "FILE_TOO_LARGE",
                                   f"单张超过 50 MB：{info.filename}")
                # 实际字节也累计一遍：声明大小可能造假，读超预算立刻中止
                read_total += len(data)
                if read_total > _ARCHIVE_MAX_UNCOMPRESSED:
                    raise ApiError(
                        400, "ARCHIVE_TOO_LARGE",
                        f"解压累计超过 "
                        f"{_ARCHIVE_MAX_UNCOMPRESSED // 1024 // 1024} MB，已中止")
                groups.setdefault(matched, []).append((leaf, data))
    finally:
        with contextlib.suppress(Exception):
            tmp.close()
        with contextlib.suppress(OSError):
            tmp_path.unlink()

    if total_images == 0:
        raise ApiError(400, "ARCHIVE_EMPTY", "压缩包里没有图片")
    if not groups:
        raise ApiError(400, "NO_MATCHED_ACTIVITY",
                       "压缩包内目录都匹配不上活动",
                       "一级目录名用活动目录名或它的前缀，如 11-共治行动-第一期")

    # 先统一做 L3 预检，避免写了一半才报重复
    if not allow_duplicate:
        for name in groups:
            prev = _photos.batch_duplicate(
                _photos.load_meta(act_root / name), day, "")
            if prev:
                raise ApiError(409, "DUPLICATE_BATCH",
                               f"{name} 24 小时内已上传过同日期批次：{prev}",
                               "确认要再传一遍请带 allow_duplicate=true")

    matched_count: dict[str, int] = {}
    total_saved = 0
    for name, items in groups.items():
        result = _photos.save_photos(
            act_root / name, items, day,
            uploaded_by=str(user.get("real_name") or user.get("username") or ""),
            allow_duplicate=bool(allow_duplicate))
        matched_count[name] = result["saved"]
        total_saved += result["saved"]

    if total_saved:
        audit(conn, user["id"], "upload_photos_archive", project=meta["name"],
              target=fname,
              detail={"matched": matched_count, "unmatched": unmatched,
                      "shot_date": day})

    return {"ok": True, "matched": matched_count, "unmatched": unmatched,
            "total_saved": total_saved}


@app.get("/api/dashboard")
def api_dashboard(user=Depends(current_user)):
    """仪表盘：项目活动进度 + 最近 10 个有改动的活动。"""
    projects: list[dict] = []
    recent: list[dict] = []

    for item in _projects.list_projects():
        if not project_allowed(user, item):
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
                "status": str(activity_yaml_data(act_dir).get("status") or ""),
                "mtime": datetime.fromtimestamp(
                    _activity_mtime(act_dir), TZ).isoformat(),
            })

        projects.append({
            "name": name,
            "short": project_short(item),
            "activity_total": len(dirs),
            "activity_complete": complete,
        })

    recent.sort(key=lambda x: x["mtime"], reverse=True)
    return {"projects": projects, "recent": recent[:10]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SETTINGS.server["host"], port=int(SETTINGS.server["port"]))
