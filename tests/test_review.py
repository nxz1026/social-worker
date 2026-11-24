"""PR-01：材料逐项核对 / 签字状态。

覆盖 engine.checks 的 reviews 读写、merge_review_status 的 deliverable 判定，
以及 /review 端点的权限、参数校验与读写闭环。
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402
from api.routes.materials import api_set_review  # noqa: E402
from engine import checks  # noqa: E402

PROJECT = "惠民社区保障资金项目"
ACT = "11-共治行动-第一期"

ADMIN = {"id": 1, "username": "admin", "role": "admin",
         "real_name": "管理员", "projects": {PROJECT: "write"}}
EDITOR = {"id": 2, "username": "editor", "role": "editor",
          "real_name": "测试员", "projects": {PROJECT: "write"}}
VIEWER = {"id": 3, "username": "viewer", "role": "viewer",
          "real_name": "旁观者", "projects": {PROJECT: "readonly"}}


class _Req:
    client = types.SimpleNamespace(host="127.0.0.1")
    headers: dict = {}


@pytest.fixture(autouse=True)
def _silent_audit(monkeypatch):
    """审计落库不在本工单范围，统一替成空操作。"""
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)
    monkeypatch.setattr("api.shared.audit.audit", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _projects_root(tmp_path, monkeypatch):
    """把项目根指到 tmp，避免读到 /srv 下的真实数据。"""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr("engine.projects.PROJECTS_ROOT", root)
    return root


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _full_act(d: Path) -> None:
    """补齐 15 项，让 check() 判 complete：deliverable 只对齐全的活动才有意义。"""
    _write(d / "01-活动计划.docx")
    _write(d / "02-活动通知.docx")
    _write(d / "03-签到表" / "签字.pdf")
    _write(d / "04-满意度调查表" / "签字.jpg")
    _write(d / "05-满意度测评汇总表.docx")
    _write(d / "06-总结报告.docx")
    _write(d / "07-简报.docx")
    _write(d / "08-决算表.docx")
    _write(d / "09-监督记录表" / "签字.pdf")
    _write(d / "09-报销单.xlsx")
    _write(d / "10-物资照片" / "a.jpg")
    _write(d / "11-活动照片" / "a.jpg")
    _write(d / "12-发票照片" / "a.pdf")
    _write(d / "13-其他佐证" / "a.txt")


@pytest.fixture(autouse=True)
def _silent_readme(monkeypatch):
    """索引刷新失败/成功都不影响本工单断言。"""
    monkeypatch.setattr("engine.readme.write_activity_readme",
                        lambda *a, **k: True)


def _act(tmp_path: Path) -> Path:
    """造一个活动目录：项目 yaml + activity.yaml + 两个必需项文件。"""
    root = tmp_path / "projects"
    proj = root / PROJECT
    d = proj / "活动" / ACT
    d.mkdir(parents=True)
    (proj / f"{PROJECT}.yaml").write_text(
        yaml.safe_dump({"project": {"short": PROJECT, "code_rules": {}}},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    (d / "activity.yaml").write_text(
        "code: XH\nstatus: imported\nimported_at: '2026-11-18T10:00:00+08:00'\n"
        "活动时间: '2026-11-01'\n执行社工: 张三\n",
        encoding="utf-8")
    (d / "01-活动计划.docx").write_text("x", encoding="utf-8")
    (d / "02-活动通知.docx").write_text("x", encoding="utf-8")
    return d


# ---------- engine：读 / 写 ----------

def test_save_then_read(tmp_path):
    d = _act(tmp_path)
    rec = checks.save_review(d, 1, reviewed_by="聂聃", note="金额已核对")
    assert rec["reviewed_by"] == "聂聃"
    assert rec["reviewed_note"] == "金额已核对"
    assert rec["signed"] is False
    assert checks.read_reviews(d)["1"] == rec


def test_save_keeps_other_yaml_fields(tmp_path):
    d = _act(tmp_path)
    checks.save_review(d, 1, reviewed_by="聂聃")
    data = yaml.safe_load((d / "activity.yaml").read_text(encoding="utf-8"))
    assert data["code"] == "XH"
    assert data["status"] == "imported"
    assert data["imported_at"] == "2026-11-18T10:00:00+08:00"
    assert data["活动时间"] == "2026-11-01"
    assert data["执行社工"] == "张三"
    assert set(data["reviews"]) == {"1"}


def test_signed_without_scan_ok(tmp_path):
    """人工点击即可确认签字，扫描件后补。"""
    d = _act(tmp_path)
    rec = checks.save_review(d, 8, reviewed_by="聂聃", signed=True)
    assert rec["signed"] is True
    assert not rec.get("scan")


def test_signed_with_missing_scan_raises(tmp_path):
    d = _act(tmp_path)
    with pytest.raises(ValueError):
        checks.save_review(d, 8, reviewed_by="聂聃", signed=True,
                           scan="不存在的.jpg")


def test_signed_with_valid_scan(tmp_path):
    d = _act(tmp_path)
    (d / "09-监督记录表").mkdir()
    (d / "09-监督记录表" / "扫描件-20261121.jpg").write_text("x", encoding="utf-8")
    rec = checks.save_review(d, 8, reviewed_by="聂聃", signed=True,
                             scan="09-监督记录表/扫描件-20261121.jpg")
    assert rec["scan"] == "09-监督记录表/扫描件-20261121.jpg"


def test_clear_removes_record(tmp_path):
    d = _act(tmp_path)
    checks.save_review(d, 1, reviewed_by="聂聃")
    checks.save_review(d, 1, reviewed_by="聂聃", clear=True)
    assert checks.read_reviews(d) == {}


def test_bad_item_no_raises(tmp_path):
    d = _act(tmp_path)
    with pytest.raises(ValueError):
        checks.save_review(d, 99, reviewed_by="聂聃")


def test_read_reviews_tolerates_broken_yaml(tmp_path):
    d = _act(tmp_path)
    (d / "activity.yaml").write_text("reviews: [1,2\n", encoding="utf-8")
    assert checks.read_reviews(d) == {}


# ---------- engine：合并 ----------

def _write_reviews(d: Path, reviews: dict) -> None:
    (d / "activity.yaml").write_text(
        yaml.safe_dump({"reviews": {str(k): v for k, v in reviews.items()}},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def _required_nos(d: Path) -> list[int]:
    return [it["no"] for it in checks.check(d)["items"] if it["status"] != "na"]


def test_merge_adds_fields_and_counts(tmp_path):
    d = _act(tmp_path)
    _write_reviews(d, {"1": {"reviewed_by": "聂聃",
                             "reviewed_at": "2026-11-21T20:00:00+08:00",
                             "reviewed_note": "已核对", "signed": False}})
    r = checks.merge_review_status(checks.check(d), checks.read_reviews(d))
    item1 = next(it for it in r["items"] if it["no"] == 1)
    assert item1["review"]["reviewed_by"] == "聂聃"
    assert item1["signed"] is False
    assert r["reviewed_count"] == 1
    assert r["signed_count"] == 0
    assert r["deliverable"] is False


def test_deliverable_all_required_reviewed(tmp_path):
    """deliverable = 必需项齐备（2026-11-24 起不再要求逐项核对）。

    旧规则要求「每一项都有核对记录」，实测 45 个真实活动没有一个满足
    （最高 10/14）——社工不会对缺文件的项逐项点核对，那条让 deliverable
    变成恒为 false 的死规则。核对进度改由 reviewed_required_count 单独展示。
    """
    d = _act(tmp_path)
    _full_act(d)
    required = _required_nos(d)
    _write_reviews(d, {no: {"reviewed_by": "聂聃", "reviewed_at": "t",
                            "reviewed_note": "", "signed": False}
                       for no in required})
    r = checks.merge_review_status(checks.check(d), checks.read_reviews(d))
    assert r["reviewed_count"] == len(required)
    assert r["deliverable"] is True

    _write_reviews(d, {no: {"reviewed_by": "聂聃", "reviewed_at": "t",
                            "reviewed_note": "", "signed": False}
                       for no in required[:-1]})
    r = checks.merge_review_status(checks.check(d), checks.read_reviews(d))
    # 齐备不变 → 仍可交付；只是核对进度少了一项
    assert r["deliverable"] is True
    assert r["reviewed_required_count"] == len(required) - 1


# ---------- API ----------

def test_api_requires_write(tmp_path):
    _act(tmp_path)
    with pytest.raises(Exception) as exc:
        api_set_review(PROJECT, ACT, {"item": 1}, _Req(), user=VIEWER, conn=None)
    assert getattr(exc.value, "status_code", None) == 403


def test_api_bad_item_400(tmp_path):
    _act(tmp_path)
    with pytest.raises(Exception) as exc:
        api_set_review(PROJECT, ACT, {"item": 99}, _Req(), user=EDITOR, conn=None)
    assert getattr(exc.value, "status_code", None) == 400


def test_api_review_roundtrip(tmp_path):
    d = _act(tmp_path)
    out = api_set_review(PROJECT, ACT, {"item": 1, "note": "金额已核对"},
                         _Req(), user=EDITOR, conn=None)
    assert out["ok"] is True
    assert out["record"]["reviewed_by"] == "测试员"

    r = checks.merge_review_status(checks.check(d), checks.read_reviews(d))
    assert r["reviewed_count"] == 1
    assert r["items"][0]["review"]["reviewed_by"] == "测试员"

    out = api_set_review(PROJECT, ACT, {"item": 1, "clear": True},
                         _Req(), user=EDITOR, conn=None)
    assert out["ok"] is True
    r = checks.merge_review_status(checks.check(d), checks.read_reviews(d))
    assert r["reviewed_count"] == 0
