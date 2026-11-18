#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板排版回退：占位符改造后页数变多的两个模板恢复原页数。

只处理：
  - 志愿者补贴领取表.docx（2 页 -> 1 页）
  - 计划书.docx（3 页 -> 2 页）
简报.docx 不动。

用法：
  python3 scripts/fix_layout.py
"""

from pathlib import Path

from docx import Document
from docx.enum.table import WD_ROW_HEIGHT_RULE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, Twips

# 模板库目录
LIB_DIR = Path("/srv/social-work/templates/library")

# 志愿者表目标列宽比例（序号/姓名/联系方式/身份证/服务内容/金额/签字）
VOLUNTEER_RATIOS = [0.08, 0.12, 0.16, 0.26, 0.20, 0.10, 0.08]
VOLUNTEER_ROW_PT = 24.0


def _log(msg: str) -> None:
    """统一打印改动记录。"""
    print(msg)


def _usable_width_twips(table) -> int:
    """按表格所在节的页面宽度与左右页边距，算出可用宽度（twips）。"""
    root = table._tbl.getroottree().getroot()
    body = root.find(qn("w:body"))
    sect = body.find(qn("w:sectPr"))
    pg_sz = sect.find(qn("w:pgSz"))
    pg_mar = sect.find(qn("w:pgMar"))
    page_w = int(pg_sz.get(qn("w:w")))
    left = int(pg_mar.get(qn("w:left")))
    right = int(pg_mar.get(qn("w:right")))
    return page_w - left - right


def set_col_widths(table, ratios: list[float]) -> None:
    """按比例设置列宽，总和等于页面可用宽度。

    同时改写 tblGrid / tblW / 每个单元格 tcW，避免 Word 与 LibreOffice
    各按一半数据排版导致列宽不一致。
    """
    total = _usable_width_twips(table)
    total_ratio = sum(ratios)
    widths = [int(round(total * r / total_ratio)) for r in ratios]
    # 四舍五入误差补到最后一列，保证总和精确
    widths[-1] += total - sum(widths)

    # 1) 表格总宽
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(total))
    tbl_w.set(qn("w:type"), "dxa")

    # 2) 列网格
    grid_cols = table._tbl.tblGrid.gridCol_lst
    for gc, w in zip(grid_cols, widths):
        gc.set(qn("w:w"), str(w))

    # 3) 每个单元格（考虑横向合并 gridSpan）
    for tr in table._tbl.tr_lst:
        idx = 0
        for tc in tr.tc_lst:
            tc_pr = tc.get_or_add_tcPr()
            span_el = tc_pr.find(qn("w:gridSpan"))
            span = int(span_el.get(qn("w:val"))) if span_el is not None else 1
            cell_w = sum(widths[idx : idx + span])
            idx += span

            tc_w = tc_pr.find(qn("w:tcW"))
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                tc_pr.insert(0, tc_w)
            tc_w.set(qn("w:w"), str(cell_w))
            tc_w.set(qn("w:type"), "dxa")

    _log(
        f"  列宽: {', '.join(f'{w}twips({w / total:.0%})' for w in widths)}"
        f"  合计 {sum(widths)}twips"
    )


def set_table_line_spacing(table, pt: float = 12.5) -> None:
    """表格单元格统一固定行距，避免继承正文 1.5 倍行距把行撑高。"""
    count = 0
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                para.paragraph_format.line_spacing = Pt(pt)
                count += 1
    _log(f"  表内行距: {count} 个单元格段落 -> 固定 {pt}pt")


def disable_snap_to_grid(table) -> None:
    """关闭单元格段落「对齐到文档网格」。

    本文档 docGrid 行距为 18pt，表格内每行文字都被抬到 18pt，
    10pt 字号折成两行时行高仍接近 37pt。关闭后行高才随字号收缩。
    """
    count = 0
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                p_pr = para._p.get_or_add_pPr()
                el = p_pr.find(qn("w:snapToGrid"))
                if el is None:
                    el = OxmlElement("w:snapToGrid")
                    p_pr.insert_element_before(
                        el,
                        "w:spacing",
                        "w:ind",
                        "w:jc",
                        "w:rPr",
                        "w:sectPr",
                    )
                el.set(qn("w:val"), "0")
                count += 1
    _log(f"  关闭表格网格对齐: {count} 个单元格段落")


def set_row_height(table, pt: float, only_if_taller: bool = False) -> None:
    """给表格所有行设置统一最小行高（atLeast，内容更高时不被截断）。

    only_if_taller=True 时只压缩原本比目标更高的行，避免把本来更矮的
    行反向撑高。
    """
    changed = 0
    for row in table.rows:
        old = row.height.pt if row.height is not None else None
        if only_if_taller and old is not None and old <= pt:
            continue
        row.height = Pt(pt)
        row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        changed += 1
        _log(f"  行高: {old if old is not None else '默认'}pt -> {pt}pt")
    if changed == 0:
        _log(f"  行高: 无高于 {pt}pt 的行，保持不变")


def set_para_spacing(para, before=0, after=0, line=None) -> None:
    """设置段落段前/段后间距（磅）；line 给定时按固定行距（磅）设置。"""
    pf = para.paragraph_format
    old_b = pf.space_before.pt if pf.space_before is not None else None
    old_a = pf.space_after.pt if pf.space_after is not None else None
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    old_line = pf.line_spacing
    if line is not None:
        pf.line_spacing = Pt(line)
    text = para.text.strip()[:16] or "(空)"
    _log(
        f"  段落 {text!r}: 段前 {old_b}->{before}pt, 段后 {old_a}->{after}pt"
        + (f", 行距 {old_line}->{line}pt" if line is not None else "")
    )


def shrink_empty_paras(doc, pt: float = 6) -> None:
    """把空段落的段落标记字号调小，压缩其占用的行高。"""
    half_points = str(int(pt * 2))
    count = 0
    for idx, para in enumerate(doc.paragraphs):
        if para.text.strip() or para.runs:
            continue
        p_pr = para._p.get_or_add_pPr()
        r_pr = p_pr.find(qn("w:rPr"))
        if r_pr is None:
            r_pr = OxmlElement("w:rPr")
            p_pr.append(r_pr)  # rPr 是 pPr 的最后一个子元素
        for tag in ("w:sz", "w:szCs"):
            el = r_pr.find(qn(tag))
            if el is None:
                el = OxmlElement(tag)
                r_pr.append(el)
            el.set(qn("w:val"), half_points)
        count += 1
        _log(f"  空段落 P{idx}: 段落标记字号 -> {pt}pt")
    if count == 0:
        _log("  空段落: 未找到")


def shrink_table_font(table, pt: float = 10.0) -> None:
    """把表格内继承默认字号（未显式设字号）的文字降到 pt。

    显式设过字号的表头（如 14pt）保持不变。
    """
    count = 0
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    if run.font.size is None and run.text.strip():
                        run.font.size = Pt(pt)
                        count += 1
    _log(f"  表内字号: {count} 个 run -> {pt}pt（显式字号保持不变）")


def fix_volunteer() -> None:
    """志愿者补贴领取表：压列宽、压行高、缩小尾部空段落。"""
    path = LIB_DIR / "志愿者补贴领取表.docx"
    print(f"\n== 处理 {path.name} ==")
    doc = Document(str(path))
    table = doc.tables[0]

    set_col_widths(table, VOLUNTEER_RATIOS)
    # 12pt 时「姓名/联系方式」列仍会折成两行，按规则第 4 级降到 10pt
    shrink_table_font(table, pt=10.0)
    disable_snap_to_grid(table)
    set_table_line_spacing(table, pt=12.5)
    set_row_height(table, VOLUNTEER_ROW_PT, only_if_taller=True)
    shrink_empty_paras(doc, pt=6)

    doc.save(str(path))
    print(f"  已保存 {path}")


def fix_proposal() -> None:
    """计划书：压表格行高、正文段前段后归零。"""
    path = LIB_DIR / "计划书.docx"
    print(f"\n== 处理 {path.name} ==")
    doc = Document(str(path))

    # 表 1「人员」、表 3「小计」列太窄，占位符折行把行撑高；
    # 从「内容」「类别」列匀出宽度（规则第 1 级）
    set_col_widths(doc.tables[0], [1004, 1296, 3100, 2087, 1800, 943])
    set_col_widths(doc.tables[1], [3335, 6892])
    set_col_widths(doc.tables[2], [1600, 2400, 1800, 1600, 2346])

    for i, table in enumerate(doc.tables, start=1):
        print(f"  表 {i}（{len(table.rows)} 行）:")
        shrink_table_font(table, pt=10.0)
        set_row_height(table, 22.0, only_if_taller=True)

    print("  正文段落间距:")
    for para in doc.paragraphs:
        set_para_spacing(para, before=0, after=0)

    doc.save(str(path))
    print(f"  已保存 {path}")


def main() -> None:
    fix_volunteer()
    fix_proposal()
    print("\n完成。请转 PDF 验证页数。")


if __name__ == "__main__":
    main()
