#!/usr/bin/env python3
"""社工文档服务 API（FastAPI）。

认证：session token（PG sessions 表，Header: x-token）
权限：role(admin/editor/viewer) + user_projects 项目隔离
审计：写操作落 audit_log
归档：调 scripts/archive.py
检索：调 scripts/cognee.py search
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
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
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from settings import SETTINGS
import status as status_mod

ROOT = SETTINGS.root
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import manifest as _mf  # noqa: E402
import error_queue as _eq  # noqa: E402
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
    """调上游 LLM 生成文书。

    地址 / 模型 / key 的优先级：环境变量 > config/api.yaml 的 llm 段 > 服务端 .env。
    返回 (正文, finish_reason)。LongCat 是推理模型，reasoning 与正文共用
    max_tokens，预算给小了正文会被截断，默认值见 config/api.yaml。
    """
    max_tokens = max_tokens or int(SETTINGS.llm["max_tokens"])
    import urllib.request
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    endpoint = (os.environ.get("LLM_ENDPOINT")
                or SETTINGS.llm.get("endpoint") or env.get("LLM_ENDPOINT", ""))
    key = (os.environ.get("LLM_API_KEY")
           or SETTINGS.llm.get("api_key") or env.get("LLM_API_KEY", ""))
    model = (os.environ.get("LLM_MODEL")
             or SETTINGS.llm.get("model") or env.get("LLM_MODEL", "")).split("/")[-1]
    if not endpoint or not key or not model:
        raise RuntimeError(
            "LLM 未配置：在 config/api.yaml 的 llm 段或服务端 .env 填 endpoint / model / api_key")
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(SETTINGS.llm["temperature"]),
            "max_tokens": max_tokens}
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(
                req, timeout=int(SETTINGS.llm["timeout_seconds"])) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        # 区分超时与其它错误，便于上层返回 504/500
        # urllib 超时会包成 URLError(reason=socket.timeout)
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(exc).lower():
            raise TimeoutError(str(exc)) from exc
        raise
    choice = (data.get("choices") or [{}])[0]
    finish = choice.get("finish_reason") or ""
    content = (choice.get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        usage = data.get("usage") or {}
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        raise RuntimeError(
            f"上游未返回正文（finish_reason={finish}, reasoning_tokens={reasoning}）")
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


def _load_manifest() -> dict:
    try:
        return _mf.load(_manifest_file())
    except _mf.ManifestCorrupt as exc:
        raise HTTPException(500, f"台账损坏，请先修复：{exc}")


def _save_manifest(manifest: dict) -> None:
    _mf.save(_manifest_file(), manifest)


def _update_manifest(mutator):
    """锁内读-改-写台账，避免与 scan-inbox / reconcile 互相覆盖。"""
    try:
        with _mf.lock(_manifest_file()):
            data = _load_manifest()
            out = mutator(data)
            _save_manifest(data)
            return out
    except _mf.ManifestCorrupt as exc:
        raise HTTPException(500, f"台账损坏，请先修复：{exc}")


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
    """给待确认文件推荐项目：文件名优先，判不了读正文（与归档同一套规则）。

    只在有待确认文件时才会加载 scripts/，避免 API 启动时把转换依赖拖进来。
    """
    try:
        st = path.stat()
        key = (str(path), st.st_mtime, st.st_size)
        if key in _SUGGEST_CACHE:
            return _SUGGEST_CACHE[key]
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import archive as archive_mod
        import classify_rules as cr_mod
        rules = cr_mod.load_rules(ROOT / "config")
        projects = sorted(rules["projects"]) if isinstance(rules.get("projects"), dict) else None
        project, decision = archive_mod.decide_project(path.name, path, rules, projects, None)
        out = (str(project or ""), "规则判定" if decision else "")
        if len(_SUGGEST_CACHE) > 500:
            _SUGGEST_CACHE.clear()      # 待确认文件不多；给个上限防止长跑无界增长
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


def _pending_items() -> list[dict]:
    """`_待确认` 里的原件清单（排除 `_md` 转换产物；已归档的标 stale）。"""
    manifest = _load_manifest()
    done_by_sha: dict[str, dict] = {}
    for rec in (manifest.get("files") or {}).values():
        if isinstance(rec, dict) and rec.get("status") == "done" and rec.get("sha256"):
            done_by_sha.setdefault(str(rec["sha256"]), rec)
    reasons: dict[str, str] = {}
    for r in _audit_rows():
        f = str(r.get("file") or "")
        why = r.get("note") or r.get("error")
        if f and why:
            reasons.setdefault(f, str(why)[:200])

    bases = [
        ROOT / "sources" / "_待确认",
        ROOT / "vault-huadu" / "md" / "_待确认",
        ROOT / "vault-huimin" / "md" / "_待确认",
    ]
    items: list[dict] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "_md" and not d.startswith(".")]
            for fn in sorted(files):
                if fn.startswith("."):
                    continue
                full = Path(root) / fn
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                sha = status_mod.sha256_of(full)
                key = sha or str(full)
                if key in seen:
                    continue
                seen.add(key)
                rec = done_by_sha.get(sha) if sha else None
                suggest, why = ("", "") if rec else _suggest_project(full)
                items.append({
                    "name": fn,
                    "path": str(full.relative_to(ROOT)),
                    "size": size,
                    "sha256": sha,
                    "status": "stale" if rec else "pending",
                    "reason": (f"已归档到 {rec.get('project') or '?'}/{rec.get('category') or '?'}"
                               if rec else reasons.get(fn, "")),
                    "suggested_project": suggest,
                    "suggested_reason": why,
                })
    items.sort(key=lambda x: (x["status"] != "pending", x["name"]))
    return items


def _recent_failures(limit: int = 20) -> list[dict]:
    """今天审计日志里的失败项；跳过后来已归档/删除的。"""
    rows = _audit_rows()
    manifest = _load_manifest()
    done_shas = {str(r.get("sha256")) for r in (manifest.get("files") or {}).values()
                 if isinstance(r, dict) and r.get("status") == "done" and r.get("sha256")}
    by_name = {str(r.get("name")): tok for tok, r in (manifest.get("files") or {}).items()
               if isinstance(r, dict) and r.get("name")}
    out = []
    for r in rows:
        if r.get("status") not in ("error", "unsupported", "failed"):
            continue
        sha = str(r.get("sha256") or "")
        if sha and sha in done_shas:
            continue
        # 同一天后面有删除记录 ⇒ 已处理
        later_delete = any(
            d.get("status") == "deleted"
            and str(d.get("ts") or "") > str(r.get("ts") or "")
            and ((sha and str(d.get("sha256") or "") == sha)
                 or (not sha and d.get("file") == r.get("file")))
            for d in rows
        )
        if later_delete:
            continue
        out.append({
            "ts": r.get("ts"),
            "file": r.get("file"),
            "status": r.get("status"),
            "error": str(r.get("error") or r.get("status") or "")[:300],
            "token": by_name.get(str(r.get("file") or "")),
        })
    out.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return out[:limit]


def _error_queue() -> list[dict]:
    p = ROOT / "state" / "errors" / "open.yaml"
    if not p.is_file():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except Exception:
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


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
            "feishu": st["feishu"]["state"],
            "index": st["index"]["state"],
        }
    return {"total": len(out), "items": out}


@app.get("/api/docs/{token}")
def get_doc(token: str, user=Depends(current_user)):
    manifest = _load_manifest()
    e = (manifest.get("files") or {}).get(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    check_project(user, e.get("project"))
    if e.get("status") == "deleted":
        raise HTTPException(404, "文档已删除")
    content = ""
    md_path = _mf.entry_path(ROOT, e)
    if md_path and md_path.is_file():
        content = md_path.read_text(encoding="utf-8")
    return {"token": token, "name": e.get("name"), "project": e.get("project"),
            "category": e.get("category"), "content": content,
            "original": e.get("original"),
            "detected_project": e.get("detected_project")}


@app.get("/api/docs/{token}/status")
def get_doc_status(token: str, user=Depends(current_user)):
    """源文件 / md / 飞书 / 索引 / 关系五态。"""
    e = _manifest_entry(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    check_project(user, str(e.get("project") or ""))
    ds = _dataset_for(str(e.get("project") or ""))
    return status_mod.doc_status(
        ROOT, token, e, _dataset_entries(ds), _feishu_template(),
        roster=_roster(str(e.get("project") or "")),
    )


@app.get("/api/docs/{token}/source")
def download_source(token: str, request: Request,
                    user=Depends(current_user), conn=Depends(db)):
    """下载原件。按项目鉴权，逐次写审计。"""
    e = _manifest_entry(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    check_project(user, str(e.get("project") or ""))
    rel = _mf.normalize_rel(str(e.get("original") or ""))
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
                and rec.get("status") in {"done", "pending_review"}):
            return tok, rec
    return None


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
               "--no-cognify", "--only-self", "--scan-timeout", str(_archive_timeout())]
        if project:
            cmd += ["--project", project]
        if category:
            cmd += ["--category", category]
        rc, out, err, timed_out = run_archive(cmd, _archive_timeout())
        receipt = _parse_archive_receipt(out)

        if timed_out:
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
    md_path = _mf.entry_path(ROOT, e)
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
               conn=Depends(db)):
    manifest = _load_manifest()
    e = (manifest.get("files") or {}).get(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    check_project(user, e.get("project"))
    try:
        proc = subprocess.run(
            ["python3", str(SCRIPTS / "delete.py"), token, "--mode", "trash", "--yes"],
            cwd=str(ROOT), capture_output=True, text=True,
            timeout=int(SETTINGS.tasks["delete_timeout_seconds"]))
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "删除超时（后台可能仍在跑，请稍后查回收站）")
    audit(conn, user["id"], "delete", e.get("project"), e.get("name"),
          {"mode": "trash", "returncode": proc.returncode}, request.client.host)
    if proc.returncode != 0:
        raise HTTPException(500, f"删除失败：{proc.stdout[-300:]}")
    return {"ok": True, "output": proc.stdout[-500:]}


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


@app.get("/api/docs/{token}/relations")
def get_doc_relations(token: str, user=Depends(current_user)):
    """实体（按类型分组）+ 反链（谁提到我 / 谁和我共享实体）。只读。"""
    e = _manifest_entry(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    project = str(e.get("project") or "")
    check_project(user, project)
    manifest = _load_manifest()
    docs = [(t, str(r.get("name") or ""), str(r.get("md") or ""))
            for t, r in (manifest.get("files") or {}).items()
            if isinstance(r, dict) and r.get("status") != "deleted"
            and r.get("project") in user["projects"]]
    md_rel = str(e.get("md") or "")
    text = ""
    if md_rel and (ROOT / md_rel).is_file():
        try:
            text = (ROOT / md_rel).read_text(encoding="utf-8")
        except OSError:
            text = ""
    rel = status_mod.relations_status(text, _roster(project))
    bl = status_mod.backlinks(ROOT, token, e, docs)
    return {"token": token, "count": rel["count"], "by_type": rel["by_type"],
            "entities": rel["entities"], "backlink_count": bl["count"],
            "backlinks": bl["backlinks"], "shared": bl["shared"],
            "roster": _roster_summary(project)}


@app.post("/api/meta/relink")
def relink_entities(request: Request, body: dict | None = None,
                    user=Depends(require_admin), conn=Depends(db)):
    """按当前 entities.yaml 给已有 md 补 `[[实体]]`，再重建索引。

    名册改了以后 `convert-md.py` 不会自动重跑（它只挑源文件比 md 新的），
    所以给关系页一个「重建实体链接」按钮，走这个端点。
    """
    body = body or {}
    project = str(body.get("project") or "").strip()
    dry = bool(body.get("dry_run"))
    cmd = ["python3", str(SCRIPTS / "convert-md.py"), "--relink-only"]
    if dry:
        cmd.append("--dry-run")
    if project in ALL_PROJECTS:
        cmd += ["--project", project]
    elif project:
        raise HTTPException(400, f"未知项目 {project}")
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                              timeout=600)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "补链超时")
    if proc.returncode != 0:
        raise HTTPException(500, f"补链失败：{(proc.stdout or proc.stderr)[-300:]}")
    if dry:
        return {"ok": True, "dry_run": True, "output": (proc.stdout or "")[-800:],
                "reconcile": []}
    targets = [project] if project in ALL_PROJECTS else list(ALL_PROJECTS)
    out = []
    failed = []
    for p in targets:
        rp = _run_reconcile(_dataset_for(p),
                            int(SETTINGS.tasks["reconcile_timeout_seconds"]))
        out.append({"project": p, "rc": rp.returncode,
                    "output": (rp.stdout or "")[-300:]})
        if rp.returncode != 0:
            failed.append(p)
    audit(conn, user["id"], "relink", project or None, "entities.yaml",
          {"projects": targets, "reconcile_failed": failed}, request.client.host)
    warn = (f"{'、'.join(failed)} 重建索引失败（15 分钟定时器会补）" if failed else "")
    return {"ok": True, "output": (proc.stdout or "")[-800:], "reconcile": out,
            "warning": warn}


@app.post("/api/docs/{token}/feishu")
def feishu_reupload(token: str, request: Request,
                    user=Depends(require_admin), conn=Depends(db)):
    """重传原件：旧副本先改名，新副本传成功后再把旧副本移入飞书回收站。

    顺序是「改名旧 → 传新 → 删旧」：如果先删旧、传新又失败，台账就指向一个
    已删除的 file_token，等于把唯一的飞书副本弄丢。改名失败则直接中止。
    """
    e = _manifest_entry(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    project = str(e.get("project") or "")
    category = str(e.get("category") or "")
    check_project(user, project)
    orig = str(e.get("original") or "")
    if not orig or not (ROOT / orig).is_file():
        raise HTTPException(400, "原件不存在，无法上传")
    flow = _flow()
    label = str(((flow.get("projects") or {}).get(project) or {}).get("label") or project)
    folder = str((((flow.get("category_folders") or {}).get(label) or {})
                  .get(category) or ""))
    if not folder:
        raise HTTPException(400, f"feishu-flow.yaml 里找不到 {label}/{category} 的目录")

    old_ft = str(e.get("feishu_token") or "")
    ftype = str(e.get("feishu_type") or "file")
    old_stem = Path(orig).stem
    renamed_to = ""
    if old_ft:
        # 飞书标题有长度上限；先截断再加后缀，避免长文件名直接失败
        renamed_to = f"{old_stem[:150]}.旧-{datetime.now(TZ).strftime('%Y%m%d%H%M%S')}"
        _lark(["drive", "+update-title", "--token", old_ft, "--type", ftype,
               "--title", renamed_to])

    try:
        data = _lark(["drive", "+upload", "--file", orig, "--folder-token", folder])
    except HTTPException:
        # 新副本没传上去 ⇒ 把旧副本名字改回来，保持原样
        if old_ft and renamed_to:
            try:
                _lark(["drive", "+update-title", "--token", old_ft, "--type", ftype,
                       "--title", old_stem])
            except HTTPException:
                pass
        raise
    ft = str((data.get("data") or {}).get("file_token") or data.get("file_token") or "")
    if not ft:
        raise HTTPException(502, "飞书没有返回 file_token")

    # 先落台账再删旧：万一写台账失败，旧副本还只是改了名（链接仍可用），
    # 不会出现「旧副本已进回收站、台账还指着它」的坏状态。
    def _set_new(data: dict) -> None:
        rec = (data.get("files") or {}).setdefault(token, {})
        rec["feishu_token"] = ft
        rec["feishu_uploaded_at"] = datetime.now(TZ).isoformat(timespec="seconds")

    _update_manifest(_set_new)

    old_trashed = False
    if old_ft:
        try:
            _lark(["drive", "+delete", "--file-token", old_ft, "--type", ftype, "--yes"])
            old_trashed = True
        except HTTPException:
            # 新副本已就绪；旧副本没删掉不影响使用，但要让调用方知道
            old_trashed = False

    audit(conn, user["id"], "feishu_upload", project, e.get("name"),
          {"file_token": ft, "old_token": old_ft, "old_trashed": old_trashed},
          request.client.host)
    return {"ok": True, "file_token": ft, "old_trashed": old_trashed,
            "renamed_to": renamed_to}


@app.delete("/api/docs/{token}/feishu")
def feishu_delete(token: str, request: Request,
                  user=Depends(require_admin), conn=Depends(db)):
    """删掉台账记的飞书副本（本地原件 / md / 索引不动）。"""
    e = _manifest_entry(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    project = str(e.get("project") or "")
    check_project(user, project)
    ft = str(e.get("feishu_token") or "")
    if not ft:
        raise HTTPException(400, "台账里没有飞书副本")
    _lark(["drive", "+delete", "--file-token", ft, "--type",
           str(e.get("feishu_type") or "file"), "--yes"])

    def _clear(data: dict) -> None:
        rec = (data.get("files") or {}).get(token)
        if isinstance(rec, dict):
            rec.pop("feishu_token", None)
            rec.pop("feishu_uploaded_at", None)

    _update_manifest(_clear)
    audit(conn, user["id"], "feishu_delete", project, e.get("name"),
          {"file_token": ft}, request.client.host)
    return {"ok": True}


@app.post("/api/docs/{token}/reindex")
def reindex_doc(token: str, request: Request,
                user=Depends(require_admin), conn=Depends(db)):
    """按项目数据集重建索引（reconcile：md → Cognee，幂等）。"""
    e = _manifest_entry(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    project = str(e.get("project") or "")
    check_project(user, project)
    ds = _dataset_for(project)
    proc = _run_reconcile(ds, int(SETTINGS.tasks["reconcile_timeout_seconds"]))
    audit(conn, user["id"], "reindex", project, e.get("name"),
          {"dataset": ds, "returncode": proc.returncode}, request.client.host)
    if proc.returncode != 0:
        raise HTTPException(500, f"重建失败：{proc.stdout[-300:] or proc.stderr[-300:]}")
    return {"ok": True, "dataset": ds, "output": proc.stdout[-800:]}


@app.post("/api/pending/archive")
def pending_archive(body: dict, request: Request,
                    user=Depends(require_admin), conn=Depends(db)):
    """把 `_待确认` 里的文件重新归档（指定项目），成功后原件移出兜底目录。"""
    path = _safe_pending_path(str(body.get("path") or ""))
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    project = str(body.get("project") or "").strip()
    if project and project not in ALL_PROJECTS:
        raise HTTPException(400, f"未知项目 {project}")
    action = str(body.get("action") or "").strip()
    cmd = ["python3", str(SCRIPTS / "archive.py"), "--file", str(path),
           "--no-cognify", "--scan-timeout", str(_archive_timeout())]
    if project:
        cmd += ["--project", project]
    if action in ("archive_only", "ocr", "vision"):
        cmd += ["--action", action]
    rc, stdout, stderr, timed_out = run_archive(cmd, _archive_timeout())
    if timed_out:
        audit(conn, user["id"], "pending_archive", project or None, path.name,
              {"returncode": None, "timeout": True}, request.client.host)
        raise HTTPException(504, "归档超时（已终止进程组，文件仍在 _待确认）")
    ok = rc == 0
    moved = ""
    if ok:
        # 归档成功后兜底目录里的这份就是残留（旧口径会一直提示「待人工确认」）
        try:
            moved = _trash_file(path)
        except OSError:
            moved = ""
    audit(conn, user["id"], "pending_archive", project or None, path.name,
          {"returncode": rc, "moved_to": moved}, request.client.host)
    out = (stdout or "")[-800:]
    if not ok:
        raise HTTPException(500, f"归档失败：{out or (stderr or '')[-300:]}")
    return {"ok": True, "moved_to": moved, "output": out}


@app.post("/api/pending/delete")
def pending_delete(body: dict, request: Request,
                   user=Depends(require_admin), conn=Depends(db)):
    """丢弃 `_待确认` 里的文件（移到 sources/.trash，可人工找回）。"""
    path = _safe_pending_path(str(body.get("path") or ""))
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    moved = _trash_file(path)
    audit(conn, user["id"], "pending_delete", None, path.name,
          {"moved_to": moved}, request.client.host)
    return {"ok": True, "moved_to": moved}


@app.get("/api/trash")
def list_trash(user=Depends(require_admin)):
    """回收站：vault 的 .trash（删 md）与 sources/.trash（丢弃待确认文件）。"""
    out: list[dict] = []
    for base in (ROOT / "vault-huadu" / ".trash", ROOT / "vault-huimin" / ".trash",
                 ROOT / "sources" / ".trash"):
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if fn.startswith("."):
                    continue
                full = Path(root) / fn
                try:
                    size = full.stat().st_size
                    mtime = datetime.fromtimestamp(full.stat().st_mtime, TZ)
                except OSError:
                    continue
                rel = full.relative_to(ROOT).as_posix()
                parts = Path(rel).parts
                out.append({
                    "path": rel,
                    "name": fn,
                    "size": size,
                    "deleted_at": mtime.isoformat(timespec="seconds"),
                    "kind": "md" if ".trash" in parts and parts[0].startswith("vault-") else "source",
                })
    out.sort(key=lambda x: x["deleted_at"], reverse=True)
    return {"total": len(out), "items": out[:500]}


def _restore_dest_parts(parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    """回收站路径 → (顶层目录, 原相对路径)。布局：<顶层>/.trash/<日期>/<原相对路径>。

    见 delete.py（vault md）与 `_trash_file`（sources 兜底文件）。
    """
    if ".trash" not in parts:
        return None
    idx = parts.index(".trash")
    if idx < 1 or len(parts) <= idx + 2:
        return None
    return parts[idx - 1], parts[idx + 2:]


@app.post("/api/trash/restore")
def trash_restore(body: dict, request: Request,
                  user=Depends(require_admin), conn=Depends(db)):
    """恢复：把 .trash/<日期>/ 里的文件挪回原位，再跑一次对账（飞书一并恢复）。

    返回真实结果：vault md 恢复后看 reconcile 退出码；sources 原件恢复后
    把对应台账条目标成 pending，让下一轮 scan 重新归档。
    """
    rel = str(body.get("path") or "").strip()
    src = (ROOT / rel).resolve()
    try:
        src.relative_to(ROOT.resolve())
    except ValueError:
        raise HTTPException(400, "路径越界")
    if not src.is_file():
        raise HTTPException(404, "回收站里没有这个文件")
    got = _restore_dest_parts(src.parts)
    if not got:
        raise HTTPException(400, "无法推断原位置")
    top, rest = got
    if top not in ("sources", "vault-huadu", "vault-huimin"):
        raise HTTPException(400, f"不认识的回收站位置：{top}")
    dest = ROOT.joinpath(top, *rest)
    if dest.exists():
        raise HTTPException(409, f"原位置已有同名文件：{dest.relative_to(ROOT).as_posix()}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    restored_to = dest.relative_to(ROOT).as_posix()

    reconcile = None
    status = "restored"
    if top.startswith("vault-"):
        project = top.replace("vault-", "")
        if project in ALL_PROJECTS:
            proc = _run_reconcile(_dataset_for(project),
                                  int(SETTINGS.tasks["reconcile_timeout_seconds"]))
            reconcile = {"returncode": proc.returncode,
                         "output": (proc.stdout or "")[-600:]}
            if proc.returncode != 0:
                raise HTTPException(
                    500, "文件已恢复，但索引/飞书对账失败："
                         + (proc.stdout or proc.stderr or "")[-300:])
    elif top == "sources":
        def _mark_pending(data: dict) -> str:
            for tok, rec in (data.get("files") or {}).items():
                if not isinstance(rec, dict):
                    continue
                if _mf.normalize_rel(str(rec.get("original") or "")) == restored_to:
                    rec["status"] = "pending"
                    for key in ("deleted_at", "delete_mode", "error"):
                        rec.pop(key, None)
                    return tok
            return ""

        token = _update_manifest(_mark_pending)
        status = "pending" if token else "orphan"

    audit(conn, user["id"], "restore", top.replace("vault-", "") or None, src.name,
          {"to": restored_to, "status": status}, request.client.host)
    return {"ok": True, "restored_to": restored_to, "status": status,
            "reconcile": reconcile, "output": (reconcile or {}).get("output", "")}


@app.post("/api/trash/purge")
def trash_purge(body: dict, request: Request,
                user=Depends(require_admin), conn=Depends(db)):
    """彻底删除回收站里的一个文件（不可恢复）。"""
    rel = str(body.get("path") or "").strip()
    src = (ROOT / rel).resolve()
    try:
        src.relative_to(ROOT.resolve())
    except ValueError:
        raise HTTPException(400, "路径越界")
    if ".trash" not in src.parts or not src.is_file():
        raise HTTPException(404, "回收站里没有这个文件")
    src.unlink()
    audit(conn, user["id"], "purge", None, src.name, {"path": rel}, request.client.host)
    return {"ok": True}


# ---------- 检索 ----------
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

    import socket
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


# ---------- 用户管理（admin） ----------
@app.get("/api/issues")
def list_issues(user=Depends(require_admin)):
    """待确认 + 今日失败 + 错误队列（admin；含未分类原件路径）。"""
    pending = _pending_items()
    failures = _recent_failures()
    errors = _error_queue()
    return {
        "pending": pending,
        "pending_count": sum(1 for x in pending if x["status"] == "pending"),
        "stale_count": sum(1 for x in pending if x["status"] == "stale"),
        "failures": failures,
        "failure_count": len(failures),
        "error_queue": errors,
        "error_queue_count": len(errors),
    }


@app.post("/api/issues/resolve")
def resolve_issue(body: dict, request: Request,
                  user=Depends(require_admin), conn=Depends(db)):
    """关闭一条错误队列；可选 requeue=true 把文件挪回 inbox 让下一轮重跑。"""
    eid = str(body.get("id") or "").strip()
    file = str(body.get("file") or "").strip()
    if not eid and not file:
        raise HTTPException(400, "需要 id 或 file")
    entry = _eq.find_open(ROOT, eid=eid or None, file=file or None)
    if not entry:
        raise HTTPException(404, "错误队列里没有这一条")
    resolved = _eq.resolve(ROOT, entry["id"]) if entry.get("id") else False
    if not resolved and file:
        resolved = _eq.close_by_file(ROOT, file) > 0

    requeued = ""
    if resolved and body.get("requeue"):
        rel = str(entry.get("file") or "")
        src = ROOT / rel
        if src.is_file():
            dest = ROOT / "inbox" / src.name
            n = 1
            while dest.exists():
                dest = ROOT / "inbox" / f"{src.stem}-{n}{src.suffix}"
                n += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            requeued = dest.relative_to(ROOT).as_posix()
    audit(conn, user["id"], "resolve_issue", None, entry.get("display") or entry.get("file"),
          {"resolved": resolved, "requeued": requeued}, request.client.host)
    return {"ok": True, "resolved": resolved, "requeued": requeued}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SETTINGS.server["host"], port=int(SETTINGS.server["port"]))
