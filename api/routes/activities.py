"""活动级端点（按指标建目录、改名、详情）。"""
from __future__ import annotations

from pathlib import Path

import re

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

def _bump_serial(code: str) -> str:
    """把编号末尾 4 位流水号 +1，用于同批去重。"""
    head, tail = code[:-4], code[-4:]
    if tail.isdigit():
        return f"{head}{int(tail) + 1:04d}"
    return code


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
        existing = _activities.list_existing_activities(project_dir)
        # 按活动名（忽略序号前缀）比对：已有目录的序号是历史遗留，
        # 与按指标顺序算出的序号未必一致，直接比全名会把已有活动当成新的。
        to_create, to_skip = _activities.match_existing(planned_names, existing)
        return {
            "dry_run": True,
            "would_create": to_create,
            "would_skip": to_skip,
            # 磁盘上有、指标里没有登记的目录（人工加的活动）
            "extra_on_disk": [n for n in existing
                              if _activities.activity_key(n)
                              not in {_activities.activity_key(x) for x in planned_names}],
        }

    # 建目录时顺手写最小 activity.yaml（含 code），避免产出「无编号活动」——
    # 那种活动所有生成接口都会报 CODE_MISSING，且修复只能直接改磁盘。
    from api.routes.projects import _existing_codes, api_project_code

    def _code_maker(item: dict, used: set[str]) -> str | None:
        dirname = str(item.get("dirname") or "")
        act_type = 1
        act_month = data.get("_default_month") or 1
        m = re.search(r"-(\d{2})-", dirname)
        if m:
            act_month = int(m.group(1))
        try:
            generated = api_project_code(name, act_type=act_type,
                                         act_month=act_month, user=user)
        except Exception:
            return None
        code = str((generated or {}).get("code") or "").strip()
        # 同一批里流水号会重复（后端只按磁盘已有编号算），这里做一次去重
        while code and code in used:
            code = _bump_serial(code)
        return code or None

    existing = _activities.list_existing_activities(project_dir)
    to_create, to_skip = _activities.match_existing(planned_names, existing)
    # 只建真正缺的（按活动名判断），不按全名
    planned_to_build = [a for a in planned if a["dirname"] in set(to_create)]
    created = _activities.create_activity_dirs(project_dir, planned_to_build,
                                               code_maker=_code_maker)
    return {
        "dry_run": False,
        "created": created,
        "skipped": to_skip,
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
        "check": _checks.merge_review_status(_checks.check(act_dir),
                                            _checks.read_reviews(act_dir)),
    }
