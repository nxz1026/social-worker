#!/usr/bin/env python3
"""R-02 / R-03 回归：核对记录的并发安全与 deliverable 判据。"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yaml

from engine import checks


def _act(tmp_path: Path) -> Path:
    act = tmp_path / "活动"
    act.mkdir()
    (act / "activity.yaml").write_text(
        yaml.safe_dump({"code": "T-1", "status": "imported"},
                       allow_unicode=True), encoding="utf-8")
    return act


def test_concurrent_reviews_keep_all_records(tmp_path):
    """R-02：8 线程写不同序号，必须全部保留且不抛错。

    修复前实测：116 次 FileNotFoundError，8 个序号只剩 3 个。
    """
    act = _act(tmp_path)
    errors: list[str] = []

    def worker(no: int) -> None:
        try:
            for _ in range(20):
                checks.save_review(act, no, reviewed_by="测试")
        except Exception as exc:            # noqa: BLE001 - 收集任何异常供断言
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入抛错 {len(errors)} 次：{errors[:3]}"
    reviews = checks.read_reviews(act)
    assert sorted(reviews, key=int) == [str(i) for i in range(1, 9)]

    data = yaml.safe_load((act / "activity.yaml").read_text(encoding="utf-8"))
    assert data["code"] == "T-1", "其他字段被覆盖"
    assert data["status"] == "imported"


def test_review_and_not_applicable_do_not_clobber(tmp_path):
    """R-02：reviews 与 not_applicable 并发写同一文件，两个字段都要在。"""
    act = _act(tmp_path)
    checks.save_review(act, 1, reviewed_by="甲")
    # 模拟另一个写者改 not_applicable（直接走同一锁与原子写）
    from engine import ylock
    ylock.update_yaml(act / "activity.yaml",
                      lambda d: {**d, "not_applicable": [15]}, lock_dir=act)
    data = yaml.safe_load((act / "activity.yaml").read_text(encoding="utf-8"))
    assert data["reviews"]["1"]["reviewed_by"] == "甲"
    assert data["not_applicable"] == [15]


def _merge(items: list[dict], reviews: dict) -> dict:
    return checks.merge_review_status({"items": items}, reviews)


def test_deliverable_requires_every_required_item_reviewed():
    """R-03：必需项未核对时，用 na 项的核对记录凑数不能算可交付。"""
    items = [{"no": i, "name": f"项{i}", "status": "ok"} for i in range(1, 15)]
    items.append({"no": 15, "name": "其他", "status": "na"})
    reviews = {str(i): {"reviewed_by": "x"} for i in range(1, 14)}   # 缺 14
    reviews["15"] = {"reviewed_by": "x"}                            # na 项也核对
    out = _merge(items, reviews)
    assert out["required_count"] == 14
    assert out["reviewed_required_count"] == 13
    assert out["deliverable"] is False, "必需项 14 未核对，不能可交付"


def test_deliverable_true_when_all_required_reviewed():
    """反向：必需项全部核对且齐全 → 可交付（na 项核对与否不影响）。"""
    items = [{"no": i, "name": f"项{i}", "status": "ok"} for i in range(1, 15)]
    items.append({"no": 15, "name": "其他", "status": "na"})
    reviews = {str(i): {"reviewed_by": "x"} for i in range(1, 15)}
    out = _merge(items, reviews)
    assert out["deliverable"] is True
    assert out["reviewed_required_count"] == 14
