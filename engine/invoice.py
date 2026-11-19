#!/usr/bin/env python3
"""电子发票解析与三表金额校验。

三表指：08-决算表.docx（决算）、21-/09-报销单.xlsx（报销）、12-发票/ 下的票据（发票）。

解析顺序是「文件名优先，正文交叉校验」：归档时电子发票已按
`日期_票种_金额元_商户_事项_发票号.pdf` 改名，文件名比 PDF 正文规整得多；
正文只在文件名缺字段时兜底，并在两者冲突时把差异写进 parse_error。

本模块只处理决算表、报销单与发票三类材料；不进项目资料的内部表格
登记在 config/excluded_templates.txt，这里不引用。
"""
from __future__ import annotations

import re
from pathlib import Path

# 发票所在的活动子目录
INVOICE_SUBDIR = "12-发票"
# 决算表 / 报销单的编号前缀（报销单旧编号 09-，新编号 21-）
DECISION_PREFIX = "08"
REIMBURSE_PREFIXES = ("21", "09")

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# 文件名里的四段：日期 / 票种 / 金额 / 发票号
_DATE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AMOUNT_NAME_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*元$")
_INVOICE_NO_RE = re.compile(r"^\d{20}$")

# 正文里的金额：优先「壹仟壹佰玖拾圆整 ¥1190.00」，其次任意 ¥ 金额取最大
_UPPER_AMOUNT_RE = re.compile(r"[¥￥]\s*([\d,]+\.\d{2})")
_CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_SELLER_CODE_RE = re.compile(r"^[0-9A-Z]{18}$")
_ITEM_RE = re.compile(r"\*[^*\n]+\*([^\s\n]+)")

# 金额比对容差（分）
_EPS = 0.01


# --------------------------------------------------------------------------
# 文件名解析
# --------------------------------------------------------------------------

def _from_name(stem: str) -> dict:
    """从规范文件名提字段；缺关键字段时返回 {} 交正文兜底。

    规范形如 2026-11-13_电子发票_160元_听澜集益_茶饮服务_26512000005052287086：
    日期 / 票种 / 金额 / 商户 / 事项（可省）/ 发票号。
    """
    parts = [part.strip() for part in str(stem or "").split("_") if part.strip()]
    if len(parts) < 4:
        return {}

    date = next((p for p in parts if _DATE_NAME_RE.match(p)), "")
    invoice_no = next((p for p in reversed(parts) if _INVOICE_NO_RE.match(p)), "")
    amount_match = next((_AMOUNT_NAME_RE.match(p) for p in parts
                         if _AMOUNT_NAME_RE.match(p)), None)
    if not (date and invoice_no and amount_match):
        return {}

    amount_token = amount_match.group(0)
    # 票种（电子发票 / 增值税发票）不是商户，先剔掉
    kind = next((p for p in parts if "发票" in p), "")
    middle = [p for p in parts
              if p not in (date, invoice_no, amount_token, kind)]
    if not middle:
        return {}

    return {
        "amount": float(amount_match.group(1)),
        "merchant": middle[0],
        "date": date,
        "item": "、".join(middle[1:]),
        "invoice_no": invoice_no,
    }


# --------------------------------------------------------------------------
# 正文解析
# --------------------------------------------------------------------------

def _pdf_text(path: Path) -> str:
    """pypdf 取 PDF 正文；未安装或读不出来返回空串（不抛错）。"""
    if path.suffix.lower() not in PDF_EXTS:
        return ""
    try:
        from pypdf import PdfReader            # 已装则用，未装就只靠文件名
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages[:2]]
    except Exception:                          # 损坏 / 加密 / 空文件都不该中断扫描
        return ""
    return "\n".join(pages).strip()


def _text_amount(text: str) -> float | None:
    """价税合计（小写）：取正文里最大的 ¥ 金额。

    发票各行金额都小于价税合计，取最大最稳；一条都抓不到返回 None。
    """
    values: list[float] = []
    for raw in _UPPER_AMOUNT_RE.findall(text or ""):
        try:
            values.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return max(values) if values else None


def _text_merchant(text: str) -> str:
    """销售方名称：购买方纳税人识别号（18 位）下一行的非空文本。"""
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        if not _SELLER_CODE_RE.match(line):
            continue
        for following in lines[index + 1:]:
            if following:
                return following
    return ""


def _text_date(text: str) -> str:
    """开票日期 → YYYY-MM-DD；抓不到返回空串。"""
    match = _CN_DATE_RE.search(text or "")
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _text_item(text: str) -> str:
    """开票项目名（*类别*名称 里的名称），去重后顿号连接。"""
    items: list[str] = []
    for raw in _ITEM_RE.findall(text or ""):
        name = raw.strip()
        if name and name not in items:
            items.append(name)
    return "、".join(items)


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def parse_invoice_pdf(path: Path) -> dict:
    """从电子发票 PDF 提字段。

    返回 {amount, merchant, date, item, invoice_no, raw, source, parse_error}：
    - 文件名规整时直接用文件名（source="filename"），正文只做交叉校验
    - 文件名不全时退回正文（source="text"）
    - 都读不出来时 amount 为 None 并带 parse_error

    图片票据没有正文，只会走文件名解析。
    """
    path = Path(path)
    raw = _pdf_text(path)
    result: dict = {
        "amount": None, "merchant": "", "date": "", "item": "",
        "invoice_no": "", "raw": raw, "source": "", "parse_error": "",
    }

    named = _from_name(path.stem)
    if named:
        result.update(named)
        result["source"] = "filename"
        # 正文交叉校验：金额或票号对不上只记问题，仍以文件名为准
        problems: list[str] = []
        text_amount = _text_amount(raw)
        if text_amount is not None and abs(text_amount - named["amount"]) > _EPS:
            problems.append(f"文件名金额 {named['amount']:.2f} 与正文价税合计 "
                            f"{text_amount:.2f} 不一致")
        text_no = next(iter(re.findall(r"\d{20}", raw)), "")
        if text_no and text_no != named["invoice_no"]:
            problems.append(f"文件名票号 {named['invoice_no']} 与正文 {text_no} 不一致")
        if problems:
            result["parse_error"] = "；".join(problems)
        return result

    # 文件名不规范：退回正文
    amount = _text_amount(raw)
    if amount is None:
        result["parse_error"] = ("文件名不符合「日期_票种_金额元_商户_发票号」规范，"
                                 "正文也未取到价税合计")
        return result

    result.update({
        "amount": amount,
        "merchant": _text_merchant(raw),
        "date": _text_date(raw),
        "item": _text_item(raw),
        "invoice_no": next(iter(re.findall(r"\d{20}", raw)), ""),
        "source": "text",
    })
    return result


def scan_invoices(act_dir: Path) -> list[dict]:
    """扫 <act_dir>/12-发票/ 下所有 pdf 与图片，逐个解析。

    返回的每一项在 parse_invoice_pdf 的结果上多一个 "file"（相对发票目录的文件名）。
    目录不存在返回空列表。
    """
    folder = Path(act_dir) / INVOICE_SUBDIR
    if not folder.is_dir():
        return []

    out: list[dict] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in (PDF_EXTS | IMAGE_EXTS):
            continue
        item = parse_invoice_pdf(path)
        item["file"] = path.relative_to(folder).as_posix()
        out.append(item)
    return out


# --------------------------------------------------------------------------
# 三表校验
# --------------------------------------------------------------------------

def _docx_text(path: Path) -> str:
    """docx 正文 + 表格单元格文本；读失败返回空串。"""
    try:
        from docx import Document
        doc = Document(str(path))
    except Exception:
        return ""

    parts = [para.text for para in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _decision_total(act_dir: Path) -> tuple[float | None, str]:
    """决算合计：08-*.docx 里「合计」后面的金额。

    决算表的合计行写作「合计 | 合计 | … | 1662（元）」，正则跳过中间的
    非数字字符直接取数；同一文档出现多个「合计」时取最后一个（表尾合计行）。
    """
    matches = sorted(Path(act_dir).glob(f"{DECISION_PREFIX}-*.docx"))
    if not matches:
        return None, f"{DECISION_PREFIX}-决算表.docx"

    text = _docx_text(matches[0])
    found = re.findall(r"合计[^0-9]*([\d.]+)", text)
    for raw in reversed(found):
        try:
            return float(raw), matches[0].name
        except ValueError:
            continue
    return None, matches[0].name


def _reimburse_total(act_dir: Path) -> tuple[float | None, str]:
    """报销合计：报销单里含「合计金额」的行右侧数值。

    只读 openpyxl 的缓存值（data_only=True）——公式单元格没算过就是 None，
    不去猜公式结果，避免把错数当成合计。
    """
    act_dir = Path(act_dir)
    path: Path | None = None
    for prefix in REIMBURSE_PREFIXES:
        matches = sorted(act_dir.glob(f"{prefix}-*.xlsx"))
        if matches:
            path = matches[0]
            break
    if path is None:
        return None, "21-报销单.xlsx"

    try:
        import openpyxl
        workbook = openpyxl.load_workbook(str(path), data_only=True)
    except Exception:
        return None, path.name

    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                label_at = next(
                    (index for index, cell in enumerate(row)
                     if isinstance(cell.value, str) and "合计金额" in cell.value),
                    None)
                if label_at is None:
                    continue
                for cell in row[label_at + 1:]:
                    if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                        return float(cell.value), path.name
        return None, path.name
    finally:
        workbook.close()


def cross_check(act_dir: Path) -> dict:
    """三表金额校验，返回决算 / 报销 / 发票三个口径与差异。

    consistent 要求三表都能读出金额且两两相等；任一口径缺失都判不一致，
    并把原因写进 issues（脚本据此提示补资料）。
    """
    act_dir = Path(act_dir)
    decision, decision_src = _decision_total(act_dir)
    reimburse, reimburse_src = _reimburse_total(act_dir)

    invoices = scan_invoices(act_dir)
    parsed = [item for item in invoices if item.get("amount") is not None]
    invoice_total = round(sum(item["amount"] for item in parsed), 2) if parsed else None

    issues: list[str] = []
    if decision is None:
        issues.append(f"决算合计未取到：{decision_src}")
    if reimburse is None:
        issues.append(f"报销合计未取到：{reimburse_src}（公式需先保存缓存值）")
    if not invoices:
        issues.append(f"发票目录为空：{INVOICE_SUBDIR}/ 下没有 PDF 或图片")
    unparsed = [item["file"] for item in invoices if item.get("amount") is None]
    if unparsed:
        issues.append(f"{len(unparsed)} 张发票未能识别金额：{'、'.join(unparsed)}")

    def _diff(left: float | None, right: float | None) -> float | None:
        if left is None or right is None:
            return None
        return round(left - right, 2)

    diff = {"决算-报销": _diff(decision, reimburse),
            "报销-发票": _diff(reimburse, invoice_total)}

    for label, value in diff.items():
        if value is not None and abs(value) > _EPS:
            issues.append(f"{label} 差 {value:+.2f} 元")

    consistent = all(value is not None for value in (decision, reimburse, invoice_total)) \
        and all(abs(value) <= _EPS for value in diff.values() if value is not None)

    return {
        "决算": {"value": decision, "source": decision_src},
        "报销": {"value": reimburse, "source": reimburse_src},
        "发票": {"value": invoice_total, "count": len(invoices), "items": invoices},
        "consistent": consistent,
        "diff": diff,
        "issues": issues,
    }
