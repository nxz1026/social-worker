"""文档归档与检索端点。"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import shutil
import signal
import subprocess
import sys
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import status as status_mod

from settings import SETTINGS

from api.deps import (check_project, current_user, db, project_access,
                      require_admin, require_write, scopes_of)
from api.shared.audit import audit
from api.shared.paths import normalize_rel
from api.state import (ALL_PROJECTS, COGNEE_BASE, PG_DSN, REPO_ROOT, ROOT,
                       SCRIPTS, _API_OCR_EXTS, _COGNEE_CACHE, _COGNEE_TTL)
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import name_parse as _np  # noqa: E402

# 以下三个辅助仍留在 app.py（_sw / search 域也在用），这里做函数内延迟 import：
# api.app 在 import 本模块时还没执行完，模块级 `from api.app import ...` 会循环。
def _legacy_project_name(value: str) -> str:
    from api.app import _legacy_project_name as _fn
    return _fn(value)

def _flow() -> dict:
    """读取飞书流程配置。

    注意 sys.modules 里可能同时存在 `app` 和 `api.app` 两个模块对象
    （脚本模式 `python3 api/app.py` 是前者，测试 `import app` 也是前者，
    `import api.app` 才是后者）。测试用 monkeypatch 打的是已加载的那个，
    所以这里优先复用 sys.modules 里现成的模块，保证 patch 能生效。
    """
    import sys

    for mod_name in ("app", "api.app"):
        mod = sys.modules.get(mod_name)
        fn = getattr(mod, "_flow", None) if mod is not None else None
        if callable(fn):
            return fn()
    return {}

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


class ManifestCorrupt(RuntimeError):
    """台账内容无法解析。"""

def _manifest_file() -> Path:
    """台账文件路径：state/manifest.yaml。"""
    return ROOT / "state" / "manifest.yaml"


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


router = APIRouter()


# ---------- 文档 ----------
@router.get("/api/docs")
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

@router.get("/api/docs/health")
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

@router.get("/api/docs/{token}")
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

@router.get("/api/docs/{token}/source")
def download_source(token: str, request: Request,
                    user=Depends(current_user), conn=Depends(db)):
    """下载原件。按项目鉴权，逐次写审计。"""
    e = _manifest_entry(token)
    if not e:
        raise HTTPException(404, "文档不存在")
    if e.get("status") == "deleted":
        raise HTTPException(404, "文档已删除")
    check_project(user, _legacy_project_name(str(e.get("project") or "")))
    rel = normalize_rel(str(e.get("original") or ""), root=ROOT)
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

@router.post("/api/docs")
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

        # 幂等：同一份内容已经归档过 → 直接回同一个 token，不产生第二份档案。
        # 但只能复用【调用方有权限的项目】里的记录：全台账按 sha256 找会命中
        # 其他项目的同名内容，原实现直接返回那条记录的 token/文件名/md 路径，
        # 等于跨项目元数据泄露（审计 I-04）。无权限就当作没命中，走正常归档。
        dup = _find_by_sha256(sha)
        if dup:
            dup_token, rec = dup
            dup_project = str(rec.get("project") or "")
            if dup_project and dup_project in scopes_of(user, ""):
                audit(conn, user["id"], "upload_duplicate", dup_project,
                      rec.get("name"),
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
                and project_access(user, archived_project) is None:
            raise HTTPException(403, f"归档到无权限项目 {archived_project}，请联系管理员处理")
        return {"ok": True, **receipt, "output": (out or "")[-800:]}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

@router.patch("/api/docs/{token}")
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

@router.delete("/api/docs/{token}")
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
