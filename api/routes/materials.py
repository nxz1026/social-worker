"""活动材料端点：核对、索引、模板填充、LLM 生成、图注。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from api.deps import current_user, db, project_access, require_write
from api.shared import audit as _audit_mod
from api.shared.domain import (activity_meta_or_404, activity_require_code,
                               find_template)
from api.shared.paths import fill_output, safe_rel_path
from engine import activity as _activity
from engine import caption as _caption
from engine import checks as _checks
from engine import fill as _fill
from engine import ingest as _ingest
from engine import grounding as _grounding
from engine import llm as _llm
from engine import projects as _projects
from engine import readme as _readme
from api.state import TZ

router = APIRouter()

# LLM 生成进度（进程内）。一次调用 50~100 秒，前端轮询这个表来显示
# 「第几次生成 / 是否在自检重写」，避免用户以为卡死。
# 键：f"{项目}/{活动}"，值：{"stage","percent","started_at"}
_LLM_PROGRESS: dict[str, dict] = {}


def _progress_key(name: str, dirname: str) -> str:
    return f"{name}/{dirname}"



@router.get("/api/projects/{name}/activities/{dirname}/check")
def api_check_activity(name: str, dirname: str, user=Depends(current_user)):
    """15 项核对。返回 check() 的结果。权限同其它 activity 端点。"""
    try:
        data = _projects.load_project(name)
    except FileNotFoundError:
        raise HTTPException(404, f"项目不存在：{name}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if user["role"] != "admin" and project_access(user, name) is None:
        raise HTTPException(403, f"无项目 {name} 权限")
    try:
        act_dir = _projects.activity_dir(name, dirname)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not act_dir.is_dir():
        raise HTTPException(404, f"活动目录不存在：{dirname}")
    return _checks.merge_review_status(_checks.check(act_dir),
                                       _checks.read_reviews(act_dir))


@router.post("/api/projects/{name}/activities/{dirname}/readme")
def api_regen_readme(name: str, dirname: str, user=Depends(require_write)):
    """手动重生成单个活动的 README。内容没变时 written=False。"""
    require_write(user)
    activity_meta_or_404(name, dirname, user, write=True)
    try:
        written = _readme.write_activity_readme(name, dirname)
    except FileNotFoundError:
        raise HTTPException(404, f"活动目录不存在：{dirname}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "written": written,
            "path": _readme.readme_filename(dirname=dirname)}


@router.patch("/api/projects/{name}/activities/{dirname}/check")
def api_set_na(name: str, dirname: str, body: dict,
               user=Depends(require_write)):
    """标记活动核对项不适用。body: {"not_applicable": [15, 3]}。

    只改 activity.yaml 的 not_applicable 字段，其余字段原样保留。
    """
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)
    raw = body.get("not_applicable") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        raise HTTPException(400, "not_applicable 必须是数组")

    nos: set[int] = set()
    for value in raw:
        if isinstance(value, bool):
            raise HTTPException(400, f"核对序号非法：{value}")
        try:
            no = int(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"核对序号非法：{value}")
        if no < 1 or no > 15:
            raise HTTPException(400, f"核对序号超出范围 1-15：{no}")
        nos.add(no)

    def _mutate(data: dict) -> dict:
        data["not_applicable"] = sorted(nos)
        return data

    # 与 save_review 共用同一把活动锁：两者都改 activity.yaml 的不同字段，
    # 各自「读全量 → 改 → 覆盖写」在并发下会互相丢字段（审计 R-02）。
    from engine import ylock as _ylock
    _ylock.update_yaml(act_dir / _ingest.ACTIVITY_YAML, _mutate, lock_dir=act_dir)
    return {"ok": True, "not_applicable": sorted(nos)}


@router.post("/api/projects/{name}/activities/{dirname}/review")
def api_set_review(name: str, dirname: str, body: dict, request: Request,
                   user=Depends(require_write), conn=Depends(db)):
    """标记某个材料序号为已核对 / 已签字，或撤销。

    body: {"item": 1, "note": "...", "signed": false, "scan": "", "clear": false}
    """
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)
    if not isinstance(body, dict):
        raise HTTPException(400, "body 必须是对象")
    raw_item = body.get("item")
    if isinstance(raw_item, bool):
        raise HTTPException(400, f"核对序号非法：{raw_item}")
    try:
        item_no = int(raw_item)
    except (TypeError, ValueError):
        raise HTTPException(400, f"核对序号非法：{raw_item}")
    valid = {it["no"] for it in _checks.CHECKLIST} | {_checks.OTHER_ITEM["no"]}
    if item_no not in valid:
        raise HTTPException(400, f"核对序号超出范围 1-{_checks.OTHER_ITEM['no']}：{item_no}")

    scan = body.get("scan") or ""
    clear = bool(body.get("clear", False))
    try:
        record = _checks.save_review(
            act_dir, item_no,
            reviewed_by=str(user.get("real_name") or user.get("username") or ""),
            note=str(body.get("note") or ""),
            signed=bool(body.get("signed", False)),
            scan=str(scan),
            clear=clear,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    _audit_mod.audit(conn, user["id"], "review_material", project=name,
                     target=str(item_no),
                     detail={"activity": dirname, "clear": clear,
                             "signed": bool(body.get("signed", False)),
                             "scan": str(scan) or None},
                     ip=request.client.host)
    # 索引跟着核对记录走；写失败不影响主流程
    try:
        _readme.write_activity_readme(name, dirname)
    except Exception:
        pass
    return {"ok": True, "item": item_no, "record": record}


@router.post("/api/projects/{name}/activities/{dirname}/fill")
def api_activity_fill(name: str, dirname: str, body: dict, request: Request,
                      user=Depends(require_write), conn=Depends(db)):
    """模板填充：build_data → 合并 data → render → 写到活动目录。"""
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)
    activity_require_code(act_dir)

    tpl = find_template(body.get("template"))
    extra = body.get("data") or {}
    if not isinstance(extra, dict):
        raise HTTPException(400, "data 必须是对象")
    dst = fill_output(act_dir, body, tpl)

    merged = _activity.build_data(name, dirname, extra=extra, user=user)
    try:
        result = _fill.render(tpl, dst, merged)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    rel = dst.relative_to(act_dir.resolve()).as_posix()
    _audit_mod.audit(conn, user["id"], "fill_template", project=name, target=rel,
                     detail={"activity": dirname, "template": tpl.stem,
                             "filled": result["filled"]}, ip=request.client.host)
    return {"path": rel, "filled": result["filled"],
            "missing": result["missing"]}


@router.post("/api/projects/{name}/activities/{dirname}/llm")
def api_activity_llm(name: str, dirname: str, body: dict, request: Request,
                     user=Depends(require_write), conn=Depends(db)):
    """LLM 生成：提取 facts → llm.write()，结果直接返回，不落盘。"""
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)

    task = str(body.get("task") or "").strip()
    if not task:
        raise HTTPException(400, "缺 task")
    style = str(body.get("style") or "")
    facts = _activity.build_data(name, dirname, user=user)
    # 冷化：从项目书/协议/知识库取事实注入 prompt，避免模型凭空编内容
    ctx = _grounding.build_context(
        Path(_projects.PROJECTS_ROOT) / name, name,
        str(facts.get("活动名称") or task))
    context_block = _grounding.render_prompt_block(ctx)
    key = _progress_key(name, dirname)

    def _on_progress(stage: str, percent: int) -> None:
        _LLM_PROGRESS[key] = {"stage": stage, "percent": percent,
                              "task": task, "ts": datetime.now(TZ).isoformat()}

    _on_progress("generating", 5)
    try:
        result = _llm.write(task, facts, style=style, on_progress=_on_progress,
                            context_block=context_block)
    except ValueError as exc:
        _LLM_PROGRESS.pop(key, None)
        raise HTTPException(400, str(exc))
    except _llm.LLMError as exc:
        _LLM_PROGRESS.pop(key, None)
        raise HTTPException(502, f"LLM 生成失败：{exc}")
    finally:
        _LLM_PROGRESS.pop(key, None)

    # 生成结果留档：一次调用 100~240 秒，不能让用户每次重开弹窗都重新生成。
    # 存到活动目录 _llm/<task>.json，下次打开弹窗直接读回来编辑（2026-11-23）。
    _save_llm_result(act_dir, task, style, result)

    _audit_mod.audit(conn, user["id"], "llm_generate", project=name, target=task,
                     detail={"activity": dirname, "style": style},
                     ip=request.client.host)
    return {"task": task, "style": style, "result": result,
            "grounding": _grounding.summarize(ctx["docs"], ctx["kb"])}


# LLM 生成结果留档目录（活动目录下）
LLM_CACHE_DIR = "_llm"
# 只缓存内容字段；_check/_rewrites 是内部元信息，一并存下来便于回看自检结果
_LLM_CACHE_EXTS = (".json",)


def _llm_cache_path(act_dir: Path, task: str) -> Path:
    """活动目录 _llm/<task>.json。task 做文件名白名单，防路径穿越。"""
    safe = "".join(ch for ch in str(task) if ch.isalnum() or ch in "_-")
    return Path(act_dir) / LLM_CACHE_DIR / f"{safe or 'task'}.json"


def _save_llm_result(act_dir: Path, task: str, style: str, result: dict) -> None:
    """把生成结果写盘；失败不影响主流程（结果已经返回给用户了）。"""
    try:
        from engine import ylock as _ylock
        path = _llm_cache_path(act_dir, task)
        payload = {
            "task": task,
            "style": style,
            "saved_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "result": result,
        }
        _ylock.atomic_write_text(
            path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        pass          # 留档失败不该让生成白跑


@router.get("/api/projects/{name}/activities/{dirname}/llm-cache")
def api_llm_cache(name: str, dirname: str, task: str = "",
                  user=Depends(current_user)):
    """读上次的 LLM 生成结果：{exists, task, style, saved_at, result}。

    没传 task 时列出该活动所有已留档的任务；传了就读那一份。
    """
    act_dir, _meta = activity_meta_or_404(name, dirname, user)
    base = Path(act_dir) / LLM_CACHE_DIR
    if not task:
        tasks = []
        if base.is_dir():
            for f in sorted(base.glob("*.json")):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                tasks.append({"task": d.get("task") or f.stem,
                              "style": d.get("style") or "",
                              "saved_at": d.get("saved_at") or ""})
        return {"exists": bool(tasks), "tasks": tasks}
    path = _llm_cache_path(act_dir, task)
    if not path.is_file():
        return {"exists": False}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"exists": False}
    return {"exists": True, "task": d.get("task") or task,
            "style": d.get("style") or "", "saved_at": d.get("saved_at") or "",
            "result": d.get("result") or {}}


@router.get("/api/projects/{name}/activities/{dirname}/llm-progress")
def api_llm_progress(name: str, dirname: str, user=Depends(current_user)):
    """LLM 生成进度：{stage, percent}，没有正在跑的任务返回 {running: false}。

    一次 LLM 调用实测 50~100 秒（含文风自检重写）。前端生成期间轮询这里，
    把「生成中」细化成「第 1 次生成中 / 正在自检 / 第 2 次重写中」。
    """
    rec = _LLM_PROGRESS.get(_progress_key(name, dirname))
    if not rec:
        return {"running": False}
    return {"running": True, **rec}


@router.post("/api/projects/{name}/activities/{dirname}/llm-fill")
def api_activity_llm_fill(name: str, dirname: str, body: dict, request: Request,
                          user=Depends(require_write), conn=Depends(db)):
    """LLM 生成 + 落盘：llm.write() → 合并进 build_data() → fill.render()。"""
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)

    task = str(body.get("task") or "").strip()
    if not task:
        raise HTTPException(400, "缺 task")
    tpl = find_template(body.get("template"))
    style = str(body.get("style") or "")
    dst = fill_output(act_dir, body, tpl)

    facts = _activity.build_data(name, dirname, user=user)
    ctx = _grounding.build_context(
        Path(_projects.PROJECTS_ROOT) / name, name,
        str(facts.get("活动名称") or task))
    try:
        generated = _llm.write(task, facts, style=style,
                               context_block=_grounding.render_prompt_block(ctx))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except _llm.LLMError as exc:
        raise HTTPException(502, f"LLM 生成失败：{exc}")

    merged = _activity.build_data(name, dirname, extra=generated, user=user)
    try:
        result = _fill.render(tpl, dst, merged)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    rel = dst.relative_to(act_dir.resolve()).as_posix()
    _audit_mod.audit(conn, user["id"], "llm_fill", project=name, target=rel,
                     detail={"activity": dirname, "task": task,
                             "template": tpl.stem},
                     ip=request.client.host)
    return {"path": rel, "filled": result["filled"],
            "missing": result["missing"], "generated": generated}


@router.post("/api/projects/{name}/activities/{dirname}/captions")
def api_activity_captions(name: str, dirname: str, body: dict,
                          user=Depends(require_write)):
    """生成图注：picked 为空时由程序按文件名挑图。"""
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)

    sub = str(body.get("sub") or _caption.PHOTO_SUBDIR).strip()
    style = str(body.get("style") or "")
    picked = body.get("picked") or []
    if not isinstance(picked, list):
        raise HTTPException(400, "picked 必须是文件名数组")

    folder = safe_rel_path(act_dir, sub)
    if picked:
        photos: list[Path] = []
        for raw in picked:
            target = safe_rel_path(folder, str(raw).strip())
            if target.is_file() and target.suffix.lower() in _caption.IMAGE_EXTS:
                photos.append(target)
        if not photos:
            raise HTTPException(400, "勾选的照片都不存在")
    else:
        photos = _caption.pick_photos(act_dir, sub)
    if not photos:
        raise HTTPException(404, f"没有可用的照片：{sub}")

    facts = _activity.build_data(name, dirname, user=user)
    try:
        return _caption.write_captions(photos, facts, style=style)
    except _llm.LLMError as exc:
        raise HTTPException(502, f"图注生成失败：{exc}")


@router.post("/api/projects/{name}/activities/{dirname}/captions/fill")
def api_captions_fill(name: str, dirname: str, body: dict,
                      user=Depends(require_write)):
    """把图注填进模板的 {{图注_N}}，返回 {path, filled, missing}。"""
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)

    raw = body.get("captions")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "captions 不能为空")
    captions = [{"file": str(item.get("file") or ""),
                 "caption": str(item.get("caption") or "").strip()}
                for item in raw if isinstance(item, dict)]
    captions = [item for item in captions if item["caption"]]
    if not captions:
        raise HTTPException(400, "captions 里没有有效图注")

    tpl = find_template(body.get("template"))
    dst = fill_output(act_dir, body, tpl)
    try:
        result = _fill.render(tpl, dst, _caption.as_fill_data(captions))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return {"path": dst.relative_to(act_dir.resolve()).as_posix(),
            "filled": result["filled"], "missing": result["missing"]}
