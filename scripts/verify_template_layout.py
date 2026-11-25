#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证模板改完后不会折行。

对每个模板每个页眉/编号段落：
- 取 run 的 sz（没有则用段落样式的，再没有用文档默认 21）
- 拼上最长的项目名（29 字），用 east_asian_width 算宽度
- 超过 487pt 就报折行；前导空格 > 8 或档案编号 sz != 24 也报错

用法：
  python3 scripts/verify_template_layout.py
  python3 scripts/verify_template_layout.py --template 计划书
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

TEMPLATE_DIR = Path("/srv/social-work/templates/library")

# 实测页眉可用宽度：9746 twips = 487pt
MAX_WIDTH_PT = 487.0
# 最长的真实项目名（29 字），来自工单
LONGEST_PROJECT_NAME = '"花小帮·邻里队"——花都社区矛盾纠纷预防与化解微治理项目'

HEADER_RE = re.compile(r"^[ \u3000\t]*\{\{项目名称\}\}")
# 签到表的编号占位符是 {{活动编号}}，其余模板是 {{项目编号}}，两种都算档案编号
ARCHIVE_RE = re.compile(
    r"^[ \u3000\t]*(?:档案编号|项目编号)[ \u3000]*[:：]?[ \u3000]*"
    r"\{\{(?:项目编号|活动编号)\}\}"
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
SPACE_CHARS = " \u3000\t"

_HEADER_PART_RE = re.compile(r"word/header[^/]*\.xml$")
_DOCUMENT_PART = "word/document.xml"


@dataclass
class LayoutItem:
    template: str
    part: str
    paragraph: int
    kind: str
    text: str
    leading: int
    sizes: list[str]
    width_pt: float
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _q(tag: str) -> str:
    return W + tag


def _text_nodes(paragraph) -> list:
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


def _paragraph_text(paragraph) -> str:
    return "".join(node.text or "" for node in _text_nodes(paragraph))


class Styles:
    """styles.xml 里字号解析需要的最小信息。"""

    def __init__(self, zf: zipfile.ZipFile):
        self.default_sz = "21"
        self.default_paragraph_style: str | None = None
        self.styles: dict[str, tuple[str | None, str | None, str | None]] = {}
        try:
            raw = zf.read("word/styles.xml")
        except KeyError:
            return
        root = etree.fromstring(raw)
        for style in root.iter(_q("style")):
            sid = style.get(_q("styleId"))
            if not sid:
                continue
            typ = style.get(_q("type"))
            based = style.find(_q("basedOn"))
            based_id = based.get(_q("val")) if based is not None else None
            sz_node = style.find(f"{_q('rPr')}/{_q('sz')}")
            sz = sz_node.get(_q("val")) if sz_node is not None else None
            self.styles[sid] = (typ, based_id, sz)
            if typ == "paragraph" and style.get(_q("default")) in ("1", "true"):
                self.default_paragraph_style = sid
        doc_sz = root.find(
            f"{_q('docDefaults')}/{_q('rPrDefault')}/{_q('rPr')}/{_q('sz')}"
        )
        if doc_sz is not None:
            self.default_sz = doc_sz.get(_q("val")) or self.default_sz

    def style_sz(self, style_id: str | None) -> str | None:
        seen = set()
        while style_id and style_id not in seen:
            seen.add(style_id)
            typ, based, sz = self.styles.get(style_id, (None, None, None))
            if sz:
                return sz
            style_id = based
        return None

    def paragraph_sz(self, paragraph) -> str:
        ppr = paragraph.find(_q("pPr"))
        if ppr is not None:
            rpr = ppr.find(_q("rPr"))
            if rpr is not None:
                sz = rpr.find(_q("sz"))
                if sz is not None and sz.get(_q("val")):
                    return sz.get(_q("val"))
        style_id = self.default_paragraph_style
        if ppr is not None:
            pstyle = ppr.find(_q("pStyle"))
            if pstyle is not None and pstyle.get(_q("val")):
                style_id = pstyle.get(_q("val"))
        return self.style_sz(style_id) or self.default_sz


def _char_width_em(ch: str) -> float:
    """按 east_asian_width 估算字符宽度（单位 em）。"""
    if ch in "\t":
        return 1.0
    width = unicodedata.east_asian_width(ch)
    # W/F 为全角；A（·、— 等）在中文排版里也按全角算，保守
    return 1.0 if width in ("W", "F", "A") else 0.5


def measure_width_pt(text: str, sizes: list[str]) -> float:
    """text 与 sizes 逐字符对应，sizes 为半磅值字符串。"""
    total = 0.0
    for ch, half_points in zip(text, sizes):
        try:
            font_pt = int(half_points) / 2
        except (TypeError, ValueError):
            font_pt = 10.5
        total += _char_width_em(ch) * font_pt
    return total


def _substitute_with_sizes(
    paragraph, styles: Styles
) -> tuple[str, list[str], list[str]]:
    """返回 (替换占位符后的段落文本, 每字符 sz, 段落里所有文本 run 的 sz)。"""
    chars: list[str] = []
    sizes: list[str] = []
    run_sizes: list[str] = []
    paragraph_sz = styles.paragraph_sz(paragraph)

    for run in paragraph.iter(_q("r")):
        if paragraph not in run.iterancestors():
            continue
        nodes = [n for n in _text_nodes(paragraph) if run in n.iterancestors()]
        if not nodes:
            continue
        rpr = run.find(_q("rPr"))
        sz = None
        if rpr is not None:
            sz_node = rpr.find(_q("sz"))
            if sz_node is not None:
                sz = sz_node.get(_q("val"))
        sz = sz or paragraph_sz
        run_text = "".join(n.text or "" for n in nodes)
        if not run_text:
            continue
        run_sizes.append(sz)
        for ch in run_text:
            chars.append(ch)
            sizes.append(sz)

    text = "".join(chars)
    # 段落内占位符可能被拆到多个 run；按整段替换，插入字沿用占位符首字符字号
    for placeholder in ("{{项目名称}}", "{{项目编号}}"):
        start = 0
        while True:
            pos = text.find(placeholder, start)
            if pos < 0:
                break
            sz = sizes[pos] if pos < len(sizes) else paragraph_sz
            text = text[:pos] + LONGEST_PROJECT_NAME + text[pos + len(placeholder):]
            sizes = sizes[:pos] + [sz] * len(LONGEST_PROJECT_NAME) + sizes[pos + len(placeholder):]
            start = pos + len(LONGEST_PROJECT_NAME)

    # 行尾空格在 Word 里不参与折行
    while text and text[-1] in SPACE_CHARS:
        text = text[:-1]
        sizes = sizes[:-1]
    return text, sizes, run_sizes


def scan_docx(path: Path) -> list[LayoutItem]:
    items: list[LayoutItem] = []
    with zipfile.ZipFile(path, "r") as zf:
        styles = Styles(zf)
        for name in zf.namelist():
            is_header = bool(_HEADER_PART_RE.fullmatch(name))
            if not (is_header or name == _DOCUMENT_PART):
                continue
            root = etree.fromstring(zf.read(name))
            for index, paragraph in enumerate(root.iter(_q("p")), start=1):
                original = _paragraph_text(paragraph)
                if not original:
                    continue
                if is_header and HEADER_RE.match(original):
                    kind = "页眉"
                elif name == _DOCUMENT_PART and ARCHIVE_RE.match(original):
                    kind = "档案编号"
                else:
                    continue

                text, sizes, run_sizes = _substitute_with_sizes(paragraph, styles)
                leading = len(original) - len(original.lstrip(SPACE_CHARS))
                width = measure_width_pt(text, sizes)
                item = LayoutItem(
                    template=path.name,
                    part=name,
                    paragraph=index,
                    kind=kind,
                    text=original.strip(),
                    leading=leading,
                    sizes=run_sizes,
                    width_pt=width,
                )
                if leading > 8:
                    item.problems.append(f"前导空格 {leading} > 8")
                if kind == "档案编号" and any(sz != "24" for sz in run_sizes):
                    item.problems.append(f"档案编号 sz {run_sizes} != 24")
                if width > MAX_WIDTH_PT:
                    item.problems.append(f"宽度 {width:.1f}pt > {MAX_WIDTH_PT:.0f}pt，会折行")
                items.append(item)
    return items


def scan_templates(template_dir: Path, pattern: str | None = None) -> list[LayoutItem]:
    files = sorted(template_dir.glob("*.docx"))
    if pattern:
        needle = pattern.strip()
        if needle.lower().endswith(".docx"):
            needle = needle[:-5]
        files = [p for p in files if needle in p.stem]
    items: list[LayoutItem] = []
    for path in files:
        try:
            items.extend(scan_docx(path))
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] {path.name}: {exc}", file=sys.stderr)
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证模板页眉/编号不会折行")
    parser.add_argument("--template", help="只验证名称包含该字符串的模板")
    parser.add_argument(
        "--templates-dir",
        type=Path,
        default=TEMPLATE_DIR,
        help=f"模板目录（默认 {TEMPLATE_DIR}）",
    )
    args = parser.parse_args(argv)

    if not args.templates_dir.is_dir():
        print(f"模板目录不存在：{args.templates_dir}", file=sys.stderr)
        return 2

    items = scan_templates(args.templates_dir, args.template)
    if not items:
        print("没有找到页眉/档案编号段落", file=sys.stderr)
        return 2

    print(
        f"{'模板':<24} {'部件':<18} {'类型':<6} {'前导':>4} "
        f"{'sz':<10} {'宽度':>8}  结果"
    )
    problems = 0
    for item in items:
        sizes = ",".join(dict.fromkeys(item.sizes)) or "-"
        result = "OK" if item.ok else "；".join(item.problems)
        if not item.ok:
            problems += 1
        print(
            f"{item.template:<24} {item.part:<18} {item.kind:<6} "
            f"{item.leading:>4} {sizes:<10} {item.width_pt:>7.1f}pt  {result}"
        )

    print(f"\n共检查 {len(items)} 处，问题 {problems} 处")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
