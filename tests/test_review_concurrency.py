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


# ---------------- 「签字确认即视为齐备」 ----------------

def test_signed_partial_counts_as_complete():
    """第 8 项只有未签字 docx（partial），人工签字后活动应算完成。"""
    items = [{"no": i, "name": f"项{i}", "status": "ok"} for i in range(1, 15)]
    items[7]["status"] = "partial"
    reviews = {str(i): {"reviewed_by": "聂聃"} for i in range(1, 15)}
    reviews["8"]["signed"] = True

    out = _merge(items, reviews)

    assert out["complete"] is True
    assert out["partial_count"] == 0
    assert out["pending_upload_count"] == 1
    item8 = [it for it in out["items"] if it["no"] == 8][0]
    assert item8["pending_upload"] is True
    assert item8["status"] == "partial", "文件状态本身不变，只是不再卡完成度"


def test_signed_missing_counts_as_complete():
    """第 9 项文件完全缺失（missing），人工签字后也算齐备。"""
    items = [{"no": i, "name": f"项{i}", "status": "ok"} for i in range(1, 15)]
    items[8]["status"] = "missing"
    reviews = {str(i): {"reviewed_by": "聂聃"} for i in range(1, 15)}
    reviews["9"]["signed"] = True

    out = _merge(items, reviews)

    assert out["complete"] is True
    assert out["missing_count"] == 0
    assert out["pending_upload_count"] == 1


def test_unsigned_missing_still_incomplete():
    """反向：没签字的缺失项仍然卡完成度。"""
    items = [{"no": i, "name": f"项{i}", "status": "ok"} for i in range(1, 15)]
    items[8]["status"] = "missing"
    reviews = {str(i): {"reviewed_by": "聂聃"} for i in range(1, 15)}

    out = _merge(items, reviews)

    assert out["complete"] is False
    assert out["missing_count"] == 1
    assert out["pending_upload_count"] == 0


def test_signed_does_not_bypass_review_requirement():
    """签字归位不等于可交付：未核对的必需项仍让 deliverable=False。"""
    items = [{"no": i, "name": f"项{i}", "status": "ok"} for i in range(1, 15)]
    items[8]["status"] = "missing"
    reviews = {"9": {"reviewed_by": "聂聃", "signed": True}}   # 只核对了 1 项

    out = _merge(items, reviews)

    assert out["complete"] is True
    assert out["deliverable"] is False, "只有 1/14 项核对过，不能可交付"


def test_dashboard_counts_signed_as_complete(tmp_path, monkeypatch):
    """仪表盘「已完整活动」要用 merge_review_status 的口径。

    原来 dashboard 只调 checks.check()，签字确认即齐备的规则拿不到，
    「已完整活动」恒为 0（2026-11-23 实测）。
    """
    # 导入 api.routes.misc 会加载配置，先给测试 DSN
    import os as _os
    _os.environ.setdefault("SOCIAL_WORK_ROOT", "/srv/social-work")
    _os.environ.setdefault(
        "SOCIALWORK_DB_DSN",
        "host=127.0.0.1 port=5432 dbname=socialwork user=admin password=x")
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
    from api.routes import misc as _misc

    proj = tmp_path / "测试项目"
    acts = proj / "活动"
    # 一个：材料全齐但第 8 项只有未签字 docx，且已签字 → 算完整
    a1 = acts / "01-已签字"
    a1.mkdir(parents=True)
    # 其余必需项都齐（用清单里的前缀造占位文件），只让第 8 项缺扫描件
    for item in checks.CHECKLIST:
        if item["no"] == 8 or item.get("kind", "").startswith("dir"):
            continue
        pre = item.get("prefix") or (item.get("prefixes") or [""])[0]
        if not pre:
            continue
        ext = ".xlsx" if item.get("kind") == "xlsx" else ".docx"
        (a1 / f"{pre}{item['name']}{ext}").write_bytes(b"x")
    (a1 / "09-监督记录表.docx").write_bytes(b"x")
    # 其他签字类项（3 签到表 / 14 满意度测评表）给扫描件，保证它们判 ok
    (a1 / "03-签到表-签字.jpg").write_bytes(b"x")
    (a1 / "04-满意度测评表-签字.jpg").write_bytes(b"x")
    # 目录类（10/11/12/13）各建一个非空目录
    for item in checks.CHECKLIST:
        pre = item.get("prefix") or ""
        if item.get("kind", "").startswith("dir") and pre:
            d = a1 / f"{pre}{item['name']}"
            d.mkdir()
            (d / "x.jpg").write_bytes(b"x")
    checks.save_review(a1, 8, reviewed_by="测试", signed=True)
    # 一个：什么都缺、没签字 → 不完整
    a2 = acts / "02-空活动"
    a2.mkdir(parents=True)

    monkeypatch.setattr(_misc._projects, "PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(_misc._projects, "list_projects",
                        lambda: [{"name": "测试项目", "short": "测试"}])
    user = {"id": 1, "username": "admin", "role": "admin",
            "projects": {"测试项目": "write"}}

    out = _misc.api_dashboard(user)

    stat = [p for p in out["projects"] if p["name"] == "测试项目"][0]
    assert stat["activity_total"] == 2
    assert stat["activity_complete"] == 1, "签字确认过的活动应计入已完整"
