#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.7 模板占位符统一改造

把 /srv/social-work/templates/library/ 下的 docx / xlsx 模板里的自然语言、
星号、示例数据统一替换成 {{字段名}} 占位符。

设计要点：
1. 段落按「拼接后的整段文本」做正则替换，再整段重写，规避 Word 把一句话
   拆成多个 run 导致的字符串替换失败。
2. 表格按 w:tc 物理单元格操作，不用 row.cells（合并单元格会被映射成同一对象）。
3. 页眉同时覆盖 default / first-page / even-page 三种变体。
4. 全部规则幂等：命中即改、未命中不计，脚本二次运行改动数为 0，且不改动文件。

用法：
    python3 scripts/mark_placeholders.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from docx import Document
from docx.table import _Cell

import openpyxl

# 模板目录（只允许改这里的文件）
TEMPLATE_DIR = Path("/srv/social-work/templates/library")


# --------------------------------------------------------------------------
# 基础能力
# --------------------------------------------------------------------------

def _run_has_object(run) -> bool:
    """判断 run 里是否嵌了图片 / 水印 / OLE 对象（不能随文本一起删）。"""
    for child in run._element.iter():
        if child.tag.endswith(("}drawing", "}pict", "}object", "}AlternateContent")):
            return True
    return False


def set_para_text(para, text: str) -> None:
    """整段重写段落文本，保留首个文本 run 的格式，其余纯文本 run 删除。

    Word 常把「（填写活动名称）」拆成多个 run，逐 run 替换必然失败，
    因此统一按整段文本重写。但页眉里的 logo、水印是以 w:pict 形式
    挂在 run 上的，删 run 会把图片一起删掉，必须原样保留。
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
    for r in runs:
        if r._element is target._element:
            continue
        if _run_has_object(r):
            continue
        r._element.getparent().remove(r._element)


def set_cell_text(cell: _Cell, text: str) -> None:
    """重写单元格文本：只保留首个段落，其余段落删除。"""
    paras = cell.paragraphs
    if not paras:
        cell.text = text
        return
    for p in paras[1:]:
        p._p.getparent().remove(p._p)
    set_para_text(paras[0], text)


def _apply_para_rules(paragraphs, rules: dict) -> int:
    """对一批段落依次套用 {正则: 替换} 规则，返回改动处数。

    rules 的值为 None 表示整段删除（用于清掉别的项目名段落）。
    """
    n = 0
    for para in list(paragraphs):
        new = para.text
        for pat, repl in rules.items():
            if repl is None:
                if re.search(pat, new):
                    para._p.getparent().remove(para._p)
                    n += 1
                    new = None
                    break
                continue
            upd = re.sub(pat, repl, new)
            if upd != new:
                new = upd
                n += 1
        if new is not None and new != para.text:
            set_para_text(para, new)
    return n


def _is_indexed_variant(current: str, target: str) -> bool:
    """current 是否已经是 target 的带序号形式（{{X}} → {{X_1}}）。

    默认模式重跑时不能把已经加过序号的格子改回 {{X}}，否则 --row-index
    的成果会被覆盖，两种模式来回跑就不幂等了。
    """
    if not isinstance(current, str) or not isinstance(target, str):
        return False
    if not (target.startswith("{{") and target.endswith("}}")):
        return False
    base = target[2:-2].strip()
    if not base or "{{" in base or "}}" in base:
        return False
    return re.fullmatch(r"\{\{%s_\d+\}\}" % re.escape(base), current.strip()) is not None


def _apply_cell_ops(cell: _Cell, ops) -> int:
    """对单元格套用一组操作，返回改动处数。

    支持三种操作：
      "文本"                      → 整格重写
      ("append", "{{占位符}}")    → 在已有固定文字后追加占位符（已含则跳过）
      ("sub", "旧", "新")         → 按段落做子串替换，保留段落结构
    """
    if not isinstance(ops, (list, tuple)) or isinstance(ops, str):
        ops = [ops]
    elif isinstance(ops, tuple) and ops and ops[0] in ("append", "sub"):
        ops = [ops]

    n = 0
    for op in ops:
        if isinstance(op, str):
            if cell.text != op and not _is_indexed_variant(cell.text, op):
                set_cell_text(cell, op)
                n += 1
        elif op[0] == "append":
            ph = op[1]
            first = cell.paragraphs[0] if cell.paragraphs else None
            if first is None:
                set_cell_text(cell, ph)
                n += 1
                continue
            cur = first.text
            if ph in cur:
                continue
            set_para_text(first, cur.rstrip() + ph)
            n += 1
        elif op[0] == "sub":
            old, new = op[1], op[2]
            for p in cell.paragraphs:
                if old in p.text:
                    set_para_text(p, p.text.replace(old, new))
                    n += 1
        else:
            raise ValueError(f"未知单元格操作: {op!r}")
    return n


def _header_paragraphs(doc):
    """收集全部页眉（默认 / 首页 / 偶数页）里的段落，含页眉表格内段落。"""
    out = []
    for section in doc.sections:
        for hf in (section.header, section.first_page_header, section.even_page_header):
            # is_linked_to_previous 不会创建 header part，避免给模板凭空加页眉
            if hf.is_linked_to_previous:
                continue
            out.extend(hf.paragraphs)
            for tb in hf.tables:
                for row in tb.rows:
                    for tc in row._tr.tc_lst:
                        out.extend(_Cell(tc, tb).paragraphs)
    return out


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def replace_in_doc(p: Path, para_map: dict, cell_map: dict, header_map: dict) -> int:
    """替换 docx 正文段落 / 表格单元格 / 页眉，返回改动处数。

    para_map   : {正则: 替换文本}，None 表示删除该段落
    cell_map   : {(表序号, 行, 列): 操作}，行列均为 w:tc 物理下标（0 起）
    header_map : {正则: 替换文本}
    """
    doc = Document(str(p))
    n = 0

    n += _apply_para_rules(doc.paragraphs, para_map)

    if cell_map:
        tables = doc.tables
        for (ti, ri, ci), ops in cell_map.items():
            tb = tables[ti]
            tcs = tb.rows[ri]._tr.tc_lst
            if ci >= len(tcs):
                raise IndexError(f"{p.name} 表{ti + 1} r{ri} 只有 {len(tcs)} 个物理单元格，取不到第 {ci} 列")
            n += _apply_cell_ops(_Cell(tcs[ci], tb), ops)

    if header_map:
        n += _apply_para_rules(_header_paragraphs(doc), header_map)

    if n:
        doc.save(str(p))
    return n


def replace_in_xlsx(p: Path, cell_map: dict) -> int:
    """替换 xlsx 单元格内容，返回改动处数。

    cell_map : {"A3": 操作}，操作形式同 _apply_cell_ops（xlsx 只用到文本与 sub）
    """
    wb = openpyxl.load_workbook(str(p))
    n = 0
    for ws in wb.worksheets:
        for coord, ops in cell_map.items():
            cell = ws[coord]
            if not isinstance(ops, (list, tuple)) or isinstance(ops, str):
                ops = [ops]
            elif isinstance(ops, tuple) and ops and ops[0] in ("append", "sub"):
                ops = [ops]
            for op in ops:
                cur = cell.value
                if isinstance(op, str):
                    if cur != op and not _is_indexed_variant(cur, op):
                        cell.value = op
                        n += 1
                elif op[0] == "sub":
                    if isinstance(cur, str) and op[1] in cur:
                        cell.value = cur.replace(op[1], op[2])
                        n += 1
                else:
                    raise ValueError(f"未知单元格操作: {op!r}")
    if n:
        wb.save(str(p))
    return n


def renumber_column(p: Path, table_idx: int, col_idx: int) -> int:
    """把表格某列的序号从 1 起重排为连续编号，返回改动处数。

    跳过表头行；遇到「合计」这类非数字首格即停止，不误改合计行。
    """
    doc = Document(str(p))
    tb = doc.tables[table_idx]
    n = 0
    seq = 0
    for ri, row in enumerate(tb.rows):
        tcs = row._tr.tc_lst
        if len(tcs) <= col_idx:
            break
        cell = _Cell(tcs[col_idx], tb)
        cur = cell.text.strip()
        if ri == 0:
            continue  # 表头
        if cur and not re.fullmatch(r"\d+", cur):
            break  # 合计行 / 说明行
        seq += 1
        want = str(seq)
        if cur != want:
            set_cell_text(cell, want)
            n += 1
    if n:
        doc.save(str(p))
    return n


# --------------------------------------------------------------------------
# P3.1 多行表格占位符加行号（{{字段}} → {{字段_1}}、{{字段_2}}……）
# --------------------------------------------------------------------------

PH_RE = re.compile(r"\{\{([^{}]+)\}\}")


def add_row_index(text: str, seq: int) -> str:
    """把一段文本里的 {{字段}} 改成 {{字段_seq}}。

    已经带 _数字 后缀的占位符原样保留，因此脚本可以反复运行而不会
    变成 {{字段_1_1}}（幂等）。
    """
    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        if re.fullmatch(r".+_\d+", key):
            return match.group(0)
        return "{{%s_%d}}" % (key, seq)

    return PH_RE.sub(repl, text)


def index_docx_rows(p: Path, table_idx: int, rows, cols: dict) -> int:
    """给 docx 表格指定数据行里的占位符加序号，返回改动处数。

    rows 中的第一个行序号对应 _1，依次递增；cols 为 {物理列: 字段名}。
    单元格为空时按字段名补占位符（决算表这类原本没标占位符的模板需要），
    非空则只给已有占位符加后缀，不动其它文字。
    """
    doc = Document(str(p))
    tb = doc.tables[table_idx]
    n = 0

    for seq, ri in enumerate(rows, start=1):
        tcs = tb.rows[ri]._tr.tc_lst
        for ci, field in cols.items():
            if ci >= len(tcs):
                raise IndexError(f"{p.name} 表{table_idx + 1} r{ri} 只有 {len(tcs)} 个物理单元格，取不到第 {ci} 列")
            cell = _Cell(tcs[ci], tb)
            text = cell.text
            if "{{" in text:
                for para in cell.paragraphs:
                    new = add_row_index(para.text, seq)
                    if new != para.text:
                        set_para_text(para, new)
                        n += 1
            elif not text.strip() and field:
                # 空数据格：按列名补 {{字段_序号}}
                set_cell_text(cell, "{{%s_%d}}" % (field, seq))
                n += 1

    if n:
        doc.save(str(p))
    return n


def index_xlsx_rows(p: Path, sheet_name: str, rows, cols: dict) -> int:
    """给 xlsx 指定数据行的单元格占位符加序号，返回改动处数。

    cols 为 {列字母: 字段名}；空单元格按字段名补占位符。
    """
    wb = openpyxl.load_workbook(str(p))
    ws = wb[sheet_name] if sheet_name else wb.worksheets[0]
    n = 0

    for seq, ri in enumerate(rows, start=1):
        for col, field in cols.items():
            cell = ws[f"{col}{ri}"]
            value = cell.value
            if isinstance(value, str) and "{{" in value:
                new = add_row_index(value, seq)
                if new != value:
                    cell.value = new
                    n += 1
            elif (value is None or (isinstance(value, str) and not value.strip())) and field:
                cell.value = "{{%s_%d}}" % (field, seq)
                n += 1

    if n:
        wb.save(str(p))
    return n


# --------------------------------------------------------------------------
# 各模板规则
# --------------------------------------------------------------------------

# 模板从旧文件另存而来，文档属性里残留的作者名与外来标题
JUNK_TITLE = "家庭同樂迎千禧活動 報名表"


def clean_metadata(p: Path) -> int:
    """清掉文档属性里的作者 / 最后修改人 / 残留标题，返回改动处数。

    正文里已无真人姓名，但 docProps 里还留着「赖敏」「刘佳」等，
    模板对外分发前需要一并清掉。
    """
    doc = Document(str(p))
    cp = doc.core_properties
    n = 0
    if cp.author:
        cp.author = ""
        n += 1
    if cp.last_modified_by:
        cp.last_modified_by = ""
        n += 1
    if cp.title.strip() == JUNK_TITLE:
        cp.title = ""
        n += 1
    if n:
        doc.save(str(p))
    return n


def _cells(ti: int, rows, cols: dict) -> dict:
    """批量生成单元格规则：{(表, 行, 列): 操作}"""
    out = {}
    for ri in rows:
        for ci, ops in cols.items():
            out[(ti, ri, ci)] = ops
    return out


# 决算表.docx
JUE_SUAN = dict(
    para={
        r"^XX$": "{{项目名称}}",
        r"^项目名称：（填写项目名称）$": "项目名称：{{项目名称}}",
        r"^活动名称：（填写活动名称）$": "活动名称：{{活动名称}}",
        r"^活动时间：（填写活动时间）$": "活动时间：{{活动时间}}",
    },
    cells={
        # 合计行（金额+备注为纵向合并格，共 2 个物理格）：金额格放合计占位符
        (0, 7, 1): "{{合计金额}}",
    },
    header={r"XX项目": "{{项目名称}}"},
    renumber=(0, 0),
)

# 计划书.docx
JI_HUA = dict(
    para={
        r"^XX活动$": "{{活动名称}}",
        r"档案编号:CDYJR2026\s*$": "档案编号:{{项目编号}}",
        r"^(活动名称：)\s*$": r"\1{{活动名称}}",
        r"^(活动时间：)\s*$": r"\1{{活动时间}}",
        r"^(活动地点：)\s*$": r"\1{{活动地点}}",
        r"^(服务对象：)\s*$": r"\1{{服务对象}}",
        r"^(预计参与人数：)\s*$": r"\1{{预计人数}}",
        r"^(项目社工人数：)\s*$": r"\1{{项目社工}}",
        r"^(志愿者人数：)\s*$": r"\1{{志愿者人数}}",
        r"^(目标人群：)\s*$": r"\1{{目标人群}}",
        r"^(宣传方式：)\s*$": r"\1{{宣传方式}}",
        # 原文误把「五、活动具体安排及内容」粘在这一段末尾，一并删掉
        r"^(筛选方式：)\s*五、活动具体安排及内容\s*$": r"\1{{筛选方式}}",
    },
    cells={
        # 表1 流程表：内容 / 所需物资 / 人员
        **_cells(0, range(1, 6), {2: "{{环节内容}}", 3: "{{环节物资}}", 4: "{{环节人员}}"}),
        # 表2 困难预案
        **_cells(1, range(1, 3), {0: "{{预计困难}}", 1: "{{解决方案}}"}),
        # 表3 物资预算：类别列留空
        **_cells(2, range(1, 6), {1: "{{物资名称}}", 2: "{{物资规格}}", 3: "{{物资单价}}", 4: "{{物资小计}}"}),
        # 表3 末行为合并的合计行（1 格 + 跨 4 格）
        (2, 6, 0): "合计",
        (2, 6, 1): "{{物资小计}}",
    },
    header={r"XX项目": "{{项目名称}}"},
    renumber=None,
)

# 活动目录.docx
HUO_DONG_MU_LU = dict(
    para={
        r"^XX活动管理$": "{{活动名称}}管理",
        r"项目编号CDYJR2026005-C-\s*$": "项目编号{{项目编号}}",
        r"(项目社工：)\s*\S+(?=\s+项目负责人：)": r"\1{{项目社工}}",
        r"(项目负责人：)\s*\S+\s*$": r"\1{{项目负责人}}",
        r"202X\s*年X月X日\s*$": "{{年月日}}",
    },
    cells=_cells(0, range(1, 16), {2: "{{归档人}}"}),
    header={r"XX项目": "{{项目名称}}"},
    renumber=(0, 0),
)

# 对公付款申请单 .docx
FU_KUAN = dict(
    para={
        r"^(日期：)\s*$": r"\1{{年月日}}",
        # 该段前有缩进空格，必须容忍行首空白
        r"^(\s*出纳：)(\s+)(经办人：)\s*$": r"\1{{出纳}}\2\3{{经办人}}",
    },
    cells={
        (0, 0, 1): "{{收款单位}}",
        # r0 第 3 列是「付 款 原 因」列标题，不是填写格：保留标题不动，
        # 付款原因内容格是 r1~r4 纵向合并的那一格（见下），只填一次
        (0, 1, 1): "{{帐号}}",
        (0, 1, 2): "{{付款原因}}",
        (0, 2, 1): "{{开户行}}",
        (0, 2, 2): "{{付款原因}}",
        (0, 3, 1): "￥{{金额}}",
        (0, 3, 2): "{{付款原因}}",
        (0, 4, 1): "{{人民币大写}}",
        (0, 4, 2): "{{付款原因}}",
    },
    header={},
    renumber=None,
)

# 总结报告.docx —— 表1 第 3 列按「阶段前缀 + 内容类型」映射
ZONG_JIE = dict(
    para={
        r"^(活动名称：\s*)XX活动$": r"\1{{活动名称}}",
        r"^(对\s+象：)\s*$": r"\1{{服务对象}}",
        r"^(时\s+间：)\s*$": r"\1{{活动时间}}",
        r"^(地\s+点：)\s*$": r"\1{{活动地点}}",
        r"^(人\s+数：)\s*人\s*$": r"\1{{实际人数}} 人",
        r"^(人手分配：).*$": r"\1{{人手分配}}",
        r"^(执行社工：)(\s+)(项目负责人：)\s*$": r"\1{{项目社工}}\2\3{{项目负责人}}",
        r"^(执行社工：)\s*$": r"\1{{项目社工}}",
        r"202X年X月X日": "{{年月日}}",
    },
    cells={
        (0, 1, 2): ("append", "{{策划目标}}"),
        (0, 2, 2): ("append", "{{策划问题}}"),
        (0, 3, 2): ("append", "{{策划行动}}"),
        (0, 4, 2): ("append", "{{准备目标}}"),
        (0, 5, 2): ("append", "{{准备问题}}"),
        (0, 6, 2): ("append", "{{准备行动}}"),
        (0, 7, 2): ("append", "{{执行目标}}"),
        (0, 8, 2): ("append", "{{执行内容}}"),
        (0, 9, 2): ("append", "{{执行表现}}"),
        (0, 10, 2): ("append", "{{执行挑战}}"),
        (0, 11, 2): ("append", "{{执行行动}}"),
        (0, 12, 2): ("append", "{{宣传目标}}"),
        (0, 13, 2): ("append", "{{宣传成效}}"),
        (0, 14, 2): ("append", "{{总结目标}}"),
        (0, 15, 2): ("append", "{{总结挑战}}"),
        (0, 16, 2): ("append", "{{总结行动}}"),
        (0, 17, 1): ("append", "{{工作员表现}}"),
        (0, 18, 1): ("append", "{{工作员反思}}"),
        (0, 19, 1): ("append", "{{跟进工作}}"),
    },
    header={
        r"“亲邻惠里·网格共治”街区活力营建项目": "{{项目名称}}",
        r"XX项目": "{{项目名称}}",
    },
    renumber=None,
)

# 满意度测评汇总表.docx
MAN_YI_DU = dict(
    para={
        r"^XX项目XX活动$": "{{项目名称}}{{活动名称}}",
        r"^(项目名称：)\s*$": r"\1{{项目名称}}",
        r"^(活动时间：)\s*$": r"\1{{活动时间}}",
        r"^(活动地点：)\s*$": r"\1{{活动地点}}",
        r"^(执行社工：)\s*$": r"\1{{项目社工}}",
        r"^\s*年\s+月\s+日\s*$": "{{年月日}}",
    },
    cells={
        (0, 1, 0): "{{实际人数}}",
        (0, 1, 1): "{{测评人数}}",
        (0, 1, 2): "{{总得分}}",
        (0, 1, 3): "{{满意率}}",
        (0, 1, 4): "",  # 备注留空
        (0, 2, 0): ("sub", "X人", "{{测评人数}}人"),
        (0, 3, 0): [("sub", "X分", "{{平均分}}分"), ("sub", "结果为X", "结果为{{评定等级}}")],
    },
    header={r"\*+项目\s*$": "{{项目名称}}"},
    renumber=None,
)

# 服务对象满意度调查表.docx
FU_WU_DUI_XIANG = dict(
    para={
        # 正文里是别的项目的具体活动名，整段短语换成占位符
        r"深耕网格察民情·商居共治破难题惠民社区街区活力营建项目走访调研": "{{项目名称}}{{活动名称}}",
        r"^\s*年\s+月\s+日\s*$": "{{年月日}}",
    },
    cells={},
    header={r"^\s*项目\s*$": "{{项目名称}}"},
    renumber=None,
)

# 物资领取表.docx
WU_ZI = dict(
    para={
        r"档案编号：CDYJR202\s*$": "档案编号：{{项目编号}}",
        r"^(活动名称：)\s*$": r"\1{{活动名称}}",
        r"^日期/时间：.*$": "日期/时间：{{活动时间}}                  负责社工：{{项目社工}}           领取人数：{{领取人数}}",
    },
    cells=_cells(0, range(1, 14), {1: "{{领取人姓名}}", 2: "{{领取人电话}}", 3: "{{领取物品}}"}),
    header={r"XX项目": "{{项目名称}}"},
    renumber=None,
)

# 劳务领取表.docx
LAO_WU = dict(
    para={
        r"项目编号CDYJR2026005-[cC]-\s*$": "项目编号{{项目编号}}",
        r"^(经办人：)(\s+)(项目负责人：)\s*$": r"\1{{经办人}}\2\3{{项目负责人}}",
        r"^(时\s+间：)(\s+)(时\s+间：)\s*$": r"\1{{年月日}}\2\3{{年月日}}",
    },
    cells={
        (0, 0, 0): "项目名称：{{项目名称}}",
        (0, 1, 0): "活动名称：{{活动名称}}",
        (0, 2, 0): "活动时间：{{活动时间}}",
        (0, 3, 0): "活动地点：{{活动地点}}",
        (0, 5, 1): "{{服务事项}}",
        (0, 5, 2): "{{补贴金额}}",
        (0, 5, 3): "{{个税扣缴}}",
        (0, 5, 4): "{{税后金额}}",
    },
    header={},
    renumber=None,
)

# 志愿者补贴领取表.docx
ZHI_YUAN_ZHE = dict(
    para={
        r"档案编号:CDYJR202\*+-\*+\s*$": "档案编号:{{项目编号}}",
        r"^(活动名称：)(\s+)(日期/时间：)\s*$": r"\1{{活动名称}}\2\3{{活动时间}}",
        r"^(负责社工：)(\s+)(签到人数：)\s*$": r"\1{{项目社工}}\2\3{{实际人数}}",
    },
    cells=_cells(0, range(1, 17), {
        1: "{{志愿者姓名}}",
        2: "{{志愿者电话}}",
        3: "{{志愿者身份证}}",
        4: "{{服务内容}}",
        5: "{{金额}}",
    }),
    header={r"\*+项目\s*$": "{{项目名称}}"},
    renumber=None,
)

# 监督记录.docx
JIAN_DU = dict(
    para={
        # 别的项目名，整段删除
        r"^公平街道惠民社区2026年社区保障资金项目\s*$": None,
        r"“亲邻惠里·网格共治”街区活力营建项目\s*$": "{{项目名称}}",
    },
    cells={
        (0, 0, 1): "{{活动名称}}",
        (0, 1, 1): "{{活动时间}}",
        (0, 2, 1): "{{活动内容}}",
        (0, 3, 1): "",  # 监委会签字留空
    },
    header={},
    renumber=None,
)

# 简报.docx
JIAN_BAO = dict(
    para={
        r"^XX社区服务简报$": "{{项目名称}}服务简报",
        r"202X年X月X日\s*$": "{{年月日}}",
        r"^XX活动$": "{{活动名称}}",
        r"^为深入贯彻落实温江区\*+，扎实推进\*\*（党建风格）$": "{{简报导语}}",
        r"^诉求描述$": "{{诉求描述}}",
        r"^达到目标$": "{{达到目标}}",
    },
    cells={},
    header={
        r"“和谐万盛·睦邻家园”社区自组织队伍培育项目": "{{项目名称}}",
        r"XX项目": "{{项目名称}}",
    },
    renumber=None,
)

# 项目管理目录.docx
XIANG_MU_MU_LU = dict(
    para={
        r"^\*+项目管理$": "{{项目名称}}管理",
        r"(项目编号：)\s*CDYJR202\*+\s*$": r"\1{{项目编号}}",
        r"^(项目社工：)(\s+)(项目负责人：)\s*$": r"\1{{项目社工}}\2\3{{项目负责人}}",
        r"^\s*年\s+月\s+日\s*$": "{{年月日}}",
    },
    cells={
        # r1 的示例数据全部清掉，归档人列统一填占位符
        (0, 1, 2): "",
        (0, 1, 3): "",
        (0, 1, 5): "",
        **_cells(0, range(1, 20), {4: "{{归档人}}"}),
    },
    header={r"\*+项目\s*$": "{{项目名称}}"},
    renumber=None,
)

# 报销单.xlsx
BAO_XIAO = {
    "A3": "费用归属部门：{{费用归属部门}}",
    "B3": "填表日期：{{填表日期}}",
    "C12": "=SUM(C6:J30)",
    "C13": "{{人民币大写}}",
    **{f"B{r}": "{{内容摘要}}" for r in range(6, 12)},
    **{f"C{r}": "{{报销金额}}" for r in range(6, 12)},
}

# 内部审计类表格不参与模板改造（清单见 config/excluded_templates.txt）


DOC_JOBS = [
    ("决算表.docx", JUE_SUAN),
    ("计划书.docx", JI_HUA),
    ("活动目录.docx", HUO_DONG_MU_LU),
    ("对公付款申请单 .docx", FU_KUAN),
    ("总结报告.docx", ZONG_JIE),
    ("满意度测评汇总表.docx", MAN_YI_DU),
    ("服务对象满意度调查表.docx", FU_WU_DUI_XIANG),
    ("物资领取表.docx", WU_ZI),
    ("劳务领取表.docx", LAO_WU),
    ("志愿者补贴领取表.docx", ZHI_YUAN_ZHE),
    ("监督记录.docx", JIAN_DU),
    ("简报.docx", JIAN_BAO),
    ("项目管理目录.docx", XIANG_MU_MU_LU),
]

XLSX_JOBS = [
    ("报销单.xlsx", BAO_XIAO),
]


# --------------------------------------------------------------------------
# P3.1 需要加行号的数据行配置
# --------------------------------------------------------------------------
# 每个表项：{"table": 表序号(0 起), "rows": 数据行(0 起, 不含表头/合计行),
#            "cols": {物理列下标: 字段名}}
# 每个表的行号都从 1 重新开始。合计行不在 rows 里，因此 {{合计金额}} 不受影响。
ROW_INDEX_DOCX_JOBS = [
    # 决算表 表1：6 行明细（物品名称 / 单价 / 规格 / 金额 / 备注）
    ("决算表.docx", [
        {"table": 0, "rows": range(1, 7),
         "cols": {1: "物品名称", 2: "单价", 3: "规格", 4: "金额", 5: "备注"}},
    ]),
    # 物资领取表 表1：13 行
    ("物资领取表.docx", [
        {"table": 0, "rows": range(1, 14),
         "cols": {1: "领取人姓名", 2: "领取人电话", 3: "领取物品"}},
    ]),
    # 志愿者补贴领取表 表1：16 行
    ("志愿者补贴领取表.docx", [
        {"table": 0, "rows": range(1, 17),
         "cols": {1: "志愿者姓名", 2: "志愿者电话", 3: "志愿者身份证",
                  4: "服务内容", 5: "金额"}},
    ]),
    # 劳务领取表 表1：只有 r5 一个数据行；r6 是合并的「备注」行，不属于明细
    ("劳务领取表.docx", [
        {"table": 0, "rows": range(5, 6),
         "cols": {1: "服务事项", 2: "补贴金额", 3: "个税扣缴", 4: "税后金额"}},
    ]),
    # 计划书 三张表各自从 _1 开始
    ("计划书.docx", [
        {"table": 0, "rows": range(1, 6),
         "cols": {2: "环节内容", 3: "环节物资", 4: "环节人员"}},
        {"table": 1, "rows": range(1, 3),
         "cols": {0: "预计困难", 1: "解决方案"}},
        {"table": 2, "rows": range(1, 6),
         "cols": {1: "物资名称", 2: "物资规格", 3: "物资单价", 4: "物资小计"}},
    ]),
]

# xlsx 用列字母定位：{"sheet": 工作表名, "rows": 行号(1 起), "cols": {列字母: 字段名}}
ROW_INDEX_XLSX_JOBS = [
    ("报销单.xlsx", [
        {"sheet": "费用报销单", "rows": range(6, 12),
         "cols": {"B": "内容摘要", "C": "报销金额"}},
    ]),
]


def run_row_index() -> int:
    """按 ROW_INDEX_*_JOBS 给多行表格的占位符加行号，返回总改动处数（失败 -1）。"""
    if not TEMPLATE_DIR.is_dir():
        print(f"模板目录不存在: {TEMPLATE_DIR}", file=sys.stderr)
        return -1

    total = 0
    print(f"模板目录: {TEMPLATE_DIR}")
    print("模式: 多行表格占位符加行号（--row-index）")
    print("-" * 56)

    for name, specs in ROW_INDEX_DOCX_JOBS:
        p = TEMPLATE_DIR / name
        if not p.exists():
            print(f"{name} → 文件缺失，跳过")
            continue
        n = 0
        for spec in specs:
            n += index_docx_rows(p, spec["table"], spec["rows"], spec["cols"])
        total += n
        print(f"{name} → 改 {n} 处")

    for name, specs in ROW_INDEX_XLSX_JOBS:
        p = TEMPLATE_DIR / name
        if not p.exists():
            print(f"{name} → 文件缺失，跳过")
            continue
        n = 0
        for spec in specs:
            n += index_xlsx_rows(p, spec["sheet"], spec["rows"], spec["cols"])
        total += n
        print(f"{name} → 改 {n} 处")

    print("-" * 56)
    print(f"合计改动 {total} 处（表格数据行占位符）")
    return total


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="模板占位符改造")
    parser.add_argument("--row-index", action="store_true",
                        help="只给多行表格的数据行占位符加序号（{{X}} → {{X_1}}）")
    args = parser.parse_args(argv)
    if args.row_index:
        return 0 if run_row_index() >= 0 else 1

    if not TEMPLATE_DIR.is_dir():
        print(f"模板目录不存在: {TEMPLATE_DIR}", file=sys.stderr)
        return 1

    total = 0
    print(f"模板目录: {TEMPLATE_DIR}")
    print("-" * 56)

    for name, cfg in DOC_JOBS:
        p = TEMPLATE_DIR / name
        if not p.exists():
            print(f"{name} → 文件缺失，跳过")
            continue
        n = replace_in_doc(p, cfg["para"], cfg["cells"], cfg["header"])
        if cfg["renumber"]:
            n += renumber_column(p, *cfg["renumber"])
        total += n
        print(f"{name} → 改 {n} 处")

    for name, cell_map in XLSX_JOBS:
        p = TEMPLATE_DIR / name
        if not p.exists():
            print(f"{name} → 文件缺失，跳过")
            continue
        n = replace_in_xlsx(p, cell_map)
        total += n
        print(f"{name} → 改 {n} 处")

    print("-" * 56)
    print("文档属性清理（作者 / 最后修改人 / 残留标题）：")
    meta = 0
    for name, _cfg in DOC_JOBS:
        p = TEMPLATE_DIR / name
        if not p.exists():
            continue
        m = clean_metadata(p)
        meta += m
        print(f"  {name} → 改 {m} 处")

    print("-" * 56)
    print(f"合计改动 {total} 处（正文/表格/页眉/单元格）")
    print(f"合计改动 {meta} 处（文档属性）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
