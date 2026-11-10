#!/usr/bin/env python3
"""上传接口（POST /api/docs）的护栏。

钉住这几件事：
  1. 没有全项目权限时必须显式指定 project（否则自动判项目会写进越权目录）；
  2. 空文件、超限文件在落盘阶段就被挡住（413/400），不会进归档；
  3. archive.py 的回执必须被解析并校验，`ok=false` 不能当成功；
  4. 归档实际落到的项目若越权，返回 403 而不是静默成功；
  5. 上传链路**不在请求里重建索引**（--no-cognify），只处理自己这一个文件；
  6. 超时返回 202 + 可查询的 token（不再是模糊的 504）；
  7. 同一内容重复上传返回同一个 token，不产生第二份档案；
  8. 未知类别在入口就 400。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402


class FakeUpload:
    """够用的 UploadFile 替身：端点已改成同步（走线程池），从 `.file` 读。"""

    def __init__(self, data: bytes, name: str = "a.txt"):
        self.filename = name
        self.file = io.BytesIO(data)


class FakeRequest:
    class _Client:
        host = "127.0.0.1"

    client = _Client()


def _receipt(**kw) -> str:
    payload = {"ok": True, "token": "t1", "project": "huadu", "md": "x.md"}
    payload.update(kw)
    return "归档中...\n__RESULT__ " + json.dumps(payload, ensure_ascii=False) + "\n"


def _prep(monkeypatch, *, size_limit_mb=1, projects=("huadu", "huimin")):
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)
    monkeypatch.setattr(app.SETTINGS, "upload", {"max_mb": size_limit_mb}, raising=False)
    monkeypatch.setattr(app.SETTINGS, "tasks", {"archive_timeout_seconds": 5}, raising=False)
    monkeypatch.setattr(app, "_find_by_sha256", lambda sha: None)
    return {"id": 1, "role": "editor", "projects": list(projects)}


def _fake_archive(monkeypatch, *, rc=0, out="", err="", timed_out=False, called=None):
    def _run(cmd, timeout):
        if called is not None:
            called.append(cmd)
        return rc, out, err, timed_out
    monkeypatch.setattr(app, "run_archive", _run)


def test_upload_requires_explicit_project_without_all_projects(monkeypatch):
    user = _prep(monkeypatch, projects=("huadu",))
    with pytest.raises(app.HTTPException) as ei:
        app.upload_doc(FakeRequest(), FakeUpload(b"x"), "", "", user, None)
    assert ei.value.status_code == 400
    assert "请指定项目" in ei.value.detail


def test_upload_rejects_empty_file(monkeypatch):
    user = _prep(monkeypatch)
    with pytest.raises(app.HTTPException) as ei:
        app.upload_doc(FakeRequest(), FakeUpload(b""), "huadu", "", user, None)
    assert ei.value.status_code == 400
    assert "空文件" in ei.value.detail


def test_upload_rejects_unknown_category(monkeypatch):
    user = _prep(monkeypatch)
    monkeypatch.setattr(app, "_flow", lambda: {"categories": ["其他", "日常工作佐证"]})
    with pytest.raises(app.HTTPException) as ei:
        app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "乱写的类别", user, None)
    assert ei.value.status_code == 400
    assert "未知类别" in ei.value.detail


def test_upload_rejects_oversize_before_archiving(monkeypatch):
    user = _prep(monkeypatch, size_limit_mb=1)
    called = []
    _fake_archive(monkeypatch, out=_receipt(), called=called)
    big = b"x" * (2 * 1024 * 1024)
    with pytest.raises(app.HTTPException) as ei:
        app.upload_doc(FakeRequest(), FakeUpload(big), "huadu", "", user, None)
    assert ei.value.status_code == 413
    assert called == []          # 超限时根本没调 archive.py


def test_upload_skips_index_and_only_handles_own_file(monkeypatch):
    """上传不再顺带重建索引（--no-cognify），也不顺手处理别人刚放进来的文件。"""
    user = _prep(monkeypatch)
    called = []
    _fake_archive(monkeypatch, out=_receipt(), called=called)
    app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "", user, None)
    cmd = called[0]
    assert "--no-cognify" in cmd
    assert "--only-self" in cmd


def test_upload_passes_category_through(monkeypatch):
    user = _prep(monkeypatch)
    called = []
    _fake_archive(monkeypatch, out=_receipt(category="日常工作佐证"), called=called)
    out = app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "日常工作佐证", user, None)
    assert "--category" in called[0]
    assert called[0][called[0].index("--category") + 1] == "日常工作佐证"
    assert out["category"] == "日常工作佐证"


def test_upload_surfaces_archive_failure(monkeypatch):
    user = _prep(monkeypatch)
    _fake_archive(monkeypatch, rc=0, out=_receipt(ok=False))
    with pytest.raises(app.HTTPException) as ei:
        app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "", user, None)
    assert ei.value.status_code == 500
    assert "未通过校验" in ei.value.detail


def test_upload_timeout_returns_202_with_token(monkeypatch):
    """超时不再回模糊的 504：202 + 由内容 hash 算出的 token，客户端可查状态。"""
    user = _prep(monkeypatch)
    _fake_archive(monkeypatch, rc=None, timed_out=True)
    resp = app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "", user, None)
    assert resp.status_code == 202
    body = json.loads(resp.body)
    assert body["ok"] is False and body["accepted"] is True
    assert body["token"] == "local-" + hashlib.sha256(b"x").hexdigest()[:16]


def test_upload_scan_timeout_exit_code_also_202(monkeypatch):
    """archive.py 自己等 scan-inbox 超时会返回 6，语义同外层超时。"""
    user = _prep(monkeypatch)
    _fake_archive(monkeypatch, rc=6)
    resp = app.upload_doc(FakeRequest(), FakeUpload(b"y"), "huadu", "", user, None)
    assert resp.status_code == 202
    assert json.loads(resp.body)["accepted"] is True


def test_upload_duplicate_returns_same_token_without_archiving(monkeypatch):
    user = _prep(monkeypatch)
    called = []
    _fake_archive(monkeypatch, out=_receipt(), called=called)
    monkeypatch.setattr(app, "_find_by_sha256",
                        lambda sha: ("local-abc", {"name": "旧.jpg", "project": "huadu",
                                                   "category": "其他", "status": "done",
                                                   "md": "m.md"}))
    out = app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "", user, None)
    assert out["duplicate"] is True and out["token"] == "local-abc"
    assert called == []          # 幂等命中时不再跑归档


def test_upload_rejects_archive_into_unauthorized_project(monkeypatch):
    user = _prep(monkeypatch, projects=("huadu",))
    _fake_archive(monkeypatch, out=_receipt(project="huimin"))
    with pytest.raises(app.HTTPException) as ei:
        app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "", user, None)
    assert ei.value.status_code == 403


def test_upload_returns_receipt(monkeypatch):
    user = _prep(monkeypatch)
    _fake_archive(monkeypatch, out=_receipt(token="abc"))
    out = app.upload_doc(FakeRequest(), FakeUpload(b"hello"), "huadu", "", user, None)
    assert out["ok"] is True
    assert out["token"] == "abc"
    assert out["project"] == "huadu"
