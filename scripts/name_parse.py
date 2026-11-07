#!/usr/bin/env python3
"""M10 文件名解析层：只从**文件名**取日期与单位，不碰正文。

手写资料的 OCR 不可能 100% 成功，所以这类资料不依赖识别正文，只取两样：
    日期 —— 建时间关系
    单位 —— 建项目关系（复用 config/project_aliases.yaml 的项目关键词）
取到任一样就能归档并建 Cognee 关系；两样都取不到 → 调用方把文件落
`_待确认/`（未分类），由每天 22:00 的审计汇总提醒人工处理。

用法：
    import name_parse
    info = name_parse.parse("花都-2026年11月5日-织围巾登记.jpg",
                            aliases={"huadu": ["花都"], "huimin": ["惠民"]},
                            labels={"huadu": "花都", "huimin": "惠民"})
    # {"date": "2026-11-05", "unit": "huadu", "unit_label": "花都",
    #  "matched": "花都", "content": "织围巾登记"}
    name_parse.usable(info)        # 日期或单位取到任一样 → True
    name_parse.describe(info)      # 一行中文摘要，写日志用
"""
from __future__ import annotations

import re
from pathlib import Path

# 2026-11-05 / 2026.11.5 / 2026_11_05 / 2026年11月5日
_DATE_SEP = re.compile(r"(20\d{2})\s*[-./_年]\s*(\d{1,2})\s*[-./_月]\s*(\d{1,2})\s*日?")
# 20261105103000（微信/手机导出的时间戳，取前 8 位）
_DATE_TS = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})\d{6}(?!\d)")
# 20261105（前后不能再是数字，避免把长数字串当日期）
_DATE_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")

# 文件名噪声：去掉后剩下的才算「内容关键词」
_NOISE = re.compile(
    r"(微信图片|mmexport|wx_camera_?|IMG_?\d*|VID_?\d*|图片|照片|截图|screenshot|"
    r"image|新建|副本|扫描件?|_?\d{6,})",
    re.IGNORECASE)
_SEP = re.compile(r"[\s_\-—–.、,，。()（）\[\]【】]+")


def parse_date(name: str) -> str:
    """文件名里的日期，返回 YYYY-MM-DD；取不到返回空串。"""
    text = Path(str(name)).stem
    for rx in (_DATE_SEP, _DATE_TS, _DATE_COMPACT):
        for m in rx.finditer(text):
            y, mo, d = (int(x) for x in m.groups())
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def parse_unit(name: str, aliases: dict | None = None,
               labels: dict | None = None) -> tuple[str, str, str]:
    """文件名里的单位/项目，返回 (key, label, 命中词)；取不到返回 ("", "", "")。

    多个关键词命中时取**最长**的（「世纪光华」压过「光华」）。
    """
    text = Path(str(name)).stem
    labels = labels or {}
    best = None                       # (关键词长度, key, 命中词)
    for key, kws in (aliases or {}).items():
        for kw in kws or []:
            kw = str(kw)
            if kw and kw in text:
                cand = (len(kw), key, kw)
                if best is None or cand[0] > best[0]:
                    best = cand
    if best is None:
        return "", "", ""
    return best[1], labels.get(best[1], best[1]), best[2]


def parse_content(name: str, date: str = "", matched: str = "") -> str:
    """文件名里去掉日期、单位词和噪声后剩下的内容关键词。"""
    text = Path(str(name)).stem
    text = _DATE_SEP.sub(" ", text)
    text = _DATE_TS.sub(" ", text)
    text = _DATE_COMPACT.sub(" ", text)
    if matched:
        text = text.replace(matched, " ")
    text = _NOISE.sub(" ", text)
    text = _SEP.sub("-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if len(text) < 2 or text.isdigit():
        return ""
    return text


def parse(name: str, aliases: dict | None = None, labels: dict | None = None) -> dict:
    """一次解析出日期与单位。返回 dict（字段见模块 docstring）。"""
    name = str(name)
    date = parse_date(name)
    unit, unit_label, matched = parse_unit(name, aliases, labels)
    return {
        "date": date,
        "unit": unit,
        "unit_label": unit_label,
        "matched": matched,
        "content": parse_content(name, date, matched),
    }


def usable(info: dict | None) -> bool:
    """文件名是否至少给出日期或单位 —— 两样都没有就是「识别不了」。"""
    info = info or {}
    return bool(info.get("date") or info.get("unit"))


def describe(info: dict | None) -> str:
    """一行中文摘要，写日志用。"""
    info = info or {}
    date = info.get("date") or "无"
    unit = info.get("unit_label") or "无"
    content = info.get("content") or ""
    text = f"日期 {date} / 单位 {unit}"
    return f"{text} / 内容 {content}" if content else text
