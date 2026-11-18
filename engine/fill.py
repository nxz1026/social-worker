#!/usr/bin/env python3
"""模板填充引擎：模板 + 数据 → 填好的 docx / xlsx。

技术要点与 scripts/mark_placeholders.py 保持一致，避免重复踩坑：

1. 段落按「拼接后的整段文本」做替换，再整段重写。Word 常把一句话拆成
   多个 run，逐 run 替换必然漏；整段重写还能保住首个 run 的字体格式。
2. 表格按 w:tc 物理单元格遍历，不用 row.cells（合并单元格会被映射成同一
   对象，导致同一格被填多次）。
3. 页眉 / 页脚覆盖 default、first-page、even-page 三种变体，并顺带处理
   页眉页脚里的表格。
4. 含图片 / 水印 / OLE 对象的 run 原样保留，否则页眉 logo 会被删掉。

填充规则：
- data 里没有的占位符：原样留在文件里，记入 missing，不抛错
- data 里值为 None 或空串：替换成空串（清空该格），计入 filled
- 输出永远写到 dst，模板文件不会被原地修改
"""
from __future__ import annotations

import re
from pathlib import Path

import openpyxl
from docx import Document
from docx.table import _Cell

PLACEHOLDER_RE = re.compile(r"\{\{([^{}]+)\}\}")


class MissingField(Exception):
    """数据里缺某个占位符对应的值。

    当前策略是「缺字段不报错、记入 missing 让用户自己看」，因此引擎内部
    不会抛这个异常；保留类型供上层在需要严格模式时使用。
    """


# --------------------------------------------------------------------------
# docx 基础操作（与 mark_placeholders.py 同源）
# --------------------------------------------------------------------------

def _run_has_object(run) -> bool:
    """run 里是否嵌了图片 / 水印 / OLE 对象（不能随文本一起删）。"""
    for child in run._element.iter():
        if child.tag.endswith(("}drawing", "}pict", "}object", "}AlternateContent")):
            return True
    return False


def _rewrite_para(para, text: str) -> None:
    """整段重写段落文本，保留首个纯文本 run 的格式，其余纯文本 run 删除。

    含 drawing / pict / object 的 run 原样保留，避免删掉页眉 logo。
    """
    runs = para.runs
    if not runs:
        para.add_run(text)
        return

    target = next((r for r in runs if not _run_has_object(r)), None)
    if target is None:
        para.add_run(text)
        return

    target.text = text
    for run in runs:
        if run._element is target._element:
            continue
        if _run_has_object(run):
            continue
        run._element.getparent().remove(run._element)


# --------------------------------------------------------------------------
# 占位符替换
# --------------------------------------------------------------------------

def _to_text(value) -> str:
    """填充值转字符串：None → 空串，整数值的浮点去掉 .0。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _substitute(text: str, data: dict, acc: dict) -> str:
    """把 text 里的 {{字段}} 换成 data 里的值，统计 filled / missing。"""

    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        if key in data:
            acc["filled"] += 1
            return _to_text(data[key])
        acc["missing"].add(key)
        return match.group(0)          # 缺字段：原样保留

    return PLACEHOLDER_RE.sub(repl, text)


def _fill_para(para, data: dict, acc: dict) -> None:
    """填充单个段落；没有占位符的段落直接跳过，保证幂等。"""
    text = para.text
    if "{{" not in text:
        return
    new = _substitute(text, data, acc)
    if new != text:
        _rewrite_para(para, new)


def _fill_table(table, data: dict, acc: dict) -> None:
    """填充一张表的所有物理单元格，并递归处理单元格里的嵌套表格。"""
    for row in table.rows:
        for tc in row._tr.tc_lst:
            cell = _Cell(tc, table)
            for para in cell.paragraphs:
                _fill_para(para, data, acc)
            for inner in cell.tables:
                _fill_table(inner, data, acc)


def _fill_story(part, data: dict, acc: dict) -> None:
    """填充页眉 / 页脚这类 story：段落 + 表格。"""
    for para in part.paragraphs:
        _fill_para(para, data, acc)
    for table in part.tables:
        _fill_table(table, data, acc)


def _all_headers_footers(doc):
    """收集全部节的页眉页脚（默认 / 首页 / 偶数页），跳过未定义的。"""
    for section in doc.sections:
        for part in (
            section.header, section.first_page_header, section.even_page_header,
            section.footer, section.first_page_footer, section.even_page_footer,
        ):
            # is_linked_to_previous 为真说明本节没定义，读取不会创建 part
            if part.is_linked_to_previous:
                continue
            yield part


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def render_docx(src: Path, dst: Path, data: dict) -> dict:
    """读 docx 模板，替换 {{字段}} 后写到 dst。

    覆盖正文段落、所有表格单元格、所有节的页眉页脚。
    返回 {"filled": int, "missing": list[str]}。
    """
    src, dst = Path(src), Path(dst)
    doc = Document(str(src))
    acc: dict = {"filled": 0, "missing": set()}

    for para in doc.paragraphs:
        _fill_para(para, data, acc)
    for table in doc.tables:
        _fill_table(table, data, acc)
    for part in _all_headers_footers(doc):
        _fill_story(part, data, acc)

    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(dst))
    return {"filled": acc["filled"], "missing": sorted(acc["missing"])}


def render_xlsx(src: Path, dst: Path, data: dict) -> dict:
    """读 xlsx 模板，替换单元格里的 {{字段}} 后写到 dst。

    openpyxl 只改 value，样式、合并单元格与公式原样保留。
    返回 {"filled": int, "missing": list[str]}。
    """
    src, dst = Path(src), Path(dst)
    workbook = openpyxl.load_workbook(str(src))
    acc: dict = {"filled": 0, "missing": set()}

    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                value = cell.value
                if not isinstance(value, str) or "{{" not in value:
                    continue
                new = _substitute(value, data, acc)
                if new != value:
                    cell.value = new

    dst.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(dst))
    workbook.close()
    return {"filled": acc["filled"], "missing": sorted(acc["missing"])}


def render(src: Path, dst: Path, data: dict) -> dict:
    """按后缀分发到 render_docx / render_xlsx。"""
    suffix = Path(src).suffix.lower()
    if suffix == ".docx":
        return render_docx(src, dst, data)
    if suffix == ".xlsx":
        return render_xlsx(src, dst, data)
    raise ValueError(f"不支持的模板格式：{Path(src).name}")


def _scan_table(table, scan_text) -> None:
    """递归扫描表格（含单元格内嵌套表格）里的占位符。"""
    for row in table.rows:
        for tc in row._tr.tc_lst:
            cell = _Cell(tc, table)
            for para in cell.paragraphs:
                scan_text(para.text)
            for inner in cell.tables:
                _scan_table(inner, scan_text)


def scan_placeholders(src: Path) -> list[str]:
    """列出模板里所有占位符（含花括号，去重排序）。docx / xlsx 都支持。

    例：["{{合计金额}}", "{{活动名称}}", "{{项目名称}}"]
    """
    src = Path(src)
    found: set[str] = set()

    def scan_text(text: str) -> None:
        for match in PLACEHOLDER_RE.finditer(text or ""):
            found.add(match.group(0))

    if src.suffix.lower() == ".docx":
        doc = Document(str(src))
        for para in doc.paragraphs:
            scan_text(para.text)
        for table in doc.tables:
            _scan_table(table, scan_text)
        for part in _all_headers_footers(doc):
            for para in part.paragraphs:
                scan_text(para.text)
            for table in part.tables:
                _scan_table(table, scan_text)
    elif src.suffix.lower() == ".xlsx":
        workbook = openpyxl.load_workbook(str(src))
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str):
                        scan_text(cell.value)
        workbook.close()
    else:
        raise ValueError(f"不支持的模板格式：{src.name}")

    return sorted(found)
