"""项目级端点（项目 CRUD、活动清单、核对、编号）。"""
from __future__ import annotations

from engine import ylock as _ylock

import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import yaml
from fastapi import (APIRouter, Depends, File, HTTPException, Query, Request,
                     UploadFile)

from api.deps import current_user, db, project_access, require_admin, scopes_of
from api.shared import audit as _audit_mod
from api.shared.domain import project_allowed, project_meta_or_404, project_year
from api.state import TZ
from engine import activities as _activities
from engine import coding as _coding
from engine import markdown as _markdown_mod
from engine import project_checks as _project_checks
from engine import project_parse as _project_parse
from engine import projects as _projects

router = APIRouter()


def _existing_codes(meta: dict) -> list[str]:
    """本项目已有编号：project.code + 各活动 activity.yaml 里的 code。

    **不能拿活动目录名当编号**：目录名是「01-项目启动-第一期」这种形式，
    parse_code 直接抛 ValueError，于是 next_serial 永远算不出已用流水，
    每个活动都分到 0001 —— 这就是「多期活动编号全撞」的根因
    （2026-11-25 实测：花都保障 20 个活动，activity_dirs 返回的目录名
    0/20 能当编号解析）。

    正确做法是读每个活动目录下的 activity.yaml。
    """
    codes = [meta["code"]] if meta.get("code") else []
    project_dir = Path(_projects.PROJECTS_ROOT) / meta["name"]
    act_root = project_dir / "活动"
    if not act_root.is_dir():
        return codes
    for d in sorted(act_root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        yaml_path = d / "activity.yaml"
        if not yaml_path.is_file():
            continue
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        code = str(data.get("code") or "").strip()
        if code:
            codes.append(code)
    return codes


def _act_year(act_month: int, act_year: int | None = None) -> int:
    """活动年：显式传了就用，否则按活动月与项目周期推断当前年。

    简单规则：不传时取当前年份。跨年活动由调用方显式传 act_year。
    """
    if act_year:
        return int(act_year)
    return datetime.now(TZ).year


# ---------- 项目 ----------

# 社区名从 funder 全称里取：四川省成都市温江区公平街道惠民社区居民委员会 → 惠民
_COMMUNITY_RE = re.compile(r"(?:街道)?([\u4e00-\u9fa5]{2,4})社区居民委员会")


def project_community(item: dict) -> str:
    """项目所属社区（侧边栏分组用）。取不到返回「未分组」。"""
    data = item.get("data") or {}
    parties = data.get("parties") or {}
    funder = (parties.get("funder") or {}).get("name") or ""
    m = _COMMUNITY_RE.search(str(funder))
    return m.group(1) if m else "未分组"


@router.get("/api/projects")
def api_list_projects(user=Depends(current_user)):
    """列全部项目。admin 看到全部；editor/viewer 按授权项目过滤。

    每项额外带 community 字段（社区名），供侧边栏「项目 → 社区 → 项目」
    两级分组用（2026-11-23）。
    """
    items = []
    for p in _projects.list_projects():
        if not project_allowed(user, p):
            continue
        items.append({**p, "community": project_community(p)})
    return {"total": len(items), "items": items}


@router.get("/api/projects/{name}")
def api_get_project(name: str, user=Depends(current_user)):
    """单个项目主数据。没配置 yaml 的项目按 404 处理。"""
    item = next((p for p in _projects.list_projects() if p["name"] == name), None)
    if item is None or not item.get("has_config"):
        raise HTTPException(404, f"项目不存在：{name}")
    if not project_allowed(user, item):
        raise HTTPException(403, f"无项目 {name} 权限")
    return item


@router.get("/api/projects/{name}/activities")
def api_list_activities(name: str, user=Depends(current_user)):
    """活动清单：计划拆出来的 / 磁盘已有的 / 缺的 / 多出来的。"""
    try:
        data = _projects.load_project(name)
    except FileNotFoundError:
        raise HTTPException(404, f"项目不存在：{name}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if user["role"] != "admin" and project_access(user, name) is None:
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


@router.post("/api/projects")
def api_create_project(body: dict, request: Request,
                       user=Depends(require_admin), conn=Depends(db)):
    """建空项目：目录 + 方案/协议/报告/活动/其他 + 最小 yaml。

    body: {name, short, code_rules?}。目录已存在 → 409。
    short 唯一 / 合法性校验 2026-11-25 补上：short 同时是 Cognee 数据集名，
    两个项目用同一个 short 会把材料写进同一个数据集里。
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
    try:
        short = _projects.validate_short(short)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    conflict = _projects.find_short_conflict(short)
    if conflict:
        raise HTTPException(
            409, f"项目简称 {short} 已被项目「{conflict['name']}」占用（short={conflict['short']}），"
                 f"简称会用作目录名与知识库数据集名，必须唯一")

    rules = body.get("code_rules") or {}
    if not isinstance(rules, dict):
        raise HTTPException(400, "code_rules 必须是对象")

    try:
        target, yaml_path = _projects.create_project_skeleton(
            name, short, yaml_data={"project": {"short": short, "code_rules": rules}})
    except FileExistsError as exc:      # 并发建同名项目：mkdir 抢占失败也是 409
        raise HTTPException(409, str(exc))

    _audit_mod.audit(conn, user["id"], "create_project", target=name,
          detail={"short": short, "code_rules": rules}, ip=request.client.host)
    return {"ok": True, "name": name, "short": short,
            "path": str(target), "yaml": str(yaml_path)}


@router.put("/api/projects/{name}")
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
    _ylock.atomic_write_text(yaml_path, text)

    _audit_mod.audit(conn, user["id"], "update_project", target=name,
          detail={"keys": sorted(body)}, ip=request.client.host)
    return {"ok": True, "name": name, "yaml": str(yaml_path)}


# ---------- 从项目书建项目 ----------

# 项目书允许的扩展名 → 转换函数（engine.markdown 里已有实现）
_DOC_EXTS = {
    ".docx": lambda p: _markdown_mod.docx_to_md(p),
    ".pdf": lambda p: _markdown_mod.pdf_to_md(p),
    ".md": lambda p: p.read_text(encoding="utf-8"),
}


def _max_upload_bytes() -> int:
    """上传上限：复用全局 upload.max_mb（与文档上传同一口径，默认 50MB）。"""
    from api.settings import SETTINGS
    return int(SETTINGS.upload.get("max_mb", 50)) * 1024 * 1024


def _read_upload_to_tmp(upload: "UploadFile", suffix: str) -> tuple[Path, int]:
    """把上传流写到临时文件，返回 (路径, 字节数)。超限抛 413、空文件抛 400。

    用流式读 + `xb` 独占创建：临时目录是 mkdtemp 独享的，不会与并发请求互覆盖。
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="sw-fromdocx-"))
    name = Path(str(upload.filename or "项目书")).name
    path = tmpdir / name
    limit = _max_upload_bytes()
    size = 0
    try:
        with open(path, "xb") as fh:
            while True:
                chunk = upload.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"项目书超过 {limit // (1024 * 1024)} MB 限制")
                fh.write(chunk)
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    if size == 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, "项目书是空文件")
    if not path.suffix.lower():
        path = path.with_suffix(suffix)
    return path, size


def _unique_project_name(base: str) -> str:
    """项目名重名时加序号：花都微格 -> 花都微格（2）。只改目录名，不改项目书里的名字。"""
    root = Path(_projects.PROJECTS_ROOT)
    for n in (1, *range(2, 100)):
        cand = base if n == 1 else f"{base}（{n}）"
        if not _projects.find_short_conflict(cand) and not (root / cand).exists():
            return cand
    raise HTTPException(409, f"项目名 {base} 重名过多，请用 ?project_name= 指定目录名")


@router.post("/api/projects/from-docx")
def api_create_project_from_docx(
    request: Request,
    file: Optional[UploadFile] = File(None),
    project_name: str = Query(""),
    short: str = Query(""),
    preview_only: bool = Query(False, alias="dry_run"),
    user=Depends(require_admin),
    conn=Depends(db),
):
    """**必须上传项目书**，按项目书建项目目录 + 项目 yaml。

    流程：收文件 → 转 Markdown → 正则抽字段 → 校验 short → 建目录骨架 → 写 yaml。

    参数（query string）：
      file          必填，.docx / .pdf / .md
      project_name  可选，覆盖解析出的项目名称（= 目录名）
      short         可选，项目简称（目录名 / Cognee 数据集名 / API 路径参数）；
                    不传时用 project_name；二者都缺 → 400
      dry_run       可选，true = **只解析不落盘**（Web 新建项目弹窗的预览用）。
                    同样做全部校验（简称合法性 / 唯一性 / 关键字段），
                    只是最后不建目录、不写 yaml。预览结果里带 yaml_preview。

    解析不到关键字段（项目名称 / 立项单位 / 申报单位）→ 400，detail.missing
    列出缺哪几个，带上对应 query 参数重试即可。其它字段解析不到一律留空，
    在返回的 warnings 里逐条说明 —— **不编造数据**。

    重复简称 → 409（short 是数据集名，两个项目共用一个会把材料混在一起）。
    """
    # 依赖里已经跑过 require_admin；这里再调一次，让「直接调用端点函数」的
    # 测试与调用方也走同一道判定（与 api_create_project 的写法一致）。
    require_admin(user)
    # preview_only 的默认值是 Query(False)；绕开 FastAPI 直接调函数时拿到的是
    # Query 对象本身（真值），会静默走进预演分支、项目根本不落盘。
    # 强制归一成 bool：只有字面 true 才算预演。
    preview_only = preview_only is True

    if file is None or not str(file.filename or "").strip():
        raise HTTPException(400, "必须上传项目书文件（.docx / .pdf / .md），字段名 file")
    suffix = Path(str(file.filename)).suffix.lower()
    if suffix not in _DOC_EXTS:
        raise HTTPException(400, f"项目书只支持 .docx / .pdf / .md，收到 {suffix or '（无扩展名）'}")

    tmp_path, size = _read_upload_to_tmp(file, suffix)
    dest: Path | None = None
    try:
        try:
            md = _DOC_EXTS[suffix](tmp_path)
        except RuntimeError as exc:
            raise HTTPException(400, f"项目书转换失败：{exc}")
        try:
            data, warnings = _project_parse.markdown_to_project(md)
        except Exception as exc:                 # 解析器自身异常也要给可读原因
            raise HTTPException(400, f"项目书解析失败：{exc}")

        parsed = dict(data.pop("_parsed", {}) or {})
        orig_name = str(parsed.get("project_name") or "").strip()

        # 关键字段：一个都不能少，缺了就列出来让调用方补参数重试
        missing: list[str] = []
        if not orig_name and not str(project_name or "").strip():
            missing.append("project_name")
        if not parsed.get("funder"):
            missing.append("funder")
        if not parsed.get("executer"):
            missing.append("executer")
        if missing:
            raise HTTPException(400, {
                "message": "项目书里没解析到关键字段，补上后重试："
                           "、".join(missing),
                "missing": missing,
                "parsed": parsed,
                "warnings": warnings,
                "hint": {
                    "project_name": "项目全名（会成为目录名）：?project_name=xxx",
                    "funder": "立项单位缺失：换一份带「立项单位 / 立项社区」栏目的申报书",
                    "executer": "申报单位缺失：换一份带「申报单位」栏目的申报书",
                },
            })

        # 项目目录名：显式参数优先，否则取项目书名字，重名自动加序号
        name = str(project_name or "").strip() or _unique_project_name(orig_name)
        try:
            name = _projects._safe_name(name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        # short：显式参数优先，否则用项目名；必须合法且全局唯一
        raw_short = str(short or "").strip() or name
        try:
            short_value = _projects.validate_short(raw_short)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        conflict = _projects.find_short_conflict(short_value)
        if conflict:
            raise HTTPException(409, {
                "message": f"项目简称 {short_value} 已被项目「{conflict['name']}」占用"
                           f"（其 short={conflict['short']}）。简称会用作目录名、"
                           f"Cognee 数据集名和 API 路径参数，必须唯一",
                "short": short_value,
                "conflict": conflict,
                "hint": "换一个简称重试：?short=新简称（≤40 字，不含 / \\ : * ? \" < > |）",
            })

        if preview_only:
            # 预演：把最终要写的 yaml 序列化出来返回，磁盘一个字节都不动。
            # 复用 engine 的同一个序列化函数：预览所见 = 确认后所写，不会两套格式。
            preview = _projects.dump_project_yaml(
                {**data, "project": {**data["project"], "short": short_value}})
            return {
                "ok": True, "dry_run": True, "project": name, "short": short_value,
                "dir": str(Path(_projects.PROJECTS_ROOT) / name),
                "yaml": str(Path(_projects.PROJECTS_ROOT) / name / f"{name}.yaml"),
                "doc": str(Path(_projects.PROJECTS_ROOT) / name / "方案" / Path(str(file.filename)).name),
                "parsed": parsed, "warnings": warnings, "yaml_preview": preview,
                "hint": "预演结果，未写盘；确认后再次调用（不带 dry_run）才会建项目",
            }

        try:
            target, yaml_path = _projects.create_project_skeleton(
                name, short_value, yaml_data=data)
        except FileExistsError as exc:
            raise HTTPException(409, f"{exc}（可传 ?project_name=其他名字）")

        # 项目书原件进 方案/：文件名保持原样，重名不覆盖
        doc_dir = target / "方案"
        dest = doc_dir / Path(str(file.filename)).name
        if dest.exists():
            raise HTTPException(409, f"方案/ 下已有同名文件：{dest.name}")
        shutil.move(str(tmp_path), str(dest))

        result = _projects.load_project(name)
        if conn is not None:
            _audit_mod.audit(conn, user["id"], "create_project_from_docx", target=name,
                             detail={"short": short_value, "source_doc": dest.name,
                                     "size": size, "warnings": len(warnings)},
                             ip=request.client.host)
    finally:
        # 临时目录必然清理：成功时文件已 move 走，失败时残留一并删掉
        shutil.rmtree(tmp_path.parent, ignore_errors=True)

    hint = ""
    if not result.get("indicators"):
        hint = ("项目书里没有可识别的量化指标表，indicators 为空："
                "「按指标建目录」会返回空清单，活动目录请手工建，"
                "建完用 PATCH /api/projects/{name}/activities/{a}/code 补编号")
    return {
        "ok": True,
        "project": name,
        "short": short_value,
        "dir": str(target),
        "yaml": str(yaml_path),
        "doc": str(dest),
        "parsed": parsed,
        "warnings": warnings,
        "yaml_preview": yaml_path.read_text(encoding="utf-8"),
        "hint": hint,
        "permissions": {
            "note": "新项目默认没有任何账号有权限，需在 用户管理（#/admin/users）里授权",
            "audit_url": f"/api/projects/{quote(name)}/permissions-audit",
        },
    }


@router.get("/api/projects/{name}/permissions-audit")
def api_project_permissions_audit(name: str, user=Depends(require_admin),
                                  conn=Depends(db)):
    """建完项目后的授权缺口清单：谁还没有这个项目的读写权限。

    新建项目后 user_projects 里不会有任何记录，非 admin 账号一律看不到它。
    管理员需要一份「该去哪里授什么权」的清单，所以这里直接按 users 表
    × user_projects 表算，而不是只回一个空数组。
    """
    meta = project_meta_or_404(name)
    short_value = str(meta.get("short") or name)

    with conn.cursor() as cur:
        cur.execute("SELECT id, username, real_name, role, enabled FROM users ORDER BY id")
        users = cur.fetchall()
        cur.execute("SELECT user_id, project, access FROM user_projects")
        grants = cur.fetchall()

    # 授权键可能是目录名，也可能写的是 short，两种都认（与 deps.project_access 同口径）
    keys = {name, short_value}
    by_user: dict[int, set[str]] = {}
    for uid, project, access in grants:
        if str(project) in keys:
            by_user.setdefault(int(uid), set()).add(str(access or "write"))

    without: list[dict] = []
    with_access: list[dict] = []
    for uid, username, real_name, role, enabled in users:
        item = {"id": int(uid), "username": username,
                "real_name": real_name or "", "role": role,
                "enabled": bool(enabled)}
        if role == "admin":
            # admin 靠角色恒有全部权限，不需要 user_projects 记录
            with_access.append({**item, "access": "write", "via": "role"})
            continue
        hit = by_user.get(int(uid))
        if hit:
            with_access.append({**item,
                                "access": "write" if "write" in hit else "readonly",
                                "via": "user_projects"})
        else:
            without.append(item)

    return {
        "project": name,
        "short": short_value,
        "users_without_access": without,
        "users_with_access": with_access,
        "hint": (
            f"共 {len(without)} 个非 admin 账号没有任何「{short_value}」权限；"
            "去 用户管理（#/admin/users）→ 编辑该用户 → 项目权限选「只读」或「读写」保存，"
            "保存后该用户 GET /api/me 的 projects 立即出现该项目"
        ) if without else "所有账号都已有该项目权限（admin 按角色恒有）",
    }


# ---------- 核对 / 编号 ----------

@router.get("/api/projects/{name}/check")
def api_check_project(name: str, user=Depends(current_user)):
    """项目级 19 项核对：依据《项目管理目录.docx》，扫四个资料子目录。"""
    meta = project_meta_or_404(name)
    if user["role"] != "admin" and meta["short"] not in scopes_of(user):
        raise HTTPException(403, f"无项目 {name} 权限")
    return _project_checks.check_project(Path(_projects.PROJECTS_ROOT) / name)


@router.get("/api/projects/{name}/code")
def api_project_code(name: str, act_type: int, act_month: int,
                     act_year: int = 0, serial: int = 0,
                     user=Depends(current_user)):
    """生成一个项目编号。act_year=0 表示取当前年；serial=0 表示自动取下一个流水号。"""
    meta = project_meta_or_404(name)
    if user["role"] != "admin" and meta["short"] not in scopes_of(user):
        raise HTTPException(403, f"无项目 {name} 权限")
    establish_month = meta["establish_month"]
    if not establish_month:
        raise HTTPException(400, "项目没有配置 code_rules.establish_month")
    act_types = meta["act_types"]
    if act_types and act_type not in set(act_types.values()):
        raise HTTPException(400, f"活动类型号 {act_type} 不在项目配置里")
    year = project_year(name)
    # act_year 传 0/不传 → 取当前年；传完整年份（2026）或两位年份（26）都接受。
    # 编号里只存活动年的**个位**（格式约束），所以 parsed.act_year 是个位数；
    # 为免调用方误以为「2026 被截断」，返回时另给 act_year_full。
    if act_year and act_year < 100:
        act_year = 2000 + int(act_year)
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
    # parsed.act_year 是编号里的个位；补一个完整年份，避免「2026→6」被当成 bug
    result = {"code": code, "parsed": {**parsed, "act_year_full": act_year,
                                       "act_year_note": "编号只存活动年个位，act_year_full 为完整年份"}}
    return result
