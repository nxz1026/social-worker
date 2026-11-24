#!/usr/bin/env python3
"""项目 / 活动 README 索引生成。

活动目录里的 README.md 给 Obsidian 看：活动基本信息、15 项材料核对状态、
材料清单与文件列表。项目目录里的 README.md 汇总 19 项项目资料 + 活动清单。

README 只写人要看的东西：不写 activity.yaml 里的 source / imported_at 这类
程序内部字段，也不把核对明细里的长说明塞进表格。
"""
from __future__ import annotations

from . import ylock as _ylock

from datetime import datetime
from pathlib import Path

from . import activities, checks, project_checks, projects
from .activity import CST, _name_from_dir
from .ingest import read_activity_yaml

# 状态图标：与 engine.checks 的四种状态一一对应
STATUS_ICON = {"ok": "✅", "partial": "⚠️", "missing": "❌", "na": "—"}

# 表格里需要额外文字的状态（ok / missing / na 只显示图标）
STATUS_LABEL = {"partial": "待补"}

README_NAME = "README.md"   # 单文件模式（兼容旧调用）


def readme_filename(dirname: str = "", project_name: str = "") -> str:
    """README 文件名。

    Obsidian 图谱按「文件名」建节点，所有活动都叫 README 会变成一堆同名节点。
    活动级用「活动名-资料索引.md」，项目级用「项目名-项目资料索引.md」，
    保证图谱里每个节点唯一、可读。
    """
    # 前缀下划线：避开核对规则里的「NN-」前缀，否则会被误判成对应材料
    if project_name:
        return f"_{project_name}-项目资料索引.md"
    if dirname:
        return f"_{dirname}-资料索引.md"
    return README_NAME

# 活动目录里属于程序内部、不该出现在文件列表里的文件
_INTERNAL_FILES = {"activity.yaml", README_NAME}

# 判定「这个文件是不是我生成的导航页」的唯一口径。
# 新增生成物时只要在这里登记，索引脚本与核对逻辑都会自动跳过，
# 不需要各自维护一份关键词表（2026-11-24：因为两处各写一份、后缀写成
# 下划线而实际是连字符，导致 45 个索引页仍被送进知识图谱）。
_INDEX_MARKERS = ("-资料索引.md", "项目资料索引.md")
_INDEX_DOCX_NAMES = {"活动目录.docx", "项目管理目录.docx"}


def is_generated_nav(path: Path) -> bool:
    """是不是本系统生成的导航/索引文件（活动索引 md、项目索引 md、目录 docx）。

    这些文件的内容就是材料清单表（文件名 + 核对状态），没有业务知识，
    文件名本身已由对应原件入图。任何「要不要索引/要不要核对」的判断
    都应调用这个函数，不要自己写关键词。
    """
    name = Path(path).name
    if any(name.endswith(m) for m in _INDEX_MARKERS):
        return True
    return name in _INDEX_DOCX_NAMES


def _is_readme_file(name: str) -> bool:
    """是不是本模块生成的索引文件（含新的带前缀命名）。"""
    return name in _INTERNAL_FILES or (name.startswith("_") and name.endswith("资料索引.md"))


def _cell(text) -> str:
    """表格单元格转义：竖线会把 Markdown 表格撑破。"""
    return str(text).replace("|", "\\|")


def _status_cell(status: str) -> str:
    """状态单元格：图标 +（部分状态）简短说明。"""
    icon = STATUS_ICON.get(status, status)
    label = STATUS_LABEL.get(status)
    return f"{icon} {label}" if label else icon


def _wikilink(rel: str) -> str:
    """Obsidian 内链：同目录 [[文件名]]，子目录 [[子目录/文件名]]。"""
    return f"[[{rel}]]"


def _human_size(size: int) -> str:
    """文件大小：B / KB / MB，保留一位小数。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _date_of(path: Path) -> str:
    """文件修改日期（北京时间，YYYY-MM-DD）。"""
    return datetime.fromtimestamp(path.stat().st_mtime, CST).strftime("%Y-%m-%d")


def _activity_name(dirname: str, act: dict) -> str:
    """活动名：activity.yaml 有就优先用，否则从目录名推断。"""
    for key in ("活动名称", "name"):
        value = str(act.get(key) or "").strip()
        if value:
            return value
    return _name_from_dir(dirname) or dirname


def _activity_code(act: dict) -> str:
    """活动编号：兼容旧字段「项目编号」。"""
    return str(act.get("code") or act.get("项目编号") or "").strip()


def _read_activity(act_dir: Path) -> dict:
    """读 activity.yaml；解析失败按空 dict 处理，README 不该因此生成失败。"""
    try:
        return read_activity_yaml(act_dir)
    except ValueError:
        return {}


def _matched_paths(act_dir: Path) -> dict[int, Path | None]:
    """逐项取出命中的文件 / 目录，用来在表格「文件」列放链接。

    check() 只返回状态和说明，不返回路径；这里复用 checks._match 的匹配逻辑，
    保证表格里的链接与核对结果指向同一份材料。
    """
    entries = checks._entries(act_dir)
    out: dict[int, Path | None] = {}
    for item in checks.CHECKLIST:
        found, _detail, _status = checks._match(item, entries, act_dir)
        out[item["no"]] = found
    return out


def _file_cell(path: Path | None, act_dir: Path,
               status: str = "", detail: str = "") -> str:
    """材料表格「文件」列：文件给 wikilink，目录给目录名，没找到写（无）。

    「待补」要写清缺什么：光写（无）看不出是压根没做，还是做了但缺签字扫描件。
    """
    if path is None:
        # 待补但一个文件都没匹配到：说明缺的是整份材料
        if status == "partial" and detail:
            return f"⚠️ {_cell(detail)}"
        return "（无）"
    if path.is_dir():
        base = f"{_cell(path.name)}/"
    else:
        try:
            rel = path.relative_to(act_dir).as_posix()
        except ValueError:
            rel = path.name
        base = _wikilink(rel)
    # 找到了文件但仍待补（例如只有未签字 docx）：把缺什么挂在后面
    if status == "partial" and detail:
        return f"{base}<br>⚠️ {_cell(detail)}"
    return base


def _visible_count(directory: Path) -> int:
    """目录下的文件数（递归，跳过 .trash 等隐藏目录）。"""
    return len(checks._visible_files_under(directory))


def _entry_lines(act_dir: Path) -> list[str]:
    """活动根目录下的文件 / 子目录清单。

    跳过隐藏文件、activity.yaml 与 README.md 本身，按名称排序。
    """
    lines: list[str] = []
    for path in checks._entries(act_dir):
        if _is_readme_file(path.name):
            continue
        if path.is_dir():
            count = _visible_count(path)
            suffix = f"{count} 个文件" if count else "空目录"
            lines.append(f"- {path.name}/（{suffix}）")
        else:
            lines.append(f"- {path.name}（{_human_size(path.stat().st_size)}，"
                         f"{_date_of(path)}）")
    return lines


def render_activity_readme(project_name: str, dirname: str) -> str:
    """生成活动 README 的 Markdown 文本。

    结构：标题 / 基本信息 / 核对汇总 / 15 项材料清单 / 文件列表。
    """
    act_dir = projects.activity_dir(project_name, dirname)
    if not act_dir.is_dir():
        raise FileNotFoundError(f"活动目录不存在：{act_dir}")

    act = _read_activity(act_dir)
    result = checks.check(act_dir)
    paths = _matched_paths(act_dir)

    name = _activity_name(dirname, act)
    code = _activity_code(act) or "（未编号）"
    status = str(act.get("status") or "").strip() or "未标注"

    lines = [
        f"# {name}",
        "",
        f"> 项目：{project_name} ｜ 编号：{code} ｜ 状态：{status}",
        f"> 核对：齐 {result['ok_count']} 项 / 待补 {result['partial_count']} 项"
        f" / 缺 {result['missing_count']} 项",
        "",
        "## 材料清单",
        "",
        "| 序号 | 材料 | 状态 | 文件 |",
        "|---|---|---|---|",
    ]
    for item in result["items"]:
        lines.append("| {} | {} | {} | {} |".format(
            item["no"],
            _cell(item["name"]),
            _status_cell(item["status"]),
            _file_cell(paths.get(item["no"]), act_dir,
                       status=item["status"], detail=item.get("detail", "")),
        ))

    lines += ["", "## 文件", ""]
    entries = _entry_lines(act_dir)
    lines.extend(entries or ["（无文件）"])
    return "\n".join(lines) + "\n"


def render_project_readme(project_name: str) -> str:
    """生成项目 README：19 项项目资料核对 + 活动清单（链接到各活动 README）。"""
    project_dir = projects._project_dir(project_name)
    if not project_dir.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{project_dir}")

    result = project_checks.check_project(project_dir)

    lines = [
        f"# {project_name}",
        "",
        f"> 核对：齐 {result['ok_count']} 项 / 缺 {result['missing_count']} 项"
        f" / 不适用 {result['na_count']} 项",
        "",
        "## 项目材料",
        "",
        "| 序号 | 材料 | 状态 |",
        "|---|---|---|",
    ]
    for item in result["items"]:
        lines.append("| {} | {} | {} |".format(
            item["no"], _cell(item["name"]), _status_cell(item["status"])))

    lines += ["", "## 活动", ""]
    dirs = activities.activity_dirs(project_dir)
    if not dirs:
        lines.append("（暂无活动）")
    else:
        lines += [
            "| 活动 | 编号 | 核对 |",
            "|---|---|---|",
        ]
        for dirname, act_dir in dirs.items():
            act = _read_activity(act_dir)
            try:
                act_result = checks.check(act_dir)
                summary = (f"齐 {act_result['ok_count']} 项 / "
                           f"待补 {act_result['partial_count']} 项 / "
                           f"缺 {act_result['missing_count']} 项")
            except Exception:
                summary = "核对失败"
            name = _activity_name(dirname, act)
            code = _activity_code(act) or "（未编号）"
            # 链接到活动自己的索引；活动名做显示文本
            link = f"[[{dirname}/{Path(readme_filename(dirname=dirname)).stem}|{name}]]"
            lines.append(f"| {link} | {_cell(code)} | {summary} |")
    return "\n".join(lines) + "\n"


def _write_if_changed(path: Path, text: str, dry_run: bool = False) -> bool:
    """内容相同不写；不同则原子写（先 .tmp 再 replace）。返回是否真的写了。"""
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        # 读不了就当内容不同，走覆盖分支
        pass
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    _ylock.atomic_write_text(path, text)
    return True


def write_activity_readme(project_name: str, dirname: str) -> bool:
    """写活动索引 md（文件名 = 活动目录名-资料索引.md）。

    内容与现有文件相同则不写，返回是否真的写了。
    同时删掉旧的 README.md（避免图谱里留一个同名节点）。
    """
    act_dir = projects.activity_dir(project_name, dirname)
    text = render_activity_readme(project_name, dirname)
    target = act_dir / readme_filename(dirname=dirname)
    wrote = _write_if_changed(target, text)
    legacy = act_dir / README_NAME
    if legacy.is_file():
        try:
            legacy.unlink()
        except OSError:
            pass
    return wrote


def write_project_readme(project_name: str) -> bool:
    """写项目索引 md（文件名 = 项目名-项目资料索引.md），并清掉旧 README.md。"""
    project_dir = projects._project_dir(project_name)
    text = render_project_readme(project_name)
    target = project_dir / readme_filename(project_name=project_name)
    wrote = _write_if_changed(target, text)
    legacy = project_dir / README_NAME
    if legacy.is_file():
        try:
            legacy.unlink()
        except OSError:
            pass
    return wrote


def refresh_all(*, dry_run: bool = False,
                project: str | None = None) -> dict:
    """扫全部项目全部活动，按需重写 README。

    返回 {"written": [相对路径], "skipped": 未变化数量, "errors": [错误说明]}。
    dry_run=True 时只统计「会写哪些」，不落盘。
    单个活动出错只记 errors，不影响其它活动。
    """
    out: dict = {"written": [], "skipped": 0, "errors": []}

    root = Path(projects.PROJECTS_ROOT)
    if project:
        names = [project]
    elif root.is_dir():
        # 直接扫目录，不读 yaml：某个项目 yaml 坏了不该让整轮刷新失败
        names = sorted(p.name for p in root.iterdir()
                       if p.is_dir() and not p.name.startswith("."))
    else:
        out["errors"].append(f"项目根目录不存在：{root}")
        return out

    for name in names:
        try:
            project_dir = projects._project_dir(name)
        except (ValueError, FileNotFoundError) as exc:
            out["errors"].append(f"{name}：{exc}")
            continue
        if not project_dir.is_dir():
            out["errors"].append(f"{name}：项目目录不存在")
            continue

        for dirname in activities.activity_dirs(project_dir):
            fname = readme_filename(dirname=dirname)
            rel = f"{name}/活动/{dirname}/{fname}"
            act_dir = project_dir / "活动" / dirname
            try:
                text = render_activity_readme(name, dirname)
                changed = _write_if_changed(act_dir / fname, text, dry_run)
            except Exception as exc:            # 单个活动失败不中断整轮
                out["errors"].append(f"{rel}：{exc}")
                continue
            if changed:
                out["written"].append(rel)
                # 清掉旧的同名 README.md，免得图谱里留同名节点
                legacy = act_dir / README_NAME
                if not dry_run and legacy.is_file():
                    try:
                        legacy.unlink()
                    except OSError:
                        pass
            else:
                out["skipped"] += 1

        fname = readme_filename(project_name=name)
        rel = f"{name}/{fname}"
        try:
            text = render_project_readme(name)
            changed = _write_if_changed(project_dir / fname, text, dry_run)
        except Exception as exc:
            out["errors"].append(f"{rel}：{exc}")
            continue
        if changed:
            out["written"].append(rel)
            legacy = project_dir / README_NAME
            if not dry_run and legacy.is_file():
                try:
                    legacy.unlink()
                except OSError:
                    pass
        else:
            out["skipped"] += 1

    return out
