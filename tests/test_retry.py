#!/usr/bin/env python3
"""M4 重试封装测试。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


retry_mod = _load("retry")


# ---------- is_retryable ----------

def test_is_retryable_true():
    for exc in [TimeoutError("timed out"), OSError("connection reset by peer"),
                RuntimeError("HTTP 502 Bad Gateway"), OSError("429 too many requests")]:
        assert retry_mod.is_retryable(exc), exc


def test_is_retryable_false():
    for exc in [ValueError("bad format"), PermissionError("denied"),
                RuntimeError("文件损坏"), KeyError("x")]:
        assert not retry_mod.is_retryable(exc), exc


# ---------- retry ----------

def test_retry_first_try_ok():
    calls = []
    out = retry_mod.retry(lambda: calls.append(1) or "ok", sleep=lambda s: None)
    assert out == "ok" and len(calls) == 1


def test_retry_recovers():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("timed out")
        return "ok"

    out = retry_mod.retry(flaky, sleep=lambda s: None)
    assert out == "ok" and len(calls) == 3


def test_retry_gives_up():
    calls = []

    def always():
        calls.append(1)
        raise TimeoutError("timed out")

    try:
        retry_mod.retry(always, attempts=3, sleep=lambda s: None)
    except TimeoutError:
        pass
    else:
        raise AssertionError("应该抛异常")
    assert len(calls) == 3


def test_retry_non_retryable_raises_immediately():
    calls = []

    def bad():
        calls.append(1)
        raise ValueError("bad format")

    try:
        retry_mod.retry(bad, attempts=5, sleep=lambda s: None)
    except ValueError:
        pass
    else:
        raise AssertionError("应该抛异常")
    assert len(calls) == 1, "不可重试的异常不该重试"


def test_retry_backoff_delays():
    delays = []
    calls = []

    def always():
        calls.append(1)
        raise TimeoutError("timed out")

    try:
        retry_mod.retry(always, attempts=3, base_delay=1.5, sleep=delays.append)
    except TimeoutError:
        pass
    assert delays == [1.5, 3.0], delays


def test_retry_log_messages():
    logs = []
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise TimeoutError("timed out")
        return "ok"

    retry_mod.retry(flaky, base_delay=0.5, label="拉取未分类", log=logs.append, sleep=lambda s: None)
    assert any("拉取未分类" in m and "重试" in m for m in logs), logs
