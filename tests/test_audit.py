#!/usr/bin/env python3
"""M3 审计摘要 + 飞书通知测试。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (filename or f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load("audit", "audit.py")
notify = _load("notify", "notify.py")


# ---------- audit.load_records ----------

def test_load_records_empty(tmp_path):
    assert audit.load_records(tmp_path, "2026-11-06") == []


def test_load_records_skips_blank_and_bad_lines(tmp_path):
    d = tmp_path / "state" / "audit"
    d.mkdir(parents=True)
    (d / "2026-11-06.jsonl").write_text(
        '{"ts":"2026-11-06T10:00:00+08:00","file":"a.jpg","status":"done"}\n'
        "\n"
        "not json at all\n",
        encoding="utf-8")
    recs = audit.load_records(tmp_path, "2026-11-06")
    assert len(recs) == 1 and recs[0]["file"] == "a.jpg"


# ---------- audit.summarize ----------

def _rec(**kw):
    base = {"ts": "2026-11-06T10:00:00+08:00", "file": "x", "status": "done",
            "converter": "pandoc", "project": "huimin", "category": "项目执行",
            "action": "normal"}
    base.update(kw)
    return base


def test_summarize_splits_docs_and_images():
    s = audit.summarize([
        _rec(file="a.docx", converter="pandoc", action="normal"),
        _rec(file="b.xlsx", converter="docling", action="normal"),
        _rec(file="c.jpg", action="archive_only", converter="media", project="", category=""),
        _rec(file="d.jpg", action="ocr", converter="media", project="", category=""),
    ])
    assert s["docs"] == 2 and s["imgs"] == 2
    assert s["converter"] == {"pandoc": 1, "docling": 1}
    assert s["actions"] == {"只存档": 1, "OCR": 1}


def test_summarize_counts_failed_and_pending_review():
    s = audit.summarize([
        _rec(file="ok.docx"),
        _rec(file="bad.pdf", status="unsupported"),
        _rec(file="boom.docx", status="error", error="转换失败：x"),
        _rec(file="unk.jpg", status="pending_review", project="", category="其他",
             action="archive_only", converter="media"),
    ])
    assert len(s["failed"]) == 2
    assert len(s["pending_review"]) == 1
    assert s["failed"][0]["file"] == "bad.pdf"


def test_summarize_pending_without_error_is_not_failed():
    # status=pending 但没 error 字段 → 可能是 move 前的中间态，不算失败
    s = audit.summarize([_rec(file="wip.docx", status="pending")])
    assert s["failed"] == []


# ---------- audit.render ----------

def test_render_shape():
    text = audit.render(audit.summarize([
        _rec(file="a.docx"),
        _rec(file="c.jpg", action="archive_only", converter="media", project="", category=""),
    ], {"huimin": "惠民"}), "2026-11-06")
    assert "=== 2026-11-06 审计 ===" in text
    assert "收到 2 个文件" in text
    assert "文档 1：成功 1（pandoc 1）" in text
    assert "图片 1：只存档 1" in text
    assert "项目分布：惠民 1 / 待确认 1" in text
    assert "无失败、无待兜底" in text


def test_render_lists_failures():
    text = audit.render(audit.summarize([
        _rec(file="bad.pdf", status="unsupported"),
    ]), "2026-11-06")
    assert "失败 1" in text and "bad.pdf" in text


def test_render_uses_note_as_reason():
    """M10：未分类图片的审计记录带 note，22:00 汇总要原样说出原因（别只说「置信度低」）。"""
    text = audit.render(audit.summarize([
        _rec(file="乱七八糟.jpg", status="pending_review", project="",
             category="日常工作佐证", action="archive_only", converter="media",
             note="文件名无日期/单位"),
    ]), "2026-11-06")
    assert "待兜底 1" in text
    assert "乱七八糟.jpg — 文件名无日期/单位" in text


# ---------- notify ----------

def test_load_targets_from_config(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "notify.yaml").write_text(
        "to:\n  - ou_aaa\n  - not-an-id\n  - ou_bbb\n", encoding="utf-8")
    assert notify.load_targets(tmp_path) == ["ou_aaa", "ou_bbb"]


def test_load_targets_falls_back_to_dsh_config(tmp_path, monkeypatch):
    # 没有 config/notify.yaml → 回落到 DSH 机器人配置
    home = tmp_path / "home"
    d = home / ".dsh" / "integrations" / "dsh-feishu"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(
        {"bots": [{"ownerOpenIds": ["ou_owner"]}]}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert notify.load_targets(tmp_path) == ["ou_owner"]


def test_load_targets_empty_when_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nope"))
    assert notify.load_targets(tmp_path) == []


def test_send_returns_false_without_targets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nope"))
    assert notify.send("hi", tmp_path) is False
    assert "没有收件人" in capsys.readouterr().err


def test_send_swallows_subprocess_failure(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "notify.yaml").write_text("to:\n  - ou_aaa\n", encoding="utf-8")

    class R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(notify.subprocess, "run", lambda *a, **k: R())
    assert notify.send("hi", tmp_path) is False
