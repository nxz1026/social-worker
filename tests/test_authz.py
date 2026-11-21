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
from api.routes import docs as docs_routes  # noqa: E402
from api.admin import valid_role
from api.deps import scopes_of
from api.shared.audit import safe_detail  # noqa: E402
from api.shared.parse import as_bool  # noqa: E402
from api.routes.misc import generate, search
from api.routes import misc as _misc  # noqa: E402


def _user(projects=None, role="editor"):
    # projects 现在是 {项目: 'readonly'|'write'}，不再是列表
    return {"id": 1, "username": "u", "role": role, "projects": projects or {}}


def test_scopes_of_zero_projects_is_empty():
    assert scopes_of(_user({})) == []
    assert scopes_of(_user({"huadu": "write"})) == ["huadu"]


def test_scopes_of_rejects_foreign_project():
    with pytest.raises(app.HTTPException) as exc:
        scopes_of(_user({"huimin": "write"}), "huadu")
    assert exc.value.status_code == 403


def test_list_docs_zero_projects_returns_nothing(monkeypatch):
    monkeypatch.setattr(docs_routes, "_load_manifest", lambda: {
        "files": {"t1": {"name": "居民名单.docx", "project": "huadu",
                         "status": "done"}}})
    out = docs_routes.list_docs("", "", "", _user({}))
    assert out == {"total": 0, "items": []}


def test_list_docs_filters_foreign_project(monkeypatch):
    monkeypatch.setattr(docs_routes, "_load_manifest", lambda: {
        "files": {
            "t1": {"name": "花都.docx", "project": "花都社区保障资金项目",
                   "status": "done"},
            "t2": {"name": "惠民.docx", "project": "惠民社区保障资金项目",
                   "status": "done"},
        }})
    out = docs_routes.list_docs("", "", "", _user({"惠民社区保障资金项目": "write"}))
    assert [x["token"] for x in out["items"]] == ["t2"]


def test_search_zero_projects_403(monkeypatch):
    called = []
    monkeypatch.setattr(_misc, "search_datasets", lambda *a, **k: called.append(a) or (True, "", False))
    with pytest.raises(app.HTTPException) as exc:
        search("x", "", _user({}))
    assert exc.value.status_code == 403
    assert called == []


def test_generate_zero_projects_403(tmp_path, monkeypatch):
    """零项目用户必须先拿到 403，不能因为模板缺失等原因先撞 500（审计 N-01）。"""
    (tmp_path / "case-record.md").write_text("模板", encoding="utf-8")
    # generate 已搬到 api/routes/misc，patch 要打在它实际读取的模块上
    monkeypatch.setattr(_misc, "TEMPLATES_DIR", tmp_path)
    with pytest.raises(app.HTTPException) as exc:
        generate({"type": "个案记录", "context": "x"}, None, _user({}), None)
    assert exc.value.status_code == 403


def test_generate_zero_projects_403_without_template(monkeypatch):
    """模板目录不存在时，零项目用户仍应稳定 403 而不是 500。

    这对应干净环境（模板未入库）下的失败路径。
    """
    monkeypatch.setattr(_misc, "TEMPLATES_DIR", Path("/nonexistent/templates"))
    with pytest.raises(app.HTTPException) as exc:
        generate({"type": "个案记录", "context": "x"}, None, _user({}), None)
    assert exc.value.status_code == 403


def test_docs_health_zero_projects_empty():
    assert docs_routes.docs_health("", _user({})) == {"total": 0, "items": {}}


def test_safe_detail_redacts_secrets_and_case_text():
    out = safe_detail({
        "password": "tjfjyjr", "token": "abc", "context": "个案正文",
        "role": "admin", "projects": ["huadu"],
    })
    assert out["password"] == "<redacted>"
    assert out["token"] == "<redacted>"
    assert out["context"] == "<redacted>"
    assert out["role"] == "admin"
    assert out["projects"] == ["huadu"]


def test_role_and_bool_parsing():
    assert valid_role("editor") == "editor"
    with pytest.raises(app.HTTPException):
        valid_role("root")
    assert as_bool("false") is False
    assert as_bool("true") is True
    assert as_bool(0) is False
    with pytest.raises(app.HTTPException):
        as_bool("maybe")


def test_parse_archive_receipt_reads_last_marker():
    stdout = "human line\n__RESULT__ {\"ok\": true, \"token\": \"t1\"}\n"
    assert docs_routes._parse_archive_receipt(stdout)["token"] == "t1"
    assert docs_routes._parse_archive_receipt("no marker") == {}
