#!/usr/bin/env python3
"""文件名解析：只从文件名取日期 / 项目 / 内容关键词，不碰正文。

活动驱动流程仍要求上传文件按「日期-项目名称-描述」命名，本模块是校验实现。
"""
from __future__ import annotations

import re
from pathlib import Path

# 2026-11-05 / 2026.11.5 / 2026_11_05 / 2026年11月5日
_DATE_SEP = re.compile(r"(20\d{2})\s*[-./_年]\s*(\d{1,2})\s*[-./_月]\s*(\d{1,2})\s*日?")
# 20261105103000（微信/手机导出的时间戳，取前 8 位）
_DATE_TS = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})\d{6}(?!\d)")
# 20261105（前后不能再是数字）
_DATE_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")

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
    """文件名里的项目，返回 (key, label, 命中词)；取不到返回 ("", "", "")。

    多个关键词命中时取最长的（「世纪光华」压过「光华」）。
    """
    text = Path(str(name)).stem
    labels = labels or {}
    best = None
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


def parse_content(name: str, date: str = "", matched: str = "",
                  words: tuple[str, ...] | list[str] = ()) -> str:
    """去掉日期、项目标签和噪声后剩下的内容关键词。"""
    text = Path(str(name)).stem
    text = _DATE_SEP.sub(" ", text)
    text = _DATE_TS.sub(" ", text)
    text = _DATE_COMPACT.sub(" ", text)
    for word in sorted({w for w in ([matched] if matched else []) + list(words or ()) if w},
                       key=len, reverse=True):
        text = text.replace(word, " ")
    text = _NOISE.sub(" ", text)
    text = _SEP.sub("-", text).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if len(text) < 2 or text.isdigit():
        return ""
    return text


def parse(name: str, aliases: dict | None = None, labels: dict | None = None) -> dict:
    """一次解析出日期、项目与内容关键词。"""
    name = str(name)
    date = parse_date(name)
    unit, unit_label, matched = parse_unit(name, aliases, labels)
    return {
        "date": date,
        "unit": unit,
        "unit_label": unit_label,
        "matched": matched,
        "content": parse_content(name, date, unit_label),
    }


def usable(info: dict | None) -> bool:
    """文件名是否至少给出日期或项目。"""
    info = info or {}
    return bool(info.get("date") or info.get("unit"))


def describe(info: dict | None) -> str:
    """一行中文摘要。"""
    info = info or {}
    date = info.get("date") or "无"
    unit = info.get("unit_label") or "无"
    content = info.get("content") or ""
    text = f"日期 {date} / 单位 {unit}"
    return f"{text} / 内容 {content}" if content else text
