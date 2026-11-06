#!/usr/bin/env python3
"""可重试调用的统一封装：指数退避。

为什么需要它：飞书 API 超时、Cognee 502、LLM 端点抖动都是**暂时性**故障，
一次失败就报错会制造大量假故障（用户 m00406 要求「失效立刻发」，
但更不该把抖动当成真故障发出去）。

判定规则（`is_retryable`）：异常类型名 + 消息里出现这些关键字就算可重试：
timed out / timeout / 502 / 503 / 504 / connection / reset / refused / temporarily / rate limit。
**格式不支持、文件损坏、权限不足**这类不在列 —— 立即抛，不浪费重试。

用法
    from retry import retry
    items = retry(lambda: pull_inbox(token), label="拉取未分类", log=log)
"""

import time

# 可重试关键字（小写匹配）
RETRYABLE = (
    "timed out", "timeout", "502", "503", "504",
    "connection", "reset", "refused", "temporarily",
    "rate limit", "too many requests", "econnreset",
)


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否属于「重试可能有用」的暂时性故障。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(k in text for k in RETRYABLE)


def retry(fn, attempts=3, base_delay=2.0, label="", log=None, sleep=None):
    """跑 `fn()`，可重试的异常按指数退避重试。

    - `attempts`：总尝试次数（默认 3 = 1 次 + 2 次重试）
    - `base_delay`：首次重试等待秒数，之后翻倍（2s / 4s）
    - `log`：可选的日志回调（收单行字符串）
    - `sleep`：可注入，测试用（默认 time.sleep）

    不可重试的异常、或最后一次尝试的异常，都会**立刻抛出**。
    """
    sleep = sleep or time.sleep
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 统一判定后决定抛还是重试
            last = exc
            if not is_retryable(exc) or i == attempts - 1:
                raise
            delay = base_delay * (2 ** i)
            if log:
                log(f"  ! {label} 失败（{type(exc).__name__}），"
                    f"{delay:.0f}s 后重试（{i + 1}/{attempts}）")
            sleep(delay)
    raise last  # 理论到不了这里，兜底
