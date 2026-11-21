#!/usr/bin/env python3
"""入参解析：严格布尔值等通用小工具。

和 paths.py 分开是因为它们与路径无关，后续可被更多端点复用。
"""
from __future__ import annotations

from fastapi import HTTPException


def as_bool(value) -> bool:
    """严格布尔解析：字符串 "false" 不能当 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise HTTPException(400, "enabled 必须是布尔值")
