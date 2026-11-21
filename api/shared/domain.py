"""项目与活动的领域辅助：定位、编号、模板解析。

被 /api/projects/*、/api/sw/*、/api/dashboard 共用，所以独立在 shared，
不放 routes 里，避免路由之间互相 import。
"""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from .. import apikey  # noqa: F401  # 凭据模块随 app 一起加载，保持原有初始化顺序

from ..deps import project_access, require_project_write, scopes_of
from ..state import REPO_ROOT, TZ, _SCRIPT_CACHE, _root

# engine 是仓库根下的包；这里按需延迟导入，避免与 api.app 互相 import。

# 活动上传文件名校验用的扩展名/字符集常量，与 app.py 原值一致。
_ACTIVITY_UPLOAD_EXTS = {
    ".docx", ".xlsx", ".doc", ".xls", ".pdf",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".txt", ".md", ".zip", ".7z",
}
_ACTIVITY_BAD_NAME_CHARS = set('/\\:*?"<>|')
_ACTIVITY_NAME_MAX = 120


def project_allowed(user, item: dict) -> bool:
    """admin 看全部；其余账号按 user_projects 授权（项目名 / short 均可命中）。"""
    if user["role"] == "admin":
        return True
    return project_access(user, str(item.get("name") or "")) is not None


def project_meta_or_404(name: str) -> dict:
    """取项目元数据：目录 / yaml 缺失按 404，yaml 损坏按 400。"""
    from engine import projects as _projects

    try:
        meta = _projects.project_meta(name)
    except FileNotFoundError:
        raise HTTPException(404, f"项目不存在：{name}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not meta["has_config"]:
        raise HTTPException(404, f"项目没有 yaml：{name}")
    return meta


def project_year(name: str) -> int:
    """立项年份取 project.period.start 的年份，缺失时退回当前年。"""
    from engine import projects as _projects

    try:
        data = _projects.load_project(name)
    except (FileNotFoundError, ValueError):
        return datetime.now(TZ).year
    period = data.get("period") if isinstance(data.get("period"), dict) else {}
    start = str(period.get("start") or "")
    if len(start) >= 4 and start[:4].isdigit():
        return int(start[:4])
    return datetime.now(TZ).year


def _load_script(name: str):
    """按仓库 scripts/ 的路径加载脚本模块。

    部署根的 scripts/ 可能还是旧版（例如 sync_feishu 没有 ensure/push），
    新端点要用仓库里的实现，所以显式按文件路径加载，避免被 sys.path 抢先。
    """
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_sw_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def script(name: str):
    """加载并缓存 scripts/<name>.py，同一进程内只 exec 一次。"""
    module = _SCRIPT_CACHE.get(name)
    if module is None:
        module = _load_script(name)
        _SCRIPT_CACHE[name] = module
    return module


def template_library() -> Path:
    """模板库目录：<ROOT>/templates/library（唯一模板来源）。"""
    return _root() / "templates" / "library"


def find_template(name: str) -> Path:
    """按模板名（不含扩展名）在模板库里找 docx/xlsx。

    名字非法（带路径分隔符 / 通配符 / ..）→ 400；找不到 → 404。
    """
    raw = str(name or "").strip()
    if (not raw or "/" in raw or "\\" in raw or ".." in raw
            or any(ch in raw for ch in "*?[]")):
        raise HTTPException(400, f"模板名非法：{raw}")
    lib = template_library()
    hits = sorted(p for p in lib.glob(f"{raw}.*")
                  if p.is_file() and p.suffix.lower() in (".docx", ".xlsx"))
    if not hits:
        raise HTTPException(404, f"模板不存在：{raw}")
    return hits[0]


def activity_meta_or_404(name: str, dirname: str, user, write: bool = False):
    """定位活动目录并校验权限，返回 (活动目录, 项目元数据)。

    读：admin 或项目在授权范围内；写：admin/editor 且项目在授权范围内。
    活动目录不存在 → 404，目录名非法 → 400。
    """
    from engine import projects as _projects

    meta = project_meta_or_404(name)
    if write:
        require_project_write(user, meta)
    elif user["role"] != "admin" and meta["short"] not in scopes_of(user):
        raise HTTPException(403, f"无项目 {name} 权限")
    try:
        act_dir = _projects.activity_dir(name, dirname)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not act_dir.is_dir():
        raise HTTPException(404, f"活动目录不存在：{dirname}")
    return act_dir, meta


def activity_yaml_data(act_dir: Path) -> dict:
    """读 activity.yaml；解析失败按空 dict 处理，详情页不因此 500。"""
    from engine import ingest as _ingest

    try:
        return _ingest.read_activity_yaml(act_dir)
    except ValueError:
        return {}


def activity_sub_dir(act_dir: Path, sub: str) -> Path:
    """把 sub 解析成活动目录内的目标子目录，逐级校验必须已存在。

    sub 为空表示活动根目录；多级用 `/` 分隔（如 13-其他佐证/知识库规范命名照片）。
    含 `..`、`.`、空段、反斜杠、绝对路径、空字节一律 400；任一级不存在也 400。
    """
    raw = str(sub or "").strip()
    base = act_dir.resolve()
    if not raw:
        return base
    if "\x00" in raw:
        raise HTTPException(400, "子目录路径含空字节")
    if "\\" in raw:
        raise HTTPException(400, "子目录路径不能包含反斜杠")
    if raw.startswith("/") or Path(raw).is_absolute():
        raise HTTPException(400, "子目录必须是相对路径")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise HTTPException(400, "子目录路径非法（不能包含 ..、. 或空段）")
    cur = act_dir
    for part in parts:
        cur = cur / part
        if not cur.is_dir():
            raise HTTPException(400, f"子目录不存在：{part}")
    path = cur.resolve()
    try:
        path.relative_to(base)
    except ValueError:
        raise HTTPException(400, "子目录越界")
    return path


def activity_upload_filename(filename: str) -> str:
    """校验上传文件名：扩展名、长度、隐藏文件、非法字符。返回原文件名。"""
    raw = str(filename or "")
    if not raw.strip():
        raise HTTPException(400, "缺文件名")
    if raw.startswith("."):
        raise HTTPException(400, "不允许上传隐藏文件（文件名不能以 . 开头）")
    if len(raw) > _ACTIVITY_NAME_MAX:
        raise HTTPException(400, f"文件名超过 {_ACTIVITY_NAME_MAX} 字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise HTTPException(400, "文件名含控制字符")
    if any(ch in _ACTIVITY_BAD_NAME_CHARS for ch in raw):
        raise HTTPException(400, '文件名不能包含 / \\ : * ? " < > |')
    ext = Path(raw).suffix.lower()
    if ext not in _ACTIVITY_UPLOAD_EXTS:
        raise HTTPException(400, f"不允许的扩展名：{ext or '（无扩展名）'}")
    return raw


def activity_require_code(act_dir: Path) -> None:
    """模板填充前置条件：activity.yaml 必须有 code。"""
    code = str(activity_yaml_data(act_dir).get("code") or "").strip()
    if not code:
        raise HTTPException(400, "activity.yaml 缺 code，无法生成材料")
