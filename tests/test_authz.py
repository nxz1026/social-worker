"""授权边界、审计脱敏、布尔/角色解析。"""
from __future__ import annotations

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


def _user(projects=None, role="editor"):
    return {"id": 1, "username": "u", "role": role, "projects": projects or []}


def test_scopes_of_zero_projects_is_empty():
    assert app.scopes_of(_user([])) == []
    assert app.scopes_of(_user(["huadu"])) == ["huadu"]


def test_scopes_of_rejects_foreign_project():
    with pytest.raises(app.HTTPException) as exc:
        app.scopes_of(_user(["huimin"]), "huadu")
    assert exc.value.status_code == 403


def test_list_docs_zero_projects_returns_nothing(monkeypatch):
    monkeypatch.setattr(app, "_load_manifest", lambda: {
        "files": {"t1": {"name": "居民名单.docx", "project": "huadu",
                         "status": "done"}}})
    out = app.list_docs("", "", "", _user([]))
    assert out == {"total": 0, "items": []}


def test_list_docs_filters_foreign_project(monkeypatch):
    monkeypatch.setattr(app, "_load_manifest", lambda: {
        "files": {
            "t1": {"name": "花都.docx", "project": "huadu", "status": "done"},
            "t2": {"name": "惠民.docx", "project": "huimin", "status": "done"},
        }})
    out = app.list_docs("", "", "", _user(["huimin"]))
    assert [x["token"] for x in out["items"]] == ["t2"]


def test_search_zero_projects_403(monkeypatch):
    called = []
    monkeypatch.setattr(app, "search_datasets", lambda *a, **k: called.append(a) or (True, "", False))
    with pytest.raises(app.HTTPException) as exc:
        app.search("x", "", _user([]))
    assert exc.value.status_code == 403
    assert called == []


def test_generate_zero_projects_403(tmp_path, monkeypatch):
    (tmp_path / "case-record.md").write_text("模板", encoding="utf-8")
    monkeypatch.setattr(app, "TEMPLATES_DIR", tmp_path)
    with pytest.raises(app.HTTPException) as exc:
        app.generate({"type": "个案记录", "context": "x"}, None, _user([]), None)
    assert exc.value.status_code == 403


def test_docs_health_zero_projects_empty():
    assert app.docs_health("", _user([])) == {"total": 0, "items": {}}


def test_safe_detail_redacts_secrets_and_case_text():
    out = app._safe_detail({
        "password": "tjfjyjr", "token": "abc", "context": "个案正文",
        "role": "admin", "projects": ["huadu"],
    })
    assert out["password"] == "<redacted>"
    assert out["token"] == "<redacted>"
    assert out["context"] == "<redacted>"
    assert out["role"] == "admin"
    assert out["projects"] == ["huadu"]


def test_role_and_bool_parsing():
    assert app._valid_role("editor") == "editor"
    with pytest.raises(app.HTTPException):
        app._valid_role("root")
    assert app._as_bool("false") is False
    assert app._as_bool("true") is True
    assert app._as_bool(0) is False
    with pytest.raises(app.HTTPException):
        app._as_bool("maybe")


def test_parse_archive_receipt_reads_last_marker():
    stdout = "human line\n__RESULT__ {\"ok\": true, \"token\": \"t1\"}\n"
    assert app._parse_archive_receipt(stdout)["token"] == "t1"
    assert app._parse_archive_receipt("no marker") == {}
