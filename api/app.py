#!/usr/bin/env python3
"""社工文档服务 API（FastAPI）。

认证：session token（PG sessions 表，Header: x-token）
权限：role(admin/editor/viewer) + user_projects 项目隔离
审计：写操作落 audit_log
归档：调 scripts/archive.py
检索：调 scripts/cognee.py search
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(os.environ.get("SOCIAL_WORK_ROOT", "/srv/social-work"))
SCRIPTS = ROOT / "scripts"
PG_DSN = os.environ.get(
    "SOCIALWORK_DB_DSN",
    "host=127.0.0.1 port=5432 dbname=socialwork user=admin password=tjfjyjr",
)
TZ = timezone(timedelta(hours=8))
TOKEN_TTL_HOURS = 12
ALL_PROJECTS = ["huadu", "huimin"]

app = FastAPI(title="社工文档服务", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATES_DIR = Path(os.environ.get(
    "SOCIALWORK_TEMPLATES", str(Path.home() / ".dsh/skills/social-work/templates")))
TYPE_MAP = {
    "个案记录": "case-record", "家访提纲": "home-visit-outline",
    "服务计划": "service-plan", "活动方案": "activity-plan",
    "项目申报": "project-application", "评估报告": "evaluation-report",
}


def llm_chat(prompt: str, max_tokens: int = 4000) -> str:
    """调 longcat 生成文书。读 /srv/social-work/.env 的 LLM 配置。"""
    import urllib.request
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    endpoint = env.get("LLM_ENDPOINT", "")
    key = env.get("LLM_API_KEY", "")
    model = env.get("LLM_MODEL", "").split("/")[-1]
    if not endpoint or not key:
        raise RuntimeError("LLM 未配置")
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3, "max_tokens": max_tokens}
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        # 区分超时与其它错误，便于上层返回 504/500
        # urllib 超时会包成 URLError(reason=socket.timeout)
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(exc).lower():
            raise TimeoutError(str(exc)) from exc
        raise
    return data["choices"][0]["message"]["content"]


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
def audit(conn, user_id, action, project=None, target=None, detail=None, ip=None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO audit_log(user_id, action, project, target, detail, ip)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (user_id, action, project, target,
             json.dumps(detail, ensure_ascii=False) if detail else None, ip),
        )
    conn.commit()


# ---------- 依赖 ----------
def current_user(request: Request, conn=Depends(db)):
    token = request.headers.get("x-token") or request.query_params.get("token")
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


def check_project(user, project: str):
    if project not in user["projects"]:
        raise HTTPException(403, f"无项目 {project} 权限")


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


# ---------- 文档 ----------
@app.get("/api/docs")
def list_docs(project: str = "", category: str = "", q: str = "",
              user=Depends(current_user)):
    if project and project not in user["projects"]:
        raise HTTPException(403, f"无项目 {project} 权限")
    scopes = [project] if project else user["projects"]   # BUG-07：无 project 返回全部授权项目
    # 台账过滤
    import yaml
    mpath = ROOT / "state" / "manifest.yaml"
    manifest = yaml.safe_load(mpath.read_text(encoding="utf-8")) if mpath.is_file() else {}
    out = []
    for token, e in (manifest.get("files") or {}).items():
        if e.get("status") == "deleted":
            continue
        if scopes and e.get("project") not in scopes:
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


@app.get("/api/docs/{token}")
def get_doc(token: str, user=Depends(current_user)):
    import yaml
    mpath = ROOT / "state" / "manifest.yaml"
    manifest = yaml.safe_load(mpath.read_text(encoding="utf-8")) if mpath.is_file() else {}
    e = (manifest.get("files") or {}).get(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    check_project(user, e.get("project"))
    if e.get("status") == "deleted":
        raise HTTPException(404, "文档已删除")
    content = ""
    if e.get("md") and (ROOT / e["md"]).is_file():
        content = (ROOT / e["md"]).read_text(encoding="utf-8")
    return {"token": token, "name": e.get("name"), "project": e.get("project"),
            "category": e.get("category"), "content": content,
            "original": e.get("original"),
            "detected_project": e.get("detected_project")}


@app.post("/api/docs")
async def upload_doc(
    request: Request,
    file: UploadFile = File(...),
    project: str = Form(""),
    category: str = Form(""),
    user=Depends(require_write),
    conn=Depends(db),
):
    if project and project not in user["projects"]:
        raise HTTPException(403, f"无项目 {project} 权限")
    filename = Path(file.filename or "upload.bin").name
    tmpdir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmpdir, filename)
    with open(tmp_path, "wb") as f:
        f.write(await file.read())
    cmd = ["python3", str(SCRIPTS / "archive.py"), "--file", tmp_path]
    if project:
        cmd += ["--project", project]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    shutil.rmtree(tmpdir, ignore_errors=True)
    audit(conn, user["id"], "create", project, file.filename,
          {"returncode": proc.returncode}, request.client.host)
    if proc.returncode != 0:
        raise HTTPException(500, f"归档失败：{proc.stdout[-300:]}")
    return {"ok": True, "output": proc.stdout[-800:]}


@app.patch("/api/docs/{token}")
def update_doc(token: str, body: dict, request: Request,
               user=Depends(require_write), conn=Depends(db)):
    import yaml
    mpath = ROOT / "state" / "manifest.yaml"
    manifest = yaml.safe_load(mpath.read_text(encoding="utf-8")) if mpath.is_file() else {}
    e = (manifest.get("files") or {}).get(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    check_project(user, e.get("project"))
    content = body.get("content")
    if content is None:
        raise HTTPException(400, "缺 content")
    md_rel = e.get("md")
    if not md_rel or not (ROOT / md_rel).is_file():
        raise HTTPException(400, "该文档无 md，无法修改")
    (ROOT / md_rel).write_text(content, encoding="utf-8")
    # BUG-04：改内容后触发索引重建（reconcile 检测 md 变化 → 更新 Cognee）
    try:
        subprocess.run(
            ["python3", str(SCRIPTS / "reconcile.py"), "--pg-dsn", PG_DSN, "--no-notify"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    except Exception:
        pass   # 索引重建失败不阻断 PATCH（下轮 15min timer 兜底）
    audit(conn, user["id"], "update", e.get("project"), e.get("name"),
          {"md": md_rel}, request.client.host)
    return {"ok": True}


@app.delete("/api/docs/{token}")
def delete_doc(token: str, request: Request, user=Depends(require_write),
               conn=Depends(db)):
    import yaml
    mpath = ROOT / "state" / "manifest.yaml"
    manifest = yaml.safe_load(mpath.read_text(encoding="utf-8")) if mpath.is_file() else {}
    e = (manifest.get("files") or {}).get(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    check_project(user, e.get("project"))
    proc = subprocess.run(
        ["python3", str(SCRIPTS / "delete.py"), token, "--mode", "trash", "--yes"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    audit(conn, user["id"], "delete", e.get("project"), e.get("name"),
          {"mode": "trash", "returncode": proc.returncode}, request.client.host)
    if proc.returncode != 0:
        raise HTTPException(500, f"删除失败：{proc.stdout[-300:]}")
    return {"ok": True, "output": proc.stdout[-500:]}


# ---------- 检索 ----------
@app.get("/api/search")
def search(q: str, project: str = "", user=Depends(current_user)):
    if project and project not in user["projects"]:
        raise HTTPException(403, f"无项目 {project} 权限")
    dataset = project or (user["projects"][0] if user["projects"] else "huadu")
    import re
    try:
        proc = subprocess.run(
            ["python3", str(SCRIPTS / "cognee.py"), "search", q, "--dataset", dataset],
            cwd=str(ROOT), capture_output=True, text=True, timeout=90)
        out = proc.stdout[-4000:]
        out = re.sub(r"<[^>]+>", "", out)   # BUG-06：去 HTML 标签（docx 表格转出）
        return {"ok": proc.returncode == 0, "output": out}
    except subprocess.TimeoutExpired:
        # BUG-05：检索超时按无结果返回，不拖死调用方
        return {"ok": True, "output": "", "note": "检索超时，已按无结果返回"}


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

    # 检索素材（取用户第一个授权项目）
    dataset = user["projects"][0] if user["projects"] else "huadu"
    search_out = ""
    if context:
        try:
            proc = subprocess.run(
                ["python3", str(SCRIPTS / "cognee.py"), "search", context,
                 "--dataset", dataset],
                cwd=str(ROOT), capture_output=True, text=True, timeout=300)
            search_out = proc.stdout[-3000:]
        except Exception:
            search_out = ""

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
        content = llm_chat(prompt)
    except (TimeoutError, socket.timeout) as exc:
        traceback.print_exc()
        raise HTTPException(504, f"生成超时（LLM 未在限时内返回）：{exc}")
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(500, f"生成失败：{exc}")
    audit(conn, user["id"], "generate", target=doc_type,
          detail={"context": context[:200]}, ip=request.client.host)
    return {"ok": True, "content": content}


# ---------- 用户管理（admin） ----------
def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "需 admin")
    return user


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


@app.post("/api/admin/users")
def admin_create_user(body: dict, request: Request, conn=Depends(db),
                      user=Depends(require_admin)):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "viewer"
    projects = body.get("projects") or []
    if not username or not password:
        raise HTTPException(400, "用户名或密码为空")
    if role not in ("admin", "editor", "viewer"):
        raise HTTPException(400, "角色非法")
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
            cur.execute("UPDATE users SET role=%s WHERE id=%s", (body["role"], uid))
        if "enabled" in body:
            cur.execute("UPDATE users SET enabled=%s WHERE id=%s",
                        (bool(body["enabled"]), uid))
        if "password" in body and body["password"]:
            cur.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                        (hash_pw(body["password"]), uid))
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


@app.get("/api/admin/audit")
def admin_audit(limit: int = 100, conn=Depends(db), user=Depends(require_admin)):
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
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8001")))
