#!/usr/bin/env python3
"""审计写入：明细剔敏后落 audit_log 表。

审计明细里绝不能出现口令 / token / 个案正文；这里统一做最后一道过滤。
"""
from __future__ import annotations

import json

from ..state import _SENSITIVE_DETAIL_KEYS


# 递归预算：防畸形/超深结构把审计拖慢或撑大
_MAX_DEPTH = 6
_MAX_NODES = 200
_STR_LIMIT = 300


def safe_detail(detail, *, _depth: int = 0, _budget: list[int] | None = None,
                _seen: set[int] | None = None):
    """剔敏后的审计明细。口令/token/正文一律替换，不落库。

    递归处理 dict / list：原实现只看顶层，嵌套里的 password/token
    会原样落库（审计 I-05）。这里同时限制深度、节点数、字符串长度，
    并对循环引用打保守占位符。
    """
    if _budget is None:
        _budget = [_MAX_NODES]
    if _seen is None:
        _seen = set()

    if _budget[0] <= 0:
        return "<truncated>"
    _budget[0] -= 1

    if isinstance(detail, str):
        return detail if len(detail) <= _STR_LIMIT else detail[:_STR_LIMIT] + "…"

    if isinstance(detail, dict):
        if _depth >= _MAX_DEPTH:
            return "<depth-limit>"
        oid = id(detail)
        if oid in _seen:
            return "<cycle>"
        _seen.add(oid)
        try:
            out = {}
            for key, value in detail.items():
                if str(key).lower() in _SENSITIVE_DETAIL_KEYS:
                    out[key] = "<redacted>"
                else:
                    out[key] = safe_detail(value, _depth=_depth + 1,
                                           _budget=_budget, _seen=_seen)
            return out
        finally:
            _seen.discard(oid)

    if isinstance(detail, (list, tuple)):
        if _depth >= _MAX_DEPTH:
            return "<depth-limit>"
        oid = id(detail)
        if oid in _seen:
            return "<cycle>"
        _seen.add(oid)
        try:
            return [safe_detail(v, _depth=_depth + 1, _budget=_budget, _seen=_seen)
                    for v in detail]
        finally:
            _seen.discard(oid)

    return detail


def audit(conn, user_id, action, project=None, target=None, detail=None, ip=None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO audit_log(user_id, action, project, target, detail, ip)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (user_id, action, project, target,
             json.dumps(safe_detail(detail), ensure_ascii=False) if detail else None, ip),
        )
    conn.commit()
