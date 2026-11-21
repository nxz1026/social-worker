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
import io
import json
import logging
import mimetypes
import os
import re
import socket
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import yaml
from fastapi import (BackgroundTasks, Depends, FastAPI, HTTPException, Request)
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
# 必须用包名 api.settings 而非裸名 settings：api/ 在 sys.path 里，
# 裸名与 api.settings 会被 Python 当成**两个不同模块**各执行一次，
# 产出两个 SETTINGS 单例（实测 is 判断为 False、dataclass 类型也不同）。
# 改一份不影响另一份，是隐性故障源。
from api.settings import SETTINGS  # noqa: E402
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
    _ALIAS_CACHE, _API_OCR_EXTS, _COGNEE_CACHE, _COGNEE_TTL, _SUGGEST_CACHE)

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
# engine 是仓库根下的包，不在 scripts 里
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import name_parse as _np  # noqa: E402
from engine import activity as _activity  # noqa: E402
from engine import caption as _caption  # noqa: E402
from engine import checks as _checks  # noqa: E402
from engine import projects as _projects  # noqa: E402
from engine import cognee_index as _cognee_index  # noqa: E402
from engine import fill as _fill  # noqa: E402
from engine import llm as _llm  # noqa: E402
from engine import readme as _readme  # noqa: E402

app = FastAPI(title="社工文档服务", version="0.1")

log = logging.getLogger("socialwork.api")
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
from api.routes import agent as _r_agent
from api.routes import misc as _r_misc  # noqa: E402
# 测试直接按名字调端点函数（tests/test_api_web.py），这里保留同名入口。
from api.routes.projects import (api_check_project, api_create_project,  # noqa: E402,F401
                                 api_get_project, api_list_activities,
                                 api_list_projects, api_project_code,
                                 api_update_project)

app.include_router(_r_assets.router)
app.include_router(_r_agent.router)
app.include_router(_r_misc.router)
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
    except Exception as exc:
        # 不阻断启动（认证只依赖 key_hash），但**必须留痕**：
        # 原来这里 `except Exception: pass` 零日志，线上缺列无人知（审计 C-07）。
        # 缺列的后果是「查看 API Key 明文」报 SQL 错误，属于要修但不必停服的问题。
        log.warning("启动迁移失败（api_keys.secret_enc 未补）：%s: %s",
                    type(exc).__name__, exc)


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














if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SETTINGS.server["host"], port=int(SETTINGS.server["port"]))
