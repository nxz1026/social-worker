#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修模板页眉/编号前导空格与字号导致的折行。

规则见工单：页眉空格统一 8、档案编号 sz=24 空格 8。
用法：
  python3 scripts/fix_template_layout.py --dry-run    # 只报告会改什么
  python3 scripts/fix_template_layout.py              # 实际改（先备份）
  python3 scripts/fix_template_layout.py --template 计划书

设计要点：
1. 按「同一段落内所有 w:t 拼接后的文本」判断，兼容 Word 把文字拆成多个 run。
2. 只动 w:t 的前导空格、档案编号段落的 w:sz，不动文字、字体、段落属性。
3. 改过的 w:t 一律补 xml:space="preserve"，否则 Word 会吞掉空格。
4. 幂等：符合规则的段落不写文件；重复运行改动数为 0。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lxml import etree

# 模板目录（只允许改这里的文件）
TEMPLATE_DIR = Path("/srv/social-work/templates/library")

# 页眉：目标前导空格数
HEADER_SPACES = 8
# 正文档案编号：目标前导空格数与字号（半磅，24 = 12pt）
ARCHIVE_SPACES = 8
ARCHIVE_SZ = "24"

# 页眉占位符；段落必须以它开头（前面只允许空格）
HEADER_RE = re.compile(r"^[ \u3000\t]*\{\{项目名称\}\}")
# 正文档案编号；兼容「档案编号:」「项目编号：」「项目编号」等写法
ARCHIVE_RE = re.compile(
    r"^[ \u3000\t]*(?:档案编号|项目编号)[ \u3000]*[:：]?[ \u3000]*\{\{项目编号\}\}"
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
SPACE_CHARS = " \u3000\t"

_HEADER_PART_RE = re.compile(r"word/header[^/]*\.xml$")
_DOCUMENT_PART = "word/document.xml"


@dataclass
class Change:
    template: str
    part: str
    paragraph: int
    field: str
    old: str
    new: str

    def render(self) -> str:
        return (
            f"{self.template} | {self.part} | 段落 {self.paragraph} | "
            f"{self.field} | {self.old} → {self.new}"
        )


def _q(tag: str) -> str:
    return W + tag


def _text_nodes(paragraph) -> list:
    """取属于本段落的 w:t（排除文本框里嵌套段落中的 w:t）。"""
    nodes = []
    for node in paragraph.iter(_q("t")):
        parent = node.getparent()
        nearest_p = None
        while parent is not None:
            if parent.tag == _q("p"):
                nearest_p = parent
                break
            parent = parent.getparent()
        if nearest_p is paragraph:
            nodes.append(node)
    return nodes


def _run_text_nodes(run, paragraph) -> list:
    return [node for node in _text_nodes(paragraph) if run in node.iterancestors()]


def _paragraph_text(paragraph) -> str:
    return "".join(node.text or "" for node in _text_nodes(paragraph))


def _leading_len(text: str) -> int:
    return len(text) - len(text.lstrip(SPACE_CHARS))


def _ensure_preserve(node) -> None:
    node.set(XML_SPACE, "preserve")


def _set_leading_spaces(paragraph, target: int) -> int | None:
    """把段落的「前导空格」总数改成 target，返回旧值；无文本时返回 None。"""
    nodes = _text_nodes(paragraph)
    if not nodes:
        return None

    total = 0
    for node in nodes:
        text = node.text or ""
        lead = _leading_len(text)
        total += lead
        if lead < len(text):
            break

    if total == target:
        return None

    if total > target:
        excess = total - target
        for node in nodes:
            text = node.text or ""
            lead = _leading_len(text)
            if lead == 0:
                if not text:
                    continue  # 空 w:t，跳过，别让它挡住后面的空格
                break
            cut = min(lead, excess)
            # 从这段前导空格的「尾部」删，保留开头的空格
            node.text = text[: lead - cut] + text[lead:]
            _ensure_preserve(node)
            excess -= cut
            if lead > cut:  # 这个节点后面已经是正文，前导区结束
                break
            if excess <= 0:
                break
    else:
        # 前导空格不足 8：补在第一个文本节点最前面
        add = target - total
        first = nodes[0]
        first.text = " " * add + (first.text or "")
        _ensure_preserve(first)

    return total


def _find_child(parent, tag: str):
    return parent.find(_q(tag)) if parent is not None else None


def _set_sz(run, value: str) -> tuple[bool, str]:
    """把 run 的 w:sz 改成 value，返回 (是否有改动, 旧值展示)。

    新增 w:sz 时按 OOXML rPr 顺序插在 w:szCs 之前；没有 szCs 则插在
    其它排在其后的属性之前。
    """
    rpr = _find_child(run, "rPr")
    if rpr is None:
        rpr = etree.Element(_q("rPr"))
        run.insert(0, rpr)

    sz = _find_child(rpr, "sz")
    if sz is not None:
        old = sz.get(_q("val"))
        if old == value:
            return False, old or ""
        sz.set(_q("val"), value)
        return True, old or ""

    sz = etree.Element(_q("sz"))
    sz.set(_q("val"), value)

    after_sz = {
        "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText",
        "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
        "specVanish", "oMath",
    }
    insert_at = len(rpr)
    for i, child in enumerate(rpr):
        local = etree.QName(child).localname
        if local in after_sz:
            insert_at = i
            break
    rpr.insert(insert_at, sz)
    return True, "未设"


def _archive_runs(paragraph) -> list:
    """档案编号段落里所有带文字的 run（含只放前导空格的 run）。"""
    runs = []
    for run in paragraph.iter(_q("r")):
        # 只取本段落直属 run，跳过嵌套段落
        if paragraph not in run.iterancestors():
            continue
        nodes = _run_text_nodes(run, paragraph)
        if not nodes:
            continue
        if not any((node.text or "") for node in nodes):
            continue
        runs.append(run)
    return runs


def _apply_part(part: str, data: bytes, template: str) -> tuple[bytes, list[Change]]:
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(data, parser)
    changes: list[Change] = []
    is_header = bool(_HEADER_PART_RE.fullmatch(part))

    for index, paragraph in enumerate(root.iter(_q("p")), start=1):
        text = _paragraph_text(paragraph)
        if not text:
            continue

        if is_header and HEADER_RE.match(text):
            leading = len(text) - len(text.lstrip(SPACE_CHARS))
            # 只收紧过长的前导空格；本来就没有空格的页眉（如 logo 后紧跟
            # 项目名）保持原样，避免无谓的版式位移。
            if leading > HEADER_SPACES:
                old = _set_leading_spaces(paragraph, HEADER_SPACES)
                if old is not None:
                    changes.append(Change(
                        template, part, index, "页眉前导空格", str(old), str(HEADER_SPACES)
                    ))
            continue

        if part == _DOCUMENT_PART and ARCHIVE_RE.match(text):
            leading = len(text) - len(text.lstrip(SPACE_CHARS))
            if leading > ARCHIVE_SPACES:
                old = _set_leading_spaces(paragraph, ARCHIVE_SPACES)
                if old is not None:
                    changes.append(Change(
                        template, part, index, "档案编号前导空格", str(old), str(ARCHIVE_SPACES)
                    ))
            for run in _archive_runs(paragraph):
                changed, old_sz = _set_sz(run, ARCHIVE_SZ)
                if changed:
                    changes.append(Change(
                        template, part, index, "档案编号字号 sz", old_sz, ARCHIVE_SZ
                    ))

    if not changes:
        return data, []

    new_data = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    return new_data, changes


def _rewrite_docx(path: Path, new_parts: dict[str, bytes]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(
            tmp, "w", zipfile.ZIP_DEFLATED
        ) as zout:
            zout.comment = zin.comment
            for info in zin.infolist():
                payload = new_parts.get(info.filename)
                if payload is None:
                    payload = zin.read(info.filename)
                zout.writestr(info, payload)
        shutil.copystat(path, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _backup(paths: list[Path], template_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = template_dir / f".backup-{stamp}"
    suffix = 1
    while backup_dir.exists():
        backup_dir = template_dir / f".backup-{stamp}-{suffix}"
        suffix += 1
    backup_dir.mkdir(parents=True)
    for path in paths:
        shutil.copy2(path, backup_dir / path.name)
    return backup_dir


def fix_file(path: Path, dry_run: bool = False) -> list[Change]:
    """修一个 docx，返回改动列表。dry_run 时不写文件、不备份。"""
    changes: list[Change] = []
    new_parts: dict[str, bytes] = {}
    with zipfile.ZipFile(path, "r") as zf:
        for name in zf.namelist():
            if not (_HEADER_PART_RE.fullmatch(name) or name == _DOCUMENT_PART):
                continue
            new_data, part_changes = _apply_part(name, zf.read(name), path.name)
            if part_changes:
                new_parts[name] = new_data
                changes.extend(part_changes)

    if changes and not dry_run:
        _rewrite_docx(path, new_parts)
    return changes


def _select_templates(template_dir: Path, pattern: str | None) -> list[Path]:
    files = sorted(template_dir.glob("*.docx"))
    if not pattern:
        return files
    needle = pattern.strip()
    if needle.lower().endswith(".docx"):
        needle = needle[:-5]
    return [p for p in files if needle in p.stem]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="修模板页眉/档案编号折行")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    parser.add_argument("--template", help="只处理名称包含该字符串的模板")
    parser.add_argument(
        "--templates-dir",
        type=Path,
        default=TEMPLATE_DIR,
        help=f"模板目录（默认 {TEMPLATE_DIR}）",
    )
    args = parser.parse_args(argv)

    template_dir = args.templates_dir
    if not template_dir.is_dir():
        print(f"模板目录不存在：{template_dir}", file=sys.stderr)
        return 2

    targets = _select_templates(template_dir, args.template)
    if not targets:
        print(f"没有匹配的模板：{args.template!r}", file=sys.stderr)
        return 2

    all_changes: list[tuple[Path, list[Change]]] = []
    for path in targets:
        try:
            changes = fix_file(path, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - 单个模板坏了不影响其它模板
            print(f"[失败] {path.name}: {exc}", file=sys.stderr)
            continue
        if changes:
            all_changes.append((path, changes))

    prefix = "[dry-run] " if args.dry_run else ""
    for _path, changes in all_changes:
        for change in changes:
            print(prefix + "[改动] " + change.render())

    total = sum(len(changes) for _path, changes in all_changes)
    if not all_changes:
        print("无改动（已全部符合规则）")
        return 0

    print(f"\n共 {total} 处改动，涉及 {len(all_changes)} 个模板")
    if not args.dry_run:
        backup = _backup([path for path, _changes in all_changes], template_dir)
        print(f"已备份到：{backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
