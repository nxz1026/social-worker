#!/usr/bin/env python3
"""从活动目录里已有的计划书 docx 解析出环节 / 困难 / 物资。

真实计划书的表格列顺序、列数都可能与模板不同：
  11-共治行动   阶段/时间/内容/所需物资/人员/备注
  爱邻议事会     同样的 6 列，但表头写法略有差异
  花都某期       列顺序被调整过
所以一律按表头名匹配列，不看列序号。

实现路径：engine.markdown.docx_to_md 先把 docx 转成 Markdown 表格，
再按表头找到三张表（流程表 / 困难表 / 物资预算表），逐行取单元格。
合并单元格在 Markdown 里表现为带 <br> 的重复文本，清洗时一并去掉。

解析失败（没有 01-*.docx、转换失败、找不到流程表）返回 None，
由调用方回退到 LLM 生成。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import markdown, plan_sheet

# 表头别名：键是内部字段名，值是可接受的表头写法（匹配前会统一去噪）
_STEP_ALIASES = {
    "阶段": ("阶段", "环节", "流程阶段"),
    "时间": ("时间", "活动时间"),
    "内容": ("内容", "活动内容", "环节内容"),
    "所需物资": ("所需物资", "物资", "所需材料"),
    "人员": ("人员", "负责人", "执行人员", "参与人员"),
    "备注": ("备注",),
}
_DIFFICULTY_ALIASES = {
    "预计困难": ("预计困难", "困难", "可能困难"),
    "解决方案": ("解决方案", "解决措施", "应对措施", "方案"),
}
_MATERIAL_ALIASES = {
    "类别": ("类别", "费用类别"),
    "物资名称": ("物资名称", "名称", "物品名称"),
    "数量\\规格": ("数量\\规格", "数量规格", "规格", "数量"),
    "单价": ("单价",),
    "小计": ("小计",),
}

# 判断表格类型的必要列：这三组都能对上，才认为找到了对应表
_STEP_REQUIRED = ("内容", "所需物资")
_DIFFICULTY_REQUIRED = ("预计困难", "解决方案")
_MATERIAL_REQUIRED = ("物资名称", "单价")

# 表头里的噪声：换行标记、空白、单位后缀、反斜杠
_HEADER_NOISE = ("<br>", "\r", "\n", " ", "\u3000", "（元）", "(元)", "\\")

# 物资表里不是明细的行（合计行）
_TOTAL_LABELS = ("合计", "总计")

_PRICE_NOISE = ("￥", "¥", "元", ",", "，", " ")


def _clean_cell(text) -> str:
    """Markdown 单元格 → 纯文本：去 <br>、反转义竖线、压掉多余空白。"""
    raw = str(text if text is not None else "")
    raw = raw.replace("<br>", "").replace("\r", "").replace("\n", "")
    raw = raw.replace("\\|", "|")
    return raw.strip()


def _norm_header(text) -> str:
    """表头归一化：去 <br>/空白/「（元）」/反斜杠，便于按名字匹配。"""
    normalized = _clean_cell(text)
    for token in _HEADER_NOISE:
        normalized = normalized.replace(token, "")
    return normalized


def _split_row(line: str) -> list[str]:
    """按未转义的竖线切一行 Markdown 表格。"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]

    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for char in body:
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            buf.append(char)
            escaped = True
        elif char == "|":
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    cells.append("".join(buf))
    return cells


def _tables(md: str) -> list[tuple[list[str], list[list[str]]]]:
    """从 Markdown 正文里取出所有表格：(表头, 数据行)。"""
    lines = md.splitlines()
    tables: list[tuple[list[str], list[list[str]]]] = []
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue

        block: list[str] = []
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            block.append(lines[index])
            index += 1
        # 至少要有表头 + 分隔行
        if len(block) < 2:
            continue
        tables.append((_split_row(block[0]), [_split_row(line) for line in block[2:]]))
    return tables


def _mapping(headers: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """表头 → {内部字段名: 列下标}，只收能对上的字段。"""
    normalized = [_norm_header(header) for header in headers]
    found: dict[str, int] = {}
    for field, names in aliases.items():
        wanted = {_norm_header(name) for name in names}
        for index, header in enumerate(normalized):
            if header and header in wanted:
                found[field] = index
                break
    return found


def _pick(tables, aliases: dict[str, tuple[str, ...]],
          required: tuple[str, ...]):
    """找第一张包含全部必要列的表格，返回 (表头, 数据行, 列映射)。"""
    for headers, rows in tables:
        mapping = _mapping(headers, aliases)
        if all(field in mapping for field in required):
            return headers, rows, mapping
    return None


def _rows(headers: list[str], rows: list[list[str]], mapping: dict[str, int],
          aliases: dict[str, tuple[str, ...]]) -> list[dict]:
    """按列映射取值；表头缺的字段补空串，保证返回结构统一。"""
    width = len(headers)
    result: list[dict] = []
    for raw in rows:
        cells = list(raw) + [""] * (width - len(raw))
        item = {}
        for field in aliases:
            index = mapping.get(field)
            item[field] = _clean_cell(cells[index]) if index is not None and index < len(cells) else ""
        result.append(item)
    return result


def _price(value) -> float:
    """单价转数字：容忍「4.00/份」「￥50」「约 30 元」，取不到按 0。"""
    text = _clean_cell(value)
    for token in _PRICE_NOISE:
        text = text.replace(token, "")
    try:
        return float(text)
    except ValueError:
        match = re.match(r"^\s*(\d+(?:\.\d+)?)", text)
        return float(match.group(1)) if match else 0.0


def _first_plan_docx(act_dir: Path) -> Path | None:
    """活动目录里的 01-*.docx（计划书）。"""
    matches = sorted(act_dir.glob("01-*.docx"))
    return matches[0] if matches else None


def parse_existing_plan(act_dir: Path) -> dict | None:
    """从活动目录的 01-*.docx 解析出环节/困难/物资。

    用 engine.markdown.docx_to_md 转 md，再解析三张表：
      - 流程表：阶段/时间/内容/所需物资/人员/备注
      - 困难表：预计困难/解决方案
      - 物资预算表：类别/物资名称/数量规格/单价/小计

    列一律按表头名匹配，不靠列序号。解析失败返回 None（调用方回退到 LLM）。
    返回结构同 plan_sheet.read_workbook：{"环节", "困难", "物资", "物资合计"}。
    """
    act_dir = Path(act_dir)
    plan = _first_plan_docx(act_dir)
    if plan is None:
        return None

    try:
        md = markdown.docx_to_md(plan)
    except Exception:                       # 转换失败一律回退，不中断整条流程
        return None

    tables = _tables(md)
    if not tables:
        return None

    picked = _pick(tables, _STEP_ALIASES, _STEP_REQUIRED)
    if picked is None:
        return None
    step_headers, step_rows, step_mapping = picked
    steps = [row for row in _rows(step_headers, step_rows, step_mapping, _STEP_ALIASES)
             if row["内容"]]

    # 阶段列在 Word 里常是纵向合并：Markdown 可能把合并文本只放在首行，
    # 后面的行留空，这里按上一行的阶段补齐。
    last_stage = ""
    for row in steps:
        if row["阶段"]:
            last_stage = row["阶段"]
        elif last_stage:
            row["阶段"] = last_stage

    if not steps:
        return None

    difficulties: list[dict] = []
    picked = _pick(tables, _DIFFICULTY_ALIASES, _DIFFICULTY_REQUIRED)
    if picked is not None:
        headers, rows, mapping = picked
        difficulties = [row for row in _rows(headers, rows, mapping, _DIFFICULTY_ALIASES)
                        if row["预计困难"]]

    materials: list[dict] = []
    picked = _pick(tables, _MATERIAL_ALIASES, _MATERIAL_REQUIRED)
    if picked is not None:
        headers, rows, mapping = picked
        for item in _rows(headers, rows, mapping, _MATERIAL_ALIASES):
            if not item["物资名称"] or item["物资名称"] in _TOTAL_LABELS:
                continue
            if item["类别"] in _TOTAL_LABELS:
                continue
            item["单价"] = _price(item["单价"])
            # 小计按 单价×数量 重算，与 Excel 公式口径一致
            item["小计"] = plan_sheet._subtotal(item)
            materials.append(item)

    total = round(sum(item["小计"] for item in materials), 2)
    return {"环节": steps, "困难": difficulties, "物资": materials, "物资合计": total}
