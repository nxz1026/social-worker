#!/usr/bin/env python3
"""交付完整性判断：本次 task 的产物是否真的落进了模板。

依赖 engine.llm 的 task 字段清单，延迟 import 以免 app.py 的
sys.path 初始化顺序影响模块加载。
"""
from __future__ import annotations


def sw_delivery(missing: list[str], task: str) -> dict:
    """判断本次 task 的产物是否真的落进了模板。

    missing 里混着两类占位符：
      1. 本 task 负责的（如 brief 的 简报导语/诉求描述/达到目标）—— 没填上就是失败
      2. 别的环节负责的（图注_N 归 caption、合计金额归人工）—— 没填上只是提示

    task 的字段清单就是白名单：只有落在里面的才算本次的责任。
    返回 {"complete": bool, "expected": [...], "unfilled": [...], "other_missing": [...]}
    """
    from engine import llm as _llm
    spec = (_llm.TASKS or {}).get(task) or {}
    expected = [str(x) for x in (spec.get("fields") or ())]
    if not expected:
        # 未知 task（理论上到不了这里）：不做判断，避免误报
        return {"complete": True, "expected": [], "unfilled": [],
                "other_missing": list(missing or [])}

    missing_set = {str(m).strip("{} ") for m in (missing or [])}
    unfilled = [f for f in expected if f in missing_set]
    others = [m for m in (missing or []) if str(m).strip("{} ") not in set(expected)]
    return {
        "complete": not unfilled,
        "expected": expected,
        "unfilled": unfilled,
        "other_missing": others,
    }
