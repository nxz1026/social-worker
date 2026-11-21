"""活动附件、上传下载、发票、照片、满意度端点。"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse
from PIL import Image

from engine import caption as _caption

from api.deps import current_user, db, require_write
from api.shared import audit as _audit_mod
from api.shared.domain import (activity_meta_or_404, activity_sub_dir,
                               activity_upload_filename, activity_yaml_data,
                               project_year, script, template_library)
from api.shared.paths import safe_rel_path
from api.state import TZ, _root

router = APIRouter()

_MIRROR_EXTS = {".docx", ".doc", ".xlsx", ".pdf"}
# 缩略图缓存目录：放 ROOT/state 下，不落进项目目录（避免 Syncthing 同步缓存）
_THUMB_DIR = "thumbs"
_THUMB_WIDTH = 320


def _file_item(path: Path) -> dict:
    """文件/目录条目：name / size / mtime / is_dir。

    文档类文件若旁边已有同名 ``.md`` 骨架，额外给 ``mirror`` 字段（md 文件名），
    前端据此显示「↗ md」提示，不参与下载或权限判断。
    """
    st = path.stat()
    item = {
        "name": path.name,
        "size": 0 if path.is_dir() else st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, TZ).isoformat(),
        "is_dir": path.is_dir(),
    }
    if not item["is_dir"] and path.suffix.lower() in _MIRROR_EXTS:
        mirror = path.with_suffix(".md")
        if mirror.is_file():
            item["mirror"] = mirror.name
    return item

def _count_files(directory: Path) -> int:
    """目录下（递归）文件数，跳过隐藏目录（.trash 等）。"""
    total = 0
    for p in directory.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(directory)
        if any(part.startswith(".") for part in rel.parts):
            continue
        total += 1
    return total


@router.get("/api/projects/{name}/activities/{dirname}/files")
def api_activity_files(name: str, dirname: str, sub: str = "",
                       user=Depends(current_user)):
    """列活动目录（或 sub 子目录）下的一级条目，按名称排序。"""
    act_dir, _meta = activity_meta_or_404(name, dirname, user)
    base = safe_rel_path(act_dir, sub, allow_empty=True)
    if not base.is_dir():
        raise HTTPException(404, f"目录不存在：{sub}")
    return [_file_item(p) for p in sorted(base.iterdir(), key=lambda x: x.name)
            if not p.name.startswith(".")]


def _check_activity_code_owner(name: str, act_dir: Path) -> None:
    """activity.yaml 有 code 时，编号前缀必须属于本项目。

    前缀 = CDYJR + 立项年 + 三位立项月（如花都 CDYJR2026005）。
    期望值优先取项目 yaml 的 code_rules；但实际数据里 yaml 的立项月可能与
    既有编号不一致（惠民 yaml 写 6、既有活动编号却是 CDYJR2026008），
    因此再并入「同项目其他活动」的既有前缀作为事实来源。
    两边都对不上，说明活动目录是从别的项目混进来的，直接 400。
    """
    from engine import activities as _activities
    from engine import coding as _coding
    from engine import projects as _projects

    code = str(activity_yaml_data(act_dir).get("code") or "").strip()
    if not code:
        return
    m = re.match(r"^([A-Za-z0-9]+)-[cC]-", code)
    if not m:
        raise HTTPException(400, f"activity.yaml 的 code 格式无法识别：{code}")
    actual = m.group(1).upper()

    expected: set[str] = set()
    month = _projects.project_meta(name).get("establish_month")
    if month:
        expected.add(f"{_coding.ORG_PREFIX}{project_year(name)}{int(month):03d}".upper())
    project_dir = Path(_projects.PROJECTS_ROOT) / name
    for other_name, other_dir in _activities.activity_dirs(project_dir).items():
        if other_name == act_dir.name:
            continue
        other_code = str(activity_yaml_data(other_dir).get("code") or "").strip()
        om = re.match(r"^([A-Za-z0-9]+)-[cC]-", other_code)
        if om:
            expected.add(om.group(1).upper())

    if not expected:
        # 项目没配立项月、也没有其他活动可参照，无从判断，不阻断
        return
    if actual not in expected:
        raise HTTPException(
            400, f"活动编号不属于本项目：期望前缀 {'/'.join(sorted(expected))}，"
                 f"实际 {actual}（{code}）")


def _activity_numbering_warning(act_dir: Path, filename: str) -> str:
    """文件名以 NN- 开头且对应核对项已 ok 时，返回重复提示（不阻断上传）。"""
    from engine import checks as _checks

    m = re.match(r"^(\d{2})-", filename)
    if not m:
        return ""
    prefix = m.group(1) + "-"
    names = [(item["no"], item["name"]) for item in _checks.CHECKLIST
             if prefix in (item.get("prefixes") or [item.get("prefix")])]
    if not names:
        return ""
    status = {it["no"]: it["status"] for it in _checks.check(act_dir).get("items") or []}
    hit = next((label for no, label in names if status.get(no) == "ok"), "")
    if not hit:
        return ""
    return f"核对项「{hit}」已齐，再上传 {prefix} 开头的文件会重复"


@router.post("/api/projects/{name}/activities/{dirname}/upload")
def api_activity_upload(
    name: str, dirname: str,
    file: UploadFile = File(...),
    sub: str = Form(""),            # 目标子目录（空 = 活动根）
    user=Depends(require_write),
    conn=Depends(db),
):
    """往活动目录上传文件。返回 {path, size, sha256}。

    校验链：写权限 → 项目/活动目录存在 → sub 逐级存在 → 文件名规范 →
    编号归属 → 同名冲突 → 流式落盘并限大小。文件名以 NN- 开头且该核对项
    已齐时，仍返回 200，但附带 warning 提醒会重复。
    """
    from settings import SETTINGS

    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)
    target_dir = activity_sub_dir(act_dir, sub)
    filename = activity_upload_filename(file.filename or "")
    _check_activity_code_owner(name, act_dir)
    # 先按上传前的磁盘状态判断是否重复，落盘后该前缀必然已齐
    warning = _activity_numbering_warning(act_dir, filename)

    dest = target_dir / filename
    if dest.exists():
        raise HTTPException(409, f"同名文件已存在：{filename}（请改名或先删除）")

    max_mb = int(SETTINGS.upload.get("max_mb", 50))
    max_bytes = max_mb * 1024 * 1024
    digest = hashlib.sha256()
    size = 0
    try:
        # "xb" 独占创建：并发同名上传时只有一个能成功，其余走 409
        with open(dest, "xb") as fh:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f"文件超过 {max_mb} MB 限制")
                digest.update(chunk)
                fh.write(chunk)
    except FileExistsError:
        raise HTTPException(409, f"同名文件已存在：{filename}（请改名或先删除）")
    except HTTPException:
        dest.unlink(missing_ok=True)      # 超限 / 中断不留半截文件
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    sha = digest.hexdigest()
    rel = dest.relative_to(act_dir.resolve()).as_posix()
    _audit_mod.audit(conn, user["id"], "upload_activity_file", project=name,
                     target=rel, detail={"activity": dirname, "size": size,
                                         "sha256": sha})
    out = {"path": rel, "size": size, "sha256": sha}
    if warning:
        out["warning"] = warning
    return out


@router.get("/api/projects/{name}/activities/{dirname}/download")
def api_activity_download(name: str, dirname: str, path: str,
                          user=Depends(current_user)):
    """下载活动内文件。path 相对活动目录，越界 → 400。"""
    act_dir, _meta = activity_meta_or_404(name, dirname, user)
    target = safe_rel_path(act_dir, path)
    if not target.is_file():
        raise HTTPException(404, f"文件不存在：{path}")
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, filename=target.name)


@router.delete("/api/projects/{name}/activities/{dirname}/files")
def api_activity_delete_file(name: str, dirname: str, body: dict, request: Request,
                             user=Depends(require_write), conn=Depends(db)):
    """删除活动内文件：移到 <活动目录>/.trash/<时间戳>/ 下，不真删。"""
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)
    target = safe_rel_path(act_dir, body.get("path"))
    if not target.is_file():
        raise HTTPException(404, f"文件不存在：{body.get('path')}")

    rel = target.relative_to(act_dir.resolve())
    if rel.parts and rel.parts[0] == ".trash":
        raise HTTPException(400, "不能删除回收站里的文件")

    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    dest = act_dir / ".trash" / stamp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        n += 1
    shutil.move(str(target), str(dest))

    _audit_mod.audit(conn, user["id"], "delete_activity_file", project=name,
                     target=rel.as_posix(),
                     detail={"activity": dirname,
                             "trash": dest.relative_to(act_dir).as_posix()},
                     ip=request.client.host)
    return {"ok": True, "path": rel.as_posix(),
            "trash": dest.relative_to(act_dir).as_posix()}


def _photo_thumb(path: Path) -> str:
    """生成/复用缩略图，返回 data URI；PIL 缺失或解码失败返回空串。

    照片原件动辄 4~5 MB，25 张就是 200 MB，不能直接塞进浏览器；
    这里按「路径 + mtime」做键缓存 320px 宽 JPEG，前端拿 data URI 直接显示。
    """
    try:
        from PIL import Image
    except ImportError:
        return ""

    try:
        stat = path.stat()
        key = hashlib.sha1(f"{path.resolve()}|{stat.st_mtime_ns}".encode()).hexdigest()
        target = _root() / "state" / _THUMB_DIR / f"{key}.jpg"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(path) as img:
                img.thumbnail((_THUMB_WIDTH, _THUMB_WIDTH * 4))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                tmp = target.with_suffix(".tmp")
                img.save(tmp, "JPEG", quality=72)
                tmp.replace(target)
        return "data:image/jpeg;base64," + base64.b64encode(
            target.read_bytes()).decode()
    except Exception:
        return ""            # 损坏图片 / 只读挂载都不该让整个列表 500


@router.post("/api/projects/{name}/activities/{dirname}/invoice/scan")
def api_invoice_scan(name: str, dirname: str, user=Depends(require_write)):
    """扫发票 + 三表校验，返回 engine.invoice.cross_check() 的结果。"""
    from engine import invoice as _invoice

    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)
    return _invoice.cross_check(act_dir)


@router.post("/api/projects/{name}/activities/{dirname}/invoice/report")
def api_invoice_report(name: str, dirname: str, body: dict,
                       user=Depends(require_write)):
    """生成发票明细表，返回 {path, count, total}。"""
    from engine import invoice as _invoice

    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)

    items = _invoice.scan_invoices(act_dir)
    report = script("invoice_report")
    out_rel = str(body.get("output")
                  or f"{_invoice.INVOICE_SUBDIR}/{report.DETAIL_NAME}").strip()
    dst = safe_rel_path(act_dir, out_rel)
    if not dst.suffix:
        dst = dst.with_name(dst.name + ".xlsx")

    template = template_library() / "发票明细表.xlsx"
    if not template.is_file():
        raise HTTPException(400, f"发票明细表模板不存在：{template}")
    report.DETAIL_TEMPLATE = template
    try:
        report.render_detail(items, dst)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))

    total = round(sum(item["amount"] for item in items
                      if item.get("amount") is not None), 2)
    return {"path": dst.relative_to(act_dir.resolve()).as_posix(),
            "count": len(items), "total": total}


@router.get("/api/projects/{name}/activities/{dirname}/photos")
def api_activity_photos(name: str, dirname: str,
                        sub: str = _caption.PHOTO_SUBDIR,
                        user=Depends(current_user)):
    """列照片：文件名 / 大小 / mtime / 缩略图 / 是否程序默认选中。

    缩略图是 data URI（认证只走 x-token header，`<img src>` 带不了 header）。
    """
    act_dir, _meta = activity_meta_or_404(name, dirname, user)
    folder = safe_rel_path(act_dir, sub)
    if not folder.is_dir():
        return []

    default = {path.relative_to(folder).as_posix()
               for path in _caption.pick_photos(act_dir, sub)}
    files = [path for path in sorted(folder.rglob("*"))
             if path.is_file() and not path.name.startswith(".")
             and path.suffix.lower() in _caption.IMAGE_EXTS]
    # 单张 4~5 MB，逐张解码要 6 秒；4 线程压到 2 秒左右（PIL 解码会放 GIL）
    with ThreadPoolExecutor(max_workers=4) as pool:
        thumbs = list(pool.map(_photo_thumb, files))

    out: list[dict] = []
    for path, thumb in zip(files, thumbs):
        rel = path.relative_to(folder).as_posix()
        stat = path.stat()
        out.append({
            "name": rel,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, TZ).isoformat(),
            "picked": rel in default,
            "thumb": thumb,
        })
    return out


@router.post("/api/projects/{name}/activities/{dirname}/satisfaction/list")
def api_sat_list(name: str, dirname: str, user=Depends(require_write)):
    """扫问卷照片，生成/更新 scores.json（保留已录分数）。"""
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)
    entries, _path = script("satisfaction").write_scores(act_dir)
    return entries


@router.get("/api/projects/{name}/activities/{dirname}/satisfaction")
def api_sat_get(name: str, dirname: str, user=Depends(current_user)):
    """读 scores.json；没有文件返回空列表。"""
    act_dir, _meta = activity_meta_or_404(name, dirname, user)
    return script("satisfaction").load_scores(act_dir)


@router.put("/api/projects/{name}/activities/{dirname}/satisfaction")
def api_sat_put(name: str, dirname: str, body: dict,
                user=Depends(require_write)):
    """写回 scores.json：body.scores = [{file, score, satisfied}]。"""
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)

    scores = body.get("scores")
    if not isinstance(scores, list):
        raise HTTPException(400, "scores 必须是数组")
    entries: list[dict] = []
    for item in scores:
        if not isinstance(item, dict):
            raise HTTPException(400, "scores 的每一项必须是对象")
        file = str(item.get("file") or "").strip()
        if not file:
            raise HTTPException(400, "每条分数都要有 file")
        score = item.get("score")
        if score is not None and (isinstance(score, bool)
                                  or not isinstance(score, (int, float))):
            raise HTTPException(400, f"分数必须是数字：{file}")
        flag = item.get("satisfied")
        entries.append({"file": file, "score": score,
                        "satisfied": flag if isinstance(flag, bool) else None})

    path = script("satisfaction").scores_path(act_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return {"ok": True, "count": len(entries),
            "path": path.relative_to(act_dir.resolve()).as_posix()}


@router.post("/api/projects/{name}/activities/{dirname}/satisfaction/fill")
def api_sat_fill(name: str, dirname: str, user=Depends(require_write)):
    """统计并填 05-满意度测评汇总表，返回统计结果 + 落盘路径。"""
    require_write(user)
    act_dir, _meta = activity_meta_or_404(name, dirname, user, write=True)

    sat = script("satisfaction")
    entries = sat.load_scores(act_dir)
    if not entries:
        raise HTTPException(400, "没有 scores.json，先点「扫描问卷」生成清单")
    stats = sat.summarize(entries)
    if not stats["份数"]:
        raise HTTPException(400, "一份分数都没录，无法统计")

    template = template_library() / "满意度测评汇总表.docx"
    if not template.is_file():
        raise HTTPException(400, f"满意度汇总表模板不存在：{template}")
    sat.SUMMARY_TEMPLATE = template

    dst = act_dir / sat.SUMMARY_NAME
    try:
        result = sat.render_summary(stats, sat._facts(act_dir), dst)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))

    return {**stats,
            "path": dst.relative_to(act_dir.resolve()).as_posix(),
            "filled": result["filled"], "missing": result["missing"]}
