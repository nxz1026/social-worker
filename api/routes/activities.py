"""活动级端点（按指标建目录、改名、详情）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from api.deps import current_user, require_project_write
from api.shared.domain import (activity_meta_or_404, activity_yaml_data,
                               project_meta_or_404)
from engine import activities as _activities
from engine import checks as _checks
from engine import projects as _projects

router = APIRouter()

# _count_files / _file_item 随 files/upload/download/delete 一起搬到 assets.py；
# api.app 在 import 本模块时还没执行完，模块级 `from api.app import ...`
# 会循环 import，所以这里做函数内延迟 import。

@router.post("/api/projects/{name}/activities/split")
def api_split_activities(name: str, body: dict, user=Depends(current_user)):
    """按 indicators 建活动目录。body: {"dry_run": true/false}，默认只预演。"""
    meta = project_meta_or_404(name)
    require_project_write(user, meta)
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


@router.patch("/api/projects/{name}/activities/{dirname}")
def api_rename_activity(name: str, dirname: str, body: dict,
                        user=Depends(current_user)):
    """重命名活动目录。body: {"new_name": "..."}。"""
    meta = project_meta_or_404(name)
    require_project_write(user, meta)
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


@router.get("/api/projects/{name}/activities/{dirname}")
def api_activity_detail(name: str, dirname: str, user=Depends(current_user)):
    """活动详情：编号 / 状态 / 顶层文件 / 子目录文件数 / 15 项核对。"""
    from api.routes.assets import _count_files, _file_item

    act_dir, _meta = activity_meta_or_404(name, dirname, user)
    act = activity_yaml_data(act_dir)

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
