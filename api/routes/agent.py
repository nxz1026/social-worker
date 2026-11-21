"""Agent 接口 /api/sw/*：能力自述、项目活动查询、一步生成、批量、检索、照片、项目资料。"""
from __future__ import annotations

import base64
import contextlib
import io
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Request,
                     UploadFile)
from PIL import Image

from api.deps import (can_write, check_project, current_user, db,
                      project_access, scopes_of)
from api.errors import ApiError
from api.shared import audit as _audit_mod
from api.shared.delivery import sw_delivery
from api.shared.domain import (activity_require_code, activity_sub_dir,
                               activity_upload_filename, find_template,
                               project_allowed, project_meta_or_404)
from api.shared.paths import fill_output
from api.state import (ROOT, SETTINGS, TZ, _ARCHIVE_MAX_BYTES,
                       _ARCHIVE_MAX_ENTRIES, _ARCHIVE_MAX_UNCOMPRESSED,
                       _ARCHIVE_RATIO_LIMIT, _ARCHIVE_RATIO_MIN_BYTES,
                       _PHOTO_EXTS, _PHOTO_MAX_BYTES, _PHOTO_MAX_FILES,
                       _PHOTO_MAX_PIXELS, _PHOTO_MAX_TOTAL_BYTES)
from engine import activities as _activities
from engine import activity as _activity
from engine import checks as _checks
from engine import fill as _fill
from engine import llm as _llm
from engine import photos as _photos
from engine import project_checks as _project_checks
from engine import projects as _projects

router = APIRouter()

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
    _audit_mod.audit(conn, user["id"], "sw_run", project=meta["name"], target=rel,
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


@router.get("/api/sw/capabilities")
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


@router.get("/api/sw/projects")
def api_sw_projects(user=Depends(current_user)):
    """列当前 Key / 账号可见的项目，并标出 access。"""
    items = []
    for item in _projects.list_projects():
        if not project_allowed(user, item):
            continue
        access = "write" if can_write(user, item["name"]) else "readonly"
        items.append({**item, "access": access})
    return {"total": len(items), "items": items}


@router.get("/api/sw/projects/{p}/activities")
def api_sw_activities(p: str, user=Depends(current_user)):
    """列活动（直接复用 Web 端点的数据结构）。"""
    from api.routes.projects import api_list_activities

    try:
        return api_list_activities(p, user)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise ApiError(404, "PROJECT_NOT_FOUND", str(exc.detail))
        if exc.status_code == 403:
            raise ApiError(403, "FORBIDDEN", str(exc.detail))
        raise ApiError(400, "VALIDATION", str(exc.detail))


@router.get("/api/sw/projects/{p}/activities/{a}")
def api_sw_activity_detail(p: str, a: str, user=Depends(current_user)):
    """活动详情（直接复用 Web 端点的数据结构）。"""
    from api.routes.activities import api_activity_detail

    try:
        return api_activity_detail(p, a, user)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise ApiError(404, "ACTIVITY_NOT_FOUND", str(exc.detail))
        if exc.status_code == 403:
            raise ApiError(403, "FORBIDDEN", str(exc.detail))
        raise ApiError(400, "VALIDATION", str(exc.detail))


@router.post("/api/sw/run")
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


@router.post("/api/sw/batch")
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


@router.get("/api/sw/tasks/{task_id}")
def api_sw_task(task_id: str, user=Depends(current_user)):
    """异步任务状态。当前尚未接入任务表。"""
    raise ApiError(501, "TASKS_NOT_IMPLEMENTED",
                   "异步任务尚未接入",
                   "本机所有操作都是同步完成，无需轮询；此端点保留给未来长任务")


@router.get("/api/sw/search")
def api_sw_search(q: str, project: str = "", limit: int = 10,
                  user=Depends(current_user)):
    """统一检索：文档台账 + Cognee 图谱。"""
    # _load_manifest / search_datasets / _legacy_project_name 留在 app.py（有外部引用），
    # 这里函数内延迟 import：api.app 在 import 本模块时还没执行完，模块级会循环。
    from api.app import _legacy_project_name, _load_manifest
    from api.routes.misc import search_datasets

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


@router.post("/api/sw/project-files/upload")
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

    _audit_mod.audit(conn, user["id"], "upload_project_file", project=meta["name"],
          target=rel, detail={"size": size, "matched": bool(matched_item)},
          ip="api")
    return out


@router.post("/api/sw/photos/precheck")
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


@router.post("/api/sw/photos/upload")
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
        _audit_mod.audit(conn, user["id"], "upload_photos", project=meta["name"],
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


@router.post("/api/sw/photos/upload-archive")
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
                # 与直接上传走同一套像素闸门：小体积巨图（解压炸弹）必须在这里挡住，
                # 否则 zip 分支会成为绕过路径。
                _check_photo_pixels(leaf, data)
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
        _audit_mod.audit(conn, user["id"], "upload_photos_archive", project=meta["name"],
              target=fname,
              detail={"matched": matched_count, "unmatched": unmatched,
                      "shot_date": day})

    return {"ok": True, "matched": matched_count, "unmatched": unmatched,
            "total_saved": total_saved}
