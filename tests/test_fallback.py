#!/usr/bin/env python3
"""M4 待兜底清单测试。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fb = _load("fallback")
eq = _load("error_queue")
audit_mod = _load("audit")


def _seed(root, day="2026-11-06"):
    """灌两条审计记录：一条待兜底、一条正常。"""
    recs = [
        {"ts": f"{day}T10:00:00+08:00", "file": "a.pdf", "status": "done",
         "project": "huimin", "category": "项目执行"},
        {"ts": f"{day}T10:01:00+08:00", "file": "b.pdf", "status": "error",
         "error": "转换失败：文件损坏"},
        {"ts": f"{day}T10:02:00+08:00", "file": "c.jpg", "status": "pending_review",
         "project": "", "category": "其他"},
    ]
    d = root / "state" / "audit"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), "utf-8")


def test_collect_pending_only(tmp_path):
    _seed(tmp_path)
    items = fb.collect(tmp_path, "2026-11-06")
    files = {i["file"] for i in items}
    assert "a.pdf" not in files, "成功的文件不该进兜底清单"
    assert {"b.pdf", "c.jpg"} <= files


def test_collect_includes_open_errors(tmp_path):
    _seed(tmp_path)
    eq.add(tmp_path, file="d.xlsx", error="权限不足")
    items = fb.collect(tmp_path, "2026-11-06")
    assert any(i["file"] == "d.xlsx" for i in items)


def test_collect_empty(tmp_path):
    assert fb.collect(tmp_path, "2026-11-06") == []


def test_render_has_rules_and_items(tmp_path):
    _seed(tmp_path)
    text = fb.render(fb.collect(tmp_path, "2026-11-06"), "2026-11-06", str(tmp_path))
    assert "待兜底清单 · 2026-11-06" in text
    assert "不能**决定隐私" in text or "不能" in text
    assert "b.pdf" in text and "c.jpg" in text
    assert "结论" in text


def test_render_empty(tmp_path):
    text = fb.render([], "2026-11-06", str(tmp_path))
    assert "无待处理项" in text


def test_record_log(tmp_path):
    fb.record_log(tmp_path, {"file": "a.pdf", "action": "renamed", "note": "改成 xx"})
    p = tmp_path / "state" / "fallback_log.jsonl"
    assert p.is_file()
    rec = json.loads(p.read_text("utf-8").strip())
    assert rec["file"] == "a.pdf" and rec["action"] == "renamed"
    assert "ts" in rec


def test_record_log_accepts_json_string(tmp_path):
    fb.record_log(tmp_path, '{"file":"z.pdf","note":"ok"}')
    rec = json.loads((tmp_path / "state" / "fallback_log.jsonl").read_text("utf-8").strip())
    assert rec["file"] == "z.pdf"


# ---------- M10：未分类原件的落点与原因 ----------

def test_locate_finds_pending_category_dir(tmp_path):
    """未分类原件落 sources/_待确认/<类别>/（不是 sources/projects/_待确认/）。"""
    p = tmp_path / "sources" / "_待确认" / "日常工作佐证"
    p.mkdir(parents=True)
    (p / "x.jpg").write_bytes(b"x")
    assert fb._locate(tmp_path, "x.jpg") == str(p / "x.jpg")


def test_locate_prefers_inbox_copy(tmp_path):
    """archive_one 是复制不是移动，inbox 里那份还在 → 优先报 inbox（人就在那儿动手）。"""
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "x.jpg").write_bytes(b"x")
    p = tmp_path / "sources" / "_待确认" / "其他"
    p.mkdir(parents=True)
    (p / "x.jpg").write_bytes(b"x")
    assert fb._locate(tmp_path, "x.jpg") == str(tmp_path / "inbox" / "x.jpg")


def test_locate_explains_when_missing(tmp_path):
    assert "未归档" in fb._locate(tmp_path, "nope.jpg")


def test_collect_uses_note_as_problem(tmp_path):
    """M10：审计记录里的 note 要成为兜底清单的「问题」列。"""
    d = tmp_path / "state" / "audit"
    d.mkdir(parents=True)
    (d / "2026-11-06.jsonl").write_text(json.dumps({
        "ts": "2026-11-06T10:00:00+08:00", "file": "乱七八糟.jpg",
        "status": "pending_review", "project": "", "category": "日常工作佐证",
        "note": "文件名无日期/单位", "action": "archive_only", "converter": "media",
    }, ensure_ascii=False) + "\n", "utf-8")
    items = fb.collect(tmp_path, "2026-11-06")
    assert items[0]["problem"] == "文件名无日期/单位"
