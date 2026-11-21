#!/usr/bin/env python3
"""给 docx / xlsx / pdf 生成同名 .md 骨架，让 Obsidian 图谱能连起来。

为什么不直接同步原件：
- docx / xlsx / pdf 体积大，Syncthing 同步成本高；
- Obsidian 里点开 docx 体验差，原件走 Web 下载更合适；
- 同目录留一个同名 md，既能全文检索，又能用 wikilink 连到活动 / 项目索引。

同步边界：``.stignore`` 排除原件、只同步 md；md 是「阅读卡片」，不替代原件。
"""
from __future__ import annotations

from . import ylock as _ylock

import os
import re
from datetime import datetime
from pathlib import Path

from . import markdown as _markdown
from . import projects, readme
from .activity import CST, _name_from_dir

MIRROR_SUFFIX = ".md"

# 只处理这四种文档；图片、压缩包、txt 等不生成骨架
_MIRROR_EXTS = {".docx", ".doc", ".xlsx", ".pdf"}

_DOC_TYPE = {
    ".docx": "Word 文档",
    ".doc": "Word 文档",
    ".xlsx": "Excel 表格",
    ".pdf": "PDF 文档",
}

# .stignore 规则块：必须在所有其它规则之前，且不能有 ! 例外把它放回来
STIGNORE_MARKER = "// ---- 文档原件不同步，只同步同名 .md 骨架"
STIGNORE_BLOCK = (
    STIGNORE_MARKER
    + "（Obsidian 里点 md 阅读，原件走 Web 下载）----\n"
    + "**/*.docx\n"
    + "**/*.doc\n"
    + "**/*.xlsx\n"
    + "**/*.pdf\n\n"
)


def mirror_path(doc: Path) -> Path:
    """docx 对应的 md 骨架路径：同目录、同主名、后缀 .md。"""
    return Path(doc).with_suffix(MIRROR_SUFFIX)


def should_mirror(doc: Path) -> bool:
    """只处理 .docx/.doc/.xlsx/.pdf；跳过已存在的 md、隐藏文件、_ 开头的索引。"""
    path = Path(doc)
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    if path.suffix.lower() not in _MIRROR_EXTS:
        return False
    if path.name.startswith(".") or path.name.startswith("_"):
        return False
    return not mirror_path(path).exists()


def _human_size(size: int) -> str:
    """文件大小：B / KB / MB，保留一位小数（与 README 口径一致）。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _modified(doc: Path) -> str:
    """文件修改日期（北京时间，YYYY-MM-DD）。"""
    try:
        stamp = doc.stat().st_mtime
    except OSError:
        return "—"
    return datetime.fromtimestamp(stamp, CST).strftime("%Y-%m-%d")


def _plain_text(md: str) -> str:
    """把 Markdown 粗略压成摘要用纯文本：去标题/列表符号、表格分隔线、多余空白。"""
    lines: list[str] = []
    for line in str(md or "").splitlines():
        text = line.strip()
        if not text:
            continue
        # 表格分隔行 |---|---| 没有信息量
        if set(text) <= set("|-: "):
            continue
        text = re.sub(r"^#{1,6}\s+", "", text)
        text = re.sub(r"^[-*+]\s+", "", text)
        text = re.sub(r"^\d+\.\s+", "", text)
        text = text.replace("|", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            lines.append(text)
    return " ".join(lines)


def _summary(doc: Path, limit: int = 300) -> str:
    """前 300 字正文；转换失败写「（无法提取正文）」，不影响骨架生成。"""
    try:
        text = _plain_text(_markdown.file_to_md(doc))
    except Exception:
        return "（无法提取正文）"
    if not text:
        return "（无法提取正文）"
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _activity_display(project_name: str, dirname: str) -> str:
    """活动显示名：优先 activity.yaml，失败时从目录名反推。"""
    try:
        act_dir = projects.activity_dir(project_name, dirname)
        act = readme._read_activity(act_dir)
        return readme._activity_name(dirname, act)
    except Exception:
        return _name_from_dir(dirname) or dirname


def render_mirror(doc: Path, project_name: str,
                  activity_dirname: str = "") -> str:
    """生成骨架 markdown：标题 / 原件信息 / 摘要 / 活动与项目 wikilink。"""
    path = Path(doc)
    try:
        size = _human_size(path.stat().st_size)
    except OSError:
        size = "—"

    lines = [
        f"# {path.stem}",
        "",
        f"> 类型：{_DOC_TYPE.get(path.suffix.lower(), '文档')}"
        f" ｜ 大小：{size} ｜ 修改：{_modified(path)}",
        f"> 原件：[[{path.name}]]",
        "",
        "## 摘要",
        "",
        _summary(path),
        "",
        "---",
        "",
    ]

    if activity_dirname:
        act_file = Path(readme.readme_filename(
            dirname=activity_dirname)).stem
        act_name = _activity_display(project_name, activity_dirname)
        lines.append(f"所属活动：[[{act_file}|{act_name}]]")
    proj_file = Path(readme.readme_filename(project_name=project_name)).stem
    lines.append(f"所属项目：[[{proj_file}|{project_name}]]")
    return "\n".join(lines) + "\n"


def write_mirror(doc: Path, project_name: str, activity_dirname: str = "",
                 dry_run: bool = False) -> bool:
    """写骨架；内容相同不写。返回是否真的写了（dry-run 时表示「会写」）。"""
    target = mirror_path(doc)
    text = render_mirror(doc, project_name, activity_dirname)
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    if dry_run:
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    _ylock.atomic_write_text(target, text)
    return True


def _activity_of(project_dir: Path, doc: Path) -> str:
    """文档属于哪个活动：活动/<目录名>/... 取第一层；其余返回空串。"""
    try:
        parts = doc.relative_to(project_dir).parts
    except ValueError:
        return ""
    if len(parts) >= 3 and parts[0] == "活动":
        return parts[1]
    return ""


def ensure_stignore(project_dir: Path, dry_run: bool = False) -> bool:
    """确保项目 .stignore 在最前面排除文档原件。已存在规则块则不动。

    只改已有 .stignore：文件不存在时**不新建**，避免只写文档规则、
    丢掉项目模板里的照片/内部数据排除规则。
    """
    path = Path(project_dir) / ".stignore"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if STIGNORE_MARKER in text:
        return False

    # 插到第一条真正的规则之前：保留文件头注释，同时保证不被后面的 ! 例外覆盖。
    # 第一段规则前的「段落注释」也一起让位，避免注释标题错配到新规则块。
    lines = text.splitlines(keepends=True)
    index = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("//") \
                and not stripped.startswith("#"):
            index = i
            break
    while index > 0:
        prev = lines[index - 1].strip()
        if prev.startswith("//") or prev.startswith("#"):
            index -= 1
        else:
            break

    head = "".join(lines[:index])
    tail = "".join(lines[index:])
    if head and not head.endswith("\n"):
        head += "\n"
    new_text = head + STIGNORE_BLOCK + tail

    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    _ylock.atomic_write_text(path, new_text)
    return True


def mirror_project(project_name: str, dry_run: bool = False) -> dict:
    """扫一个项目所有 docx/xlsx/pdf，逐个补同名 md 骨架。

    返回 {"written": [相对路径], "skipped": n, "errors": [错误说明]}。
    """
    out: dict = {"written": [], "skipped": 0, "errors": []}
    project_dir = projects._project_dir(project_name)
    if not project_dir.is_dir():
        out["errors"].append(f"{project_name}：项目目录不存在")
        return out

    try:
        ensure_stignore(project_dir, dry_run=dry_run)
    except Exception as exc:                 # .stignore 失败不阻断骨架生成
        out["errors"].append(f"{project_name}/.stignore：{exc}")

    for dirpath, dirnames, filenames in os.walk(project_dir):
        # 跳过隐藏目录、回收站、Python 缓存；排序保证输出稳定
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d != "__pycache__")
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() not in _MIRROR_EXTS:
                continue
            if not should_mirror(path):
                out["skipped"] += 1
                continue
            try:
                wrote = write_mirror(
                    path, project_name, _activity_of(project_dir, path),
                    dry_run=dry_run)
            except Exception as exc:         # 单个文件失败不中断整轮
                rel = path.relative_to(project_dir).as_posix()
                out["errors"].append(f"{project_name}/{rel}：{exc}")
                continue
            if wrote:
                rel_md = mirror_path(path).relative_to(project_dir).as_posix()
                out["written"].append(f"{project_name}/{rel_md}")
            else:
                out["skipped"] += 1
    return out


def refresh_all(dry_run: bool = False) -> dict:
    """全部项目：逐个补 md 骨架，并确保 .stignore 规则存在。"""
    out: dict = {"written": [], "skipped": 0, "errors": []}
    root = Path(projects.PROJECTS_ROOT)
    if not root.is_dir():
        out["errors"].append(f"项目根目录不存在：{root}")
        return out

    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        try:
            result = mirror_project(project_dir.name, dry_run=dry_run)
        except Exception as exc:
            out["errors"].append(f"{project_dir.name}：{exc}")
            continue
        out["written"].extend(result["written"])
        out["skipped"] += result["skipped"]
        out["errors"].extend(result["errors"])
    return out
