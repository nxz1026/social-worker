"""项目级端点（项目 CRUD、活动清单、核对、编号）。"""
from __future__ import annotations

from engine import ylock as _ylock

import re
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from api.deps import current_user, db, project_access, require_admin, scopes_of
from api.shared import audit as _audit_mod
from api.shared.domain import project_allowed, project_meta_or_404, project_year
from api.state import TZ
from engine import activities as _activities
from engine import coding as _coding
from engine import project_checks as _project_checks
from engine import projects as _projects

router = APIRouter()


def _existing_codes(meta: dict) -> list[str]:
    """本项目已有编号：project.code + 以编号命名的活动目录（解析不了的自动跳过）。

    从 app.py 搬过来（原实现见 1734239）；1942829 抽 shared/domain.py 时漏了它，
    导致本文件落地前 `/api/projects/{name}/code?serial=0` 会 NameError → 500。
    """
    codes = [meta["code"]] if meta.get("code") else []
    project_dir = Path(_projects.PROJECTS_ROOT) / meta["name"]
    codes.extend(_activities.activity_dirs(project_dir).keys())
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
    _ylock.atomic_write_text(yaml_path, text)

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
