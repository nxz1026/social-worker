#!/usr/bin/env python3
"""独立审计 I-01 / I-03 / I-05 的回归测试。

这三个问题都不是单请求测试能覆盖的：并发一致性、认证资源放大、嵌套脱敏。
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

from engine import photos  # noqa: E402


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), color).save(buf, "JPEG")
    return buf.getvalue()


def test_concurrent_uploads_get_unique_sequence(tmp_path):
    """I-01：同活动并发上传，序号必须唯一且磁盘与 meta 一致。"""
    act = tmp_path / "活动" / "01-测试"
    act.mkdir(parents=True)
    results: list[tuple] = []

    def worker(idx: int) -> None:
        try:
            r = photos.save_photos(act, [(f"IMG_{idx}.jpg", _jpeg((idx, idx, idx)))],
                                   "2026-11-10", scene=f"场景{idx}")
            results.append(("ok", r["saved"]))
        except Exception as exc:               # noqa: BLE001 - 记录任何异常供断言
            results.append(("err", type(exc).__name__, str(exc)[:80]))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r[0] == "ok" for r in results), f"出现异常：{results}"
    files = sorted(p.name for p in (act / photos.PHOTO_SUBDIR).glob("*.jpg"))
    meta = photos.load_meta(act)
    seqs = [f.split("_")[1].split(".")[0] for f in files]
    assert len(set(seqs)) == len(seqs), f"序号重复：{seqs}"
    assert len(files) == len(meta["photos"]), "磁盘文件数与 meta 记录数不一致"


def test_activity_lock_serializes(tmp_path):
    """锁本身：两个持锁者的临界区不能重叠。"""
    act = tmp_path / "活动"
    act.mkdir()
    order: list[str] = []

    def hold(tag: str) -> None:
        with photos.activity_lock(act):
            order.append(f"{tag}-in")
            time.sleep(0.15)
            order.append(f"{tag}-out")

    threads = [threading.Thread(target=hold, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 必须成对出现，不能交错成 a-in b-in a-out b-out
    assert order[0].endswith("-in") and order[1] == order[0].replace("-in", "-out")


def test_last_used_is_throttled(monkeypatch):
    """I-03：同一 Key 的 last_used_at 在窗口内只写一次。"""
    from api import apikey

    created: list[int] = []

    class FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass

    class FakeConn:
        def __init__(self): created.append(1)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return FakeCur()
        def commit(self): pass

    monkeypatch.setattr(apikey.psycopg, "connect", lambda dsn: FakeConn())
    apikey._touch_last.clear()
    for _ in range(20):
        apikey._touch_last_used(999)
    time.sleep(0.4)
    assert len(created) == 1, f"节流失效：20 次认证建了 {len(created)} 个连接"


def test_safe_detail_redacts_nested_and_cycles():
    """I-05：嵌套/列表里的秘密也要替换，循环引用不炸。"""
    from api.shared.audit import safe_detail

    out = safe_detail({
        "password": "top",
        "nested": {"password": "n", "token": "t"},
        "items": [{"secret": "s"}],
        "long": {"content": "x" * 400},
    })
    assert out["password"] == "<redacted>"
    assert out["nested"]["password"] == "<redacted>"
    assert out["nested"]["token"] == "<redacted>"
    assert out["items"][0]["secret"] == "<redacted>"
    assert len(out["long"]["content"]) <= 301

    cyc: dict = {"x": 1}
    cyc["self"] = cyc
    assert safe_detail(cyc)["self"] == "<cycle>"
