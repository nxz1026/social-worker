#!/usr/bin/env python3
"""上传接口（POST /api/docs）的护栏。

钉住四件事：
  1. 没有全项目权限时必须显式指定 project（否则自动判项目会写进越权目录）；
  2. 空文件、超限文件在落盘阶段就被挡住（413/400），不会进归档；
  3. archive.py 的回执必须被解析并校验，`ok=false` 不能当成功；
  4. 归档实际落到的项目若越权，返回 403 而不是静默成功。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
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
    def __init__(self, data: bytes, name: str = "a.txt"):
        self.filename = name
        self._buf = data
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._buf):
            return b""
        end = len(self._buf) if n < 0 else self._pos + n
        chunk = self._buf[self._pos:end]
        self._pos = end
        return chunk


class FakeRequest:
    class _Client:
        host = "127.0.0.1"

    client = _Client()


def _run(coro):
    return asyncio.run(coro)


def _receipt(**kw) -> str:
    import json
    payload = {"ok": True, "token": "t1", "project": "huadu", "md": "x.md"}
    payload.update(kw)
    return "归档中...\n__RESULT__ " + json.dumps(payload, ensure_ascii=False) + "\n"


def _prep(monkeypatch, *, size_limit_mb=1, projects=("huadu", "huimin")):
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)
    monkeypatch.setattr(app.SETTINGS, "upload", {"max_mb": size_limit_mb}, raising=False)
    monkeypatch.setattr(app.SETTINGS, "tasks", {"archive_timeout_seconds": 5}, raising=False)
    return {"id": 1, "role": "editor", "projects": list(projects)}


def test_upload_requires_explicit_project_without_all_projects(monkeypatch):
    user = _prep(monkeypatch, projects=("huadu",))
    with pytest.raises(app.HTTPException) as ei:
        _run(app.upload_doc(FakeRequest(), FakeUpload(b"x"), "", "", user, None))
    assert ei.value.status_code == 400
    assert "请指定项目" in ei.value.detail


def test_upload_rejects_empty_file(monkeypatch):
    user = _prep(monkeypatch)
    with pytest.raises(app.HTTPException) as ei:
        _run(app.upload_doc(FakeRequest(), FakeUpload(b""), "huadu", "", user, None))
    assert ei.value.status_code == 400
    assert "空文件" in ei.value.detail


def test_upload_rejects_oversize_before_archiving(monkeypatch):
    user = _prep(monkeypatch, size_limit_mb=1)
    called = []
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: called.append(a) or None)
    big = b"x" * (2 * 1024 * 1024)
    with pytest.raises(app.HTTPException) as ei:
        _run(app.upload_doc(FakeRequest(), FakeUpload(big), "huadu", "", user, None))
    assert ei.value.status_code == 413
    assert called == []          # 超限时根本没调 archive.py


def test_upload_surfaces_archive_failure(monkeypatch):
    user = _prep(monkeypatch)
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=_receipt(ok=False), stderr=""))
    with pytest.raises(app.HTTPException) as ei:
        _run(app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "", user, None))
    assert ei.value.status_code == 500
    assert "未通过校验" in ei.value.detail


def test_upload_timeout_returns_504(monkeypatch):
    user = _prep(monkeypatch)

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=5)

    monkeypatch.setattr(app.subprocess, "run", _boom)
    with pytest.raises(app.HTTPException) as ei:
        _run(app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "", user, None))
    assert ei.value.status_code == 504


def test_upload_rejects_archive_into_unauthorized_project(monkeypatch):
    user = _prep(monkeypatch, projects=("huadu",))
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=_receipt(project="huimin"), stderr=""))
    with pytest.raises(app.HTTPException) as ei:
        _run(app.upload_doc(FakeRequest(), FakeUpload(b"x"), "huadu", "", user, None))
    assert ei.value.status_code == 403


def test_upload_returns_receipt(monkeypatch):
    user = _prep(monkeypatch)
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout=_receipt(token="abc"), stderr=""))
    out = _run(app.upload_doc(FakeRequest(), FakeUpload(b"hello"), "huadu", "", user, None))
    assert out["ok"] is True
    assert out["token"] == "abc"
    assert out["project"] == "huadu"
