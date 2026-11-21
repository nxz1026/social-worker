#!/usr/bin/env python3
"""审计写入：明细剔敏后落 audit_log 表。

审计明细里绝不能出现口令 / token / 个案正文；这里统一做最后一道过滤。
"""
from __future__ import annotations

import json

from ..state import _SENSITIVE_DETAIL_KEYS


def safe_detail(detail):
    """剔敏后的审计明细。口令/token/正文一律替换，不落库。"""
    if not isinstance(detail, dict):
        return detail
    out = {}
    for key, value in detail.items():
        if str(key).lower() in _SENSITIVE_DETAIL_KEYS:
            out[key] = "<redacted>"
        elif isinstance(value, str) and len(value) > 300:
            out[key] = value[:300] + "…"
        else:
            out[key] = value
    return out


def audit(conn, user_id, action, project=None, target=None, detail=None, ip=None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO audit_log(user_id, action, project, target, detail, ip)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (user_id, action, project, target,
             json.dumps(safe_detail(detail), ensure_ascii=False) if detail else None, ip),
        )
    conn.commit()
