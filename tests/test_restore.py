"""回收站恢复：sources 回 pending、vault 对账失败要报错。"""
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


def _req():
    return types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))


def _write_manifest(root, files):
    p = root / "state" / "manifest.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"files": files}, allow_unicode=True), encoding="utf-8")


def test_sources_restore_marks_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)
    _write_manifest(tmp_path, {"t1": {
        "name": "a.docx", "project": "", "category": "其他",
        "original": "sources/_待确认/a.docx", "status": "deleted",
        "deleted_at": "2026-01-01T00:00:00+08:00", "delete_mode": "trash",
    }})
    trash = tmp_path / "sources" / ".trash" / "2026-01-01" / "_待确认" / "a.docx"
    trash.parent.mkdir(parents=True)
    trash.write_bytes(b"x")

    out = app.trash_restore(
        {"path": "sources/.trash/2026-01-01/_待确认/a.docx"}, _req(), {"id": 1}, None)

    assert out["restored_to"] == "sources/_待确认/a.docx"
    assert out["status"] == "pending"
    assert (tmp_path / "sources/_待确认/a.docx").is_file()
    entry = yaml.safe_load(
        (tmp_path / "state/manifest.yaml").read_text(encoding="utf-8"))["files"]["t1"]
    assert entry["status"] == "pending"
    assert "deleted_at" not in entry and "delete_mode" not in entry


def test_vault_restore_reports_reconcile_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)
    monkeypatch.setattr(app, "_run_reconcile", lambda ds, timeout: types.SimpleNamespace(
        returncode=1, stdout="cognee down", stderr=""))
    trash = tmp_path / "vault-huadu" / ".trash" / "2026-01-01" / "md" / "a.md"
    trash.parent.mkdir(parents=True)
    trash.write_text("x", encoding="utf-8")

    with pytest.raises(app.HTTPException) as exc:
        app.trash_restore(
            {"path": "vault-huadu/.trash/2026-01-01/md/a.md"}, _req(), {"id": 1}, None)
    assert exc.value.status_code == 500
    assert "对账失败" in exc.value.detail
    assert (tmp_path / "vault-huadu/md/a.md").is_file()
