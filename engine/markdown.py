#!/usr/bin/env python3
"""文档转 Markdown：docx / doc / xlsx / pdf。

只做本地转换，不调用任何外部服务：
  - docx：python-docx，按正文顺序输出段落与表格
  - xlsx：openpyxl，每个 sheet 一节，公式保留「公式 + 缓存值」
  - doc ：soffice 转成 docx 后再走 docx_to_md
  - pdf ：优先 pdftotext，其次 pypdf，最后 soffice 转 txt

所有转换失败统一抛 RuntimeError，调用方据此跳过单个文件。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# docx 正文里需要按顺序处理的节点
_W_P = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
_W_TBL = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tbl"


def _escape_cell(text: str) -> str:
    """Markdown 表格单元格：竖线转义，换行压成 <br>。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("|", "\\|")
    return "<br>".join(line.strip() for line in text.split("\n")).strip()


def _table_to_md(table) -> str:
    """把 python-docx 的 Table 转成 Markdown 表格。

    合并单元格在 row.cells 里会重复出现同一个 tc，这里按 tc 去重；
    各行长度不一致时补空列，保证表格结构完整。
    """
    rows: list[list[str]] = []
    for row in table.rows:
        cells: list[str] = []
        seen: set[int] = set()
        for cell in row.cells:
            key = id(cell._tc)
            if key in seen:          # 横向合并的重复格
                continue
            seen.add(key)
            cells.append(_escape_cell(cell.text))
        rows.append(cells)

    if not rows:
        return ""
    width = max(len(r) for r in rows)
    if width == 0:
        return ""
    rows = [r + [""] * (width - len(r)) for r in rows]

    lines = ["| " + " | ".join(rows[0]) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _paragraph_to_md(paragraph) -> str:
    """段落转 Markdown：标题加 #，列表加 -，其余原样。"""
    text = (paragraph.text or "").strip()
    if not text:
        return ""
    style = (paragraph.style.name if paragraph.style is not None else "") or ""
    lowered = style.lower()
    if lowered.startswith("heading") or style.startswith("标题"):
        digits = "".join(ch for ch in style if ch.isdigit())
        level = min(max(int(digits), 1), 6) if digits else 1
        return "#" * level + " " + text
    if lowered.startswith("list") or style.startswith("列表"):
        return "- " + text
    return text


def docx_to_md(path: Path) -> str:
    """docx → Markdown。表格转成 Markdown 表格（保留行结构），段落按顺序输出。

    用 python-docx，不调用外部服务。失败抛 RuntimeError。
    """
    path = Path(path)
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = Document(str(path))
        blocks: list[str] = []
        # 直接遍历 body 子节点，段落和表格才能保持原始顺序
        for child in document.element.body.iterchildren():
            if child.tag == _W_P:
                md = _paragraph_to_md(Paragraph(child, document))
                if md:
                    blocks.append(md)
            elif child.tag == _W_TBL:
                md = _table_to_md(Table(child, document))
                if md:
                    blocks.append(md)
        return "\n\n".join(blocks).strip()
    except Exception as exc:
        raise RuntimeError(f"docx 转换失败：{path}：{exc}") from exc


def _cell_text(value) -> str:
    """单元格值转字符串：整数浮点去掉 .0，其余按原样。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def xlsx_to_md(path: Path) -> str:
    """每个 sheet 一节，单元格转 Markdown 表格。公式单元格输出「公式 + 缓存值」。

    用 openpyxl。失败抛 RuntimeError。
    """
    path = Path(path)
    try:
        import openpyxl

        # 公式视图 + 缓存值视图，两份按坐标对齐
        wb_formula = openpyxl.load_workbook(str(path), data_only=False)
        wb_value = openpyxl.load_workbook(str(path), data_only=True)
    except Exception as exc:
        raise RuntimeError(f"xlsx 读取失败：{path}：{exc}") from exc

    sections: list[str] = []
    try:
        for sheet in wb_formula.worksheets:
            value_sheet = wb_value[sheet.title]
            # 先找有内容的边界，避免把空白样式格也画进表格
            last_row = last_col = 0
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is not None:
                        last_row = max(last_row, cell.row)
                        last_col = max(last_col, cell.column)
            if last_row == 0 or last_col == 0:
                sections.append(f"## {sheet.title}\n\n_（空表）_")
                continue

            # 整列全空的中间列（合并单元格留下的占位）不画进表格
            used_cols = [
                col for col in range(1, last_col + 1)
                if any(sheet.cell(row=r, column=col).value is not None
                       for r in range(1, last_row + 1))
            ]
            lines = [f"## {sheet.title}", "",
                     "| " + " | ".join([""] * len(used_cols)) + " |",
                     "| " + " | ".join(["---"] * len(used_cols)) + " |"]
            for row in sheet.iter_rows(min_row=1, max_row=last_row,
                                       min_col=1, max_col=last_col):
                cells: list[str] = []
                for cell in row:
                    if cell.column not in used_cols:
                        continue
                    value = cell.value
                    cached = value_sheet[cell.coordinate].value
                    if isinstance(value, str) and value.startswith("="):
                        # 公式单元格：同时给出公式和 Excel 缓存的计算结果
                        shown = "无缓存值" if cached is None else _cell_text(cached)
                        text = f"{value}（缓存值：{shown}）"
                    else:
                        text = _cell_text(value)
                    cells.append(_escape_cell(text))
                lines.append("| " + " | ".join(cells) + " |")
            sections.append("\n".join(lines))
    finally:
        wb_formula.close()
        wb_value.close()

    return "\n\n".join(sections).strip()


def _soffice() -> str:
    """定位 LibreOffice 可执行文件。"""
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise RuntimeError(
            "未找到 soffice，无法转换该格式；请安装：apt install libreoffice-writer-nogui")
    return exe


def _run_soffice(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    """调用 soffice，用独立用户配置目录避免并发锁冲突。"""
    return subprocess.run(
        [_soffice(), "--headless", f"-env:UserInstallation=file://{args[0]}", *args[1:]],
        capture_output=True, text=True, timeout=timeout,
    )


def doc_to_md(path: Path) -> str:
    """老 .doc 格式：调 soffice --headless --convert-to docx 到临时目录再走 docx_to_md。

    soffice 不可用抛 RuntimeError（提示 apt install libreoffice-writer-nogui）。
    """
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"文件不存在：{path}")
    exe = _soffice()          # 不可用会在这里抛出带安装提示的 RuntimeError
    try:
        with tempfile.TemporaryDirectory(prefix="doc2md-") as tmp:
            profile = Path(tmp) / "lo-profile"
            outdir = Path(tmp) / "out"
            outdir.mkdir(parents=True, exist_ok=True)
            proc = _run_soffice([
                str(profile), "--convert-to", "docx",
                "--outdir", str(outdir), str(path),
            ])
            candidates = sorted(outdir.glob("*.docx"))
            if proc.returncode != 0 or not candidates:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"soffice 转换 .doc 失败：{path}：{detail}")
            return docx_to_md(candidates[0])
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f".doc 转换失败：{path}：{exc}") from exc


def _pdf_with_pdftotext(path: Path) -> str | None:
    """pdftotext 可用时直接抽文本；不可用返回 None。"""
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    proc = subprocess.run([exe, "-layout", str(path), "-"],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _pdf_with_pypdf(path: Path) -> str | None:
    """pypdf 兜底（纯 Python，无需系统包）。"""
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return None


def _pdf_with_soffice(path: Path) -> str | None:
    """soffice 转 txt 兜底；失败返回 None。"""
    try:
        _soffice()
    except RuntimeError:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="pdf2md-") as tmp:
            profile = Path(tmp) / "lo-profile"
            outdir = Path(tmp) / "out"
            outdir.mkdir(parents=True, exist_ok=True)
            proc = _run_soffice([
                str(profile), "--convert-to", "txt",
                "--outdir", str(outdir), str(path),
            ])
            if proc.returncode != 0:
                return None
            texts = sorted(outdir.glob("*.txt"))
            if not texts:
                return None
            return texts[0].read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def pdf_to_md(path: Path) -> str:
    """pdf → 文本。优先 pdftotext，没有则依次退化 pypdf / soffice 转 txt。

    三条路都走不通时抛 RuntimeError。
    """
    path = Path(path)
    if not path.is_file():
        raise RuntimeError(f"文件不存在：{path}")

    for extractor in (_pdf_with_pdftotext, _pdf_with_pypdf, _pdf_with_soffice):
        try:
            text = extractor(path)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"PDF 转换超时：{path}：{exc}") from exc
        if text is not None:
            return text.strip()

    raise RuntimeError(
        f"PDF 转换失败：{path}：未找到 pdftotext / pypdf / soffice 任一可用工具")


# 后缀 → 转换器
_CONVERTERS = {
    ".docx": docx_to_md,
    ".doc": doc_to_md,
    ".xlsx": xlsx_to_md,
    ".pdf": pdf_to_md,
}


def file_to_md(path: Path) -> str:
    """按后缀分发。不支持的后缀返回空串。"""
    path = Path(path)
    converter = _CONVERTERS.get(path.suffix.lower())
    if converter is None:
        return ""
    return converter(path)
