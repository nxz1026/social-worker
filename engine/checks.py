#!/usr/bin/env python3
"""归档核对：按模板「活动目录」的 15 项清单核对一个活动目录。

清单写死成常量 CHECKLIST（1..14），第 15 项「其他」恒为不适用，
在 check() 里补进结果。真实活动包的命名差异（04-/10- 有同名文档和资料目录、
报销单旧编号 21-）都体现在 kind 与 prefixes 上：

- kind=file        前缀命中的文件
- kind=xlsx        前缀命中的 .xlsx 文件（09-/21- 都认）
- kind=docx        前缀命中的 .docx 文件（04- 的资料目录不算）
- kind=dir_or_file 前缀命中的目录或文件（签到表两种形态都有）
- kind=dir_with_media  前缀命中的目录，且目录里有图片
- kind=dir_with_file   前缀命中的目录，且目录里有 pdf/图片等任何文件
- kind=dir_nonempty    前缀命中的目录，且目录非空
"""
from __future__ import annotations

from pathlib import Path

from .ingest import read_activity_yaml

CHECKLIST = [
    {"no": 1, "name": "活动计划", "prefix": "01-", "kind": "file"},
    {"no": 2, "name": "活动通知", "prefix": "02-", "kind": "file"},
    {"no": 3, "name": "签到表",   "prefix": "03-", "kind": "dir_or_file"},
    {"no": 4, "name": "满意度测评汇总表", "prefix": "05-", "kind": "file"},
    {"no": 5, "name": "总结报告", "prefix": "06-", "kind": "file"},
    {"no": 6, "name": "简报",     "prefix": "07-", "kind": "file"},
    {"no": 7, "name": "决算表",   "prefix": "08-", "kind": "file"},
    {"no": 8, "name": "监督记录表", "prefix": "09-", "kind": "file"},
    {"no": 9, "name": "报销单",   "prefixes": ["09-", "21-"], "kind": "xlsx"},
    {"no": 10, "name": "物资照片", "prefix": "10-", "kind": "dir_with_media"},
    {"no": 11, "name": "活动照片", "prefix": "11-", "kind": "dir_with_media"},
    {"no": 12, "name": "发票照片", "prefix": "12-", "kind": "dir_with_file"},
    {"no": 13, "name": "其他佐证", "prefix": "13-", "kind": "dir_nonempty"},
    {"no": 14, "name": "满意度测评表", "prefix": "04-", "kind": "docx"},
]

# 第 15 项「其他」：没有命中规则，恒为不适用
OTHER_ITEM = {"no": 15, "name": "其他"}

MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
ANY_FILE_EXT = MEDIA_EXT | {".pdf", ".docx", ".xlsx", ".doc", ".txt", ".md", ".7z", ".zip"}

# 文件名 / 目录名里出现这个词，视为该项不适用
NA_MARK = "不适用"

STATUS_TEXT = {"ok": "齐", "missing": "缺", "na": "不适用"}


def _prefixes(item: dict) -> list[str]:
    """一项可能有多个编号（报销单 09-/21-）。"""
    return list(item.get("prefixes") or [item["prefix"]])


def _entries(dir_path: Path) -> list[Path]:
    """活动目录下的一级条目，跳过隐藏文件，按名字排序保证结果稳定。"""
    return sorted(
        (p for p in dir_path.iterdir() if not p.name.startswith(".")),
        key=lambda p: p.name,
    )


def _files_under(directory: Path, exts: set[str]) -> list[Path]:
    """递归取目录下指定后缀的文件；后缀比较不区分大小写。"""
    return [p for p in directory.rglob("*")
            if p.is_file() and p.suffix.lower() in exts]


def _all_files_under(directory: Path) -> list[Path]:
    """递归取目录下所有文件（判断「非空」用，不限后缀）。"""
    return [p for p in directory.rglob("*") if p.is_file()]


def _match(item: dict, entries: list[Path]) -> tuple[Path | None, str]:
    """按 kind 判定一项是否命中，返回 (命中的路径, 说明)。

    命中的路径为 None 表示缺；说明文字直接进报告的「说明」列。
    """
    prefixes = _prefixes(item)
    kind = item.get("kind")
    hits = [p for p in entries if any(p.name.startswith(pre) for pre in prefixes)]

    if kind == "file":
        found = [p for p in hits if p.is_file()]
        if found:
            return found[0], f"命中 {found[0].name}"
        return None, f"未找到 {'/'.join(prefixes)} 开头的文件"

    if kind in ("xlsx", "docx"):
        suffix = "." + kind
        found = [p for p in hits if p.is_file() and p.suffix.lower() == suffix]
        if found:
            return found[0], f"命中 {found[0].name}"
        return None, f"未找到 {'/'.join(prefixes)} 开头的 {suffix} 文件"

    if kind == "dir_or_file":
        if hits:
            first = hits[0]
            name = first.name + ("/" if first.is_dir() else "")
            return first, f"命中 {name}"
        return None, f"未找到 {'/'.join(prefixes)} 开头的目录或文件"

    # 以下三种都要求是目录
    dirs = [p for p in hits if p.is_dir()]
    if not dirs:
        return None, f"未找到 {'/'.join(prefixes)} 开头的目录"

    if kind == "dir_with_media":
        for directory in dirs:
            media = _files_under(directory, MEDIA_EXT)
            if media:
                return directory, f"命中 {directory.name}/（{len(media)} 个图片）"
        return None, f"{dirs[0].name}/ 里没有图片"

    if kind == "dir_with_file":
        for directory in dirs:
            files = _files_under(directory, ANY_FILE_EXT)
            if files:
                return directory, f"命中 {directory.name}/（{len(files)} 个文件）"
        return None, f"{dirs[0].name}/ 里没有 pdf 或图片"

    if kind == "dir_nonempty":
        for directory in dirs:
            files = _all_files_under(directory)
            if files:
                return directory, f"命中 {directory.name}/（{len(files)} 个文件）"
        return None, f"{dirs[0].name}/ 是空目录"

    raise ValueError(f"未知的核对类型：{kind}")


def _not_applicable(act_dir: Path) -> set[int]:
    """读 activity.yaml 的 not_applicable，返回序号集合；读不了按空集处理。"""
    try:
        data = read_activity_yaml(act_dir)
    except ValueError:
        # yaml 坏了不该阻断核对，按「没有标记」继续
        return set()
    raw = data.get("not_applicable")
    if not isinstance(raw, (list, tuple)):
        return set()
    out: set[int] = set()
    for value in raw:
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return out


def check(dir_path: Path) -> dict:
    """核对一个活动目录，返回逐项结果 + 计数 + 是否完整。

    - 找到 → ok
    - 没找到 → missing
    - 序号在 activity.yaml 的 not_applicable 里，或命中的条目名含「不适用」→ na
    """
    act_dir = Path(dir_path)
    if not act_dir.is_dir():
        raise FileNotFoundError(f"活动目录不存在：{act_dir}")

    entries = _entries(act_dir)
    na_nos = _not_applicable(act_dir)
    items: list[dict] = []

    for item in CHECKLIST:
        found, detail = _match(item, entries)
        marked = any(NA_MARK in p.name for p in entries
                     if any(p.name.startswith(pre) for pre in _prefixes(item)))
        if item["no"] in na_nos:
            status, detail = "na", "activity.yaml 标记不适用"
        elif marked:
            status, detail = "na", "文件名标注不适用"
        else:
            status = "ok" if found is not None else "missing"
        items.append({"no": item["no"], "name": item["name"],
                      "status": status, "detail": detail})

    # 第 15 项没有命中规则，恒为不适用
    items.append({"no": OTHER_ITEM["no"], "name": OTHER_ITEM["name"],
                  "status": "na", "detail": "默认不适用"})

    ok_count = sum(1 for it in items if it["status"] == "ok")
    missing_count = sum(1 for it in items if it["status"] == "missing")
    na_count = sum(1 for it in items if it["status"] == "na")
    return {
        "items": items,
        "ok_count": ok_count,
        "missing_count": missing_count,
        "na_count": na_count,
        "complete": missing_count == 0,
    }


def _cell(text: str) -> str:
    """表格单元格转义：竖线会把 Markdown 表格撑破。"""
    return str(text).replace("|", "\\|")


def to_markdown(result: dict) -> str:
    """核对结果转 Markdown：明细表 + 汇总行，方便直接贴进报告。"""
    lines = [
        "| 序号 | 材料 | 状态 | 说明 |",
        "|---|---|---|---|",
    ]
    for item in result["items"]:
        lines.append("| {} | {} | {} | {} |".format(
            item["no"], _cell(item["name"]),
            STATUS_TEXT.get(item["status"], item["status"]),
            _cell(item["detail"]),
        ))
    verdict = "完整" if result["complete"] else "未完整"
    lines.append("")
    lines.append(f"**汇总**：齐 {result['ok_count']} 项、缺 {result['missing_count']} 项、"
                 f"不适用 {result['na_count']} 项；{verdict}")
    return "\n".join(lines) + "\n"
