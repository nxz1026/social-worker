"""W1：Web 管理界面后端 API。

全部直接调用端点函数（不依赖 httpx / TestClient），外部调用
（LLM、飞书、Cognee）一律 monkeypatch；项目根与模板库用 tmp_path。
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

import pytest
import yaml
from docx import Document
from fastapi import BackgroundTasks
from fastapi.responses import FileResponse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402
from api.routes.activities import api_activity_detail  # noqa: E402
from api.routes.materials import api_activity_fill, api_activity_llm, api_activity_llm_fill
from api.routes.assets import (api_activity_delete_file, api_activity_download,  # noqa: E402
                               api_activity_files)
from api.routes import misc as _misc  # noqa: E402
from api.routes.misc import (  # noqa: E402
    api_cognee_datasets, api_cognee_index, api_dashboard, api_sync_feishu, api_templates)

ADMIN = {"id": 1, "username": "admin", "role": "admin",
         "projects": {"huimin": "write", "huadu": "write", "jili": "write"}}
EDITOR = {"id": 2, "username": "editor", "role": "editor", "projects": {"huimin": "write"}}
VIEWER = {"id": 3, "username": "viewer", "role": "viewer", "projects": {"huimin": "readonly"}}


class _Req:
    """端点只用到 request.client.host。"""

    client = types.SimpleNamespace(host="127.0.0.1")
    headers: dict = {}


@pytest.fixture(autouse=True)
def _silent_audit(monkeypatch):
    """审计落库不在本工单范围，统一替成空操作。"""
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)
    monkeypatch.setattr("api.shared.audit.audit", lambda *a, **k: None)


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """三个项目：惠民 / 花都 / 激励，各带一份最小 yaml 与 活动/ 目录。"""
    root = tmp_path / "projects"
    for name, short in (("惠民项目", "huimin"), ("花都项目", "huadu"),
                        ("激励项目", "jili")):
        d = root / name
        (d / "活动").mkdir(parents=True)
        (d / f"{name}.yaml").write_text(
            yaml.safe_dump({"project": {"short": short, "code_rules": {}}},
                           allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    monkeypatch.setattr(app._projects, "PROJECTS_ROOT", root)
    return root


def _activity(projects_root: Path, project: str, dirname: str,
              code: str = "CDYJR2026008-C-16110001", complete: bool = False) -> Path:
    """造一个活动目录：activity.yaml + 一个顶层文件 + 一个照片子目录。"""
    d = projects_root / project / "活动" / dirname
    d.mkdir(parents=True)
    meta = {"code": code, "status": "imported"}
    if complete:
        # 15 项全部标不适用 → check() 判 complete
        meta["not_applicable"] = list(range(1, 16))
    (d / "activity.yaml").write_text(
        yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (d / "01-活动计划.docx").write_bytes(b"plan")
    photos = d / "11-活动照片"
    photos.mkdir()
    (photos / "a.jpg").write_bytes(b"img")
    return d


def _template_root(tmp_path: Path) -> Path:
    """造 <root>/templates/library。"""
    lib = tmp_path / "templates" / "library"
    lib.mkdir(parents=True)
    return lib


def _docx_template(path: Path, placeholders: list[str]) -> None:
    doc = Document()
    doc.add_paragraph("项目：{{项目名称}}")
    doc.add_paragraph("编号：{{项目编号}}")
    for key in placeholders:
        doc.add_paragraph("{{" + key + "}}")
    doc.save(str(path))


# ---------------- A. 项目管理 ----------------

def test_create_project_makes_dirs_and_yaml(projects_root):
    out = app.api_create_project(
        {"name": "新项目", "short": "newp",
         "code_rules": {"establish_month": 8}}, _Req(), ADMIN, None)

    target = projects_root / "新项目"
    assert out["ok"] is True
    for sub in ("方案", "协议", "报告", "活动", "其他"):
        assert (target / sub).is_dir()
    data = yaml.safe_load((target / "新项目.yaml").read_text(encoding="utf-8"))
    assert data["project"]["short"] == "newp"
    assert data["project"]["code_rules"]["establish_month"] == 8


def test_create_project_conflict_409(projects_root):
    app.api_create_project({"name": "新项目", "short": "newp"}, _Req(), ADMIN, None)
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project({"name": "新项目"}, _Req(), ADMIN, None)
    assert exc.value.status_code == 409


def test_create_project_rejects_traversal(projects_root):
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project({"name": "../逃逸"}, _Req(), ADMIN, None)
    assert exc.value.status_code == 400


def test_update_project_overwrites_yaml(projects_root):
    out = app.api_update_project(
        "惠民项目",
        {"project": {"short": "hm2", "code_rules": {"establish_month": 6}},
         "indicators": [{"name": "走访调研", "count": 2}]},
        _Req(), ADMIN, None)

    data = yaml.safe_load(Path(out["yaml"]).read_text(encoding="utf-8"))
    assert data["project"]["short"] == "hm2"
    assert data["indicators"][0]["count"] == 2


# ---------------- B. 活动管理 ----------------

def test_activity_detail(projects_root):
    _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    out = api_activity_detail("惠民项目", "01-走访调研-第一期", ADMIN)

    assert out["dirname"] == "01-走访调研-第一期"
    assert out["code"] == "CDYJR2026008-C-16110001"
    assert out["status"] == "imported"
    assert [f["name"] for f in out["files"]] == ["01-活动计划.docx", "activity.yaml"]
    assert out["dirs"] == [{"name": "11-活动照片", "count": 1}]
    assert len(out["check"]["items"]) == 15


def test_activity_files_sub(projects_root):
    _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    out = api_activity_files("惠民项目", "01-走访调研-第一期", "11-活动照片", ADMIN)
    assert out == [{"name": "a.jpg", "size": 3,
                    "mtime": out[0]["mtime"], "is_dir": False}]


def test_activity_files_traversal_400(projects_root):
    _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    with pytest.raises(app.HTTPException) as exc:
        api_activity_files("惠民项目", "01-走访调研-第一期", "../..", ADMIN)
    assert exc.value.status_code == 400


def test_download_traversal_400(projects_root):
    _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    with pytest.raises(app.HTTPException) as exc:
        api_activity_download("惠民项目", "01-走访调研-第一期",
                              "../../../../etc/passwd", ADMIN)
    assert exc.value.status_code == 400


def test_download_returns_file(projects_root):
    _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    resp = api_activity_download("惠民项目", "01-走访调研-第一期",
                                 "11-活动照片/a.jpg", ADMIN)
    assert isinstance(resp, FileResponse)
    assert Path(resp.path).name == "a.jpg"


def test_delete_file_moves_to_trash(projects_root):
    act = _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    out = api_activity_delete_file(
        "惠民项目", "01-走访调研-第一期", {"path": "01-活动计划.docx"},
        _Req(), ADMIN, None)

    assert out["ok"] is True
    assert not (act / "01-活动计划.docx").exists()
    assert (act / ".trash" / out["trash"].split("/")[1] / "01-活动计划.docx").is_file()


def test_viewer_write_403(projects_root):
    _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    with pytest.raises(app.HTTPException) as exc:
        api_activity_delete_file(
            "惠民项目", "01-走访调研-第一期", {"path": "01-活动计划.docx"},
            _Req(), VIEWER, None)
    assert exc.value.status_code == 403


def test_viewer_admin_endpoint_403(projects_root):
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project({"name": "x"}, _Req(), VIEWER, None)
    assert exc.value.status_code == 403


def test_editor_foreign_project_403(projects_root):
    _activity(projects_root, "花都项目", "01-走访调研-第一期")
    with pytest.raises(app.HTTPException) as exc:
        api_activity_detail("花都项目", "01-走访调研-第一期", EDITOR)
    assert exc.value.status_code == 403


# ---------------- C. 材料生成 ----------------

def test_templates_returns_15(tmp_path, monkeypatch):
    lib = _template_root(tmp_path)
    for seq in range(1, 16):
        suffix = ".xlsx" if seq % 3 == 0 else ".docx"
        (lib / f"模板{seq:02d}{suffix}").write_bytes(b"")
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr("api.state.ROOT", tmp_path)
    monkeypatch.setattr(app._fill, "scan_placeholders",
                        lambda p: ["{{项目名称}}", "{{活动名称}}"])

    out = api_templates(ADMIN)
    assert len(out) == 15
    assert out[0]["name"] == "模板01"
    assert out[0]["kind"] == "docx"
    assert out[0]["placeholders"] == ["{{项目名称}}", "{{活动名称}}"]
    assert any(item["kind"] == "xlsx" for item in out)


def test_fill_writes_output(tmp_path, projects_root, monkeypatch):
    lib = _template_root(tmp_path)
    _docx_template(lib / "决算表.docx", [])
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr("api.state.ROOT", tmp_path)
    act = _activity(projects_root, "惠民项目", "01-走访调研-第一期")

    out = api_activity_fill(
        "惠民项目", "01-走访调研-第一期",
        {"template": "决算表", "data": {"合计金额": "1000"},
         "output": "08-决算表.docx"},
        _Req(), ADMIN, None)

    dst = act / out["path"]
    assert dst.is_file()
    assert out["filled"] >= 2
    assert "{{项目编号}}" not in "\n".join(p.text for p in Document(str(dst)).paragraphs)
    assert "1000" in "\n".join(p.text for p in Document(str(dst)).paragraphs)


def test_fill_without_code_400(tmp_path, projects_root, monkeypatch):
    lib = _template_root(tmp_path)
    _docx_template(lib / "决算表.docx", [])
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr("api.state.ROOT", tmp_path)
    act = _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    (act / "activity.yaml").write_text("status: imported\n", encoding="utf-8")

    with pytest.raises(app.HTTPException) as exc:
        api_activity_fill("惠民项目", "01-走访调研-第一期",
                              {"template": "决算表"}, _Req(), ADMIN, None)
    assert exc.value.status_code == 400


def test_llm_returns_result_without_writing(projects_root, monkeypatch):
    act = _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    before = {p.name for p in act.iterdir()}
    seen = {}

    def fake_write(task, facts, style=""):
        seen.update({"task": task, "facts": facts, "style": style})
        return {"导语": "测试导语", "目的": "测试目的"}

    monkeypatch.setattr(app._llm, "write", fake_write)
    out = api_activity_llm("惠民项目", "01-走访调研-第一期",
                               {"task": "brief", "style": "务实"}, _Req(), ADMIN, None)

    assert out["result"]["导语"] == "测试导语"
    assert seen["task"] == "brief" and seen["style"] == "务实"
    assert seen["facts"]["项目编号"] == "CDYJR2026008-C-16110001"
    assert {p.name for p in act.iterdir()} == before      # 不落盘


def test_llm_fill_writes_output(tmp_path, projects_root, monkeypatch):
    lib = _template_root(tmp_path)
    _docx_template(lib / "简报.docx", ["导语"])
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr("api.state.ROOT", tmp_path)
    act = _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    monkeypatch.setattr(app._llm, "write",
                        lambda task, facts, style="": {"导语": "AI 写的导语"})

    out = api_activity_llm_fill(
        "惠民项目", "01-走访调研-第一期",
        {"task": "brief", "template": "简报", "output": "07-简报.docx"},
        _Req(), ADMIN, None)

    dst = act / out["path"]
    assert dst.is_file()
    assert "AI 写的导语" in "\n".join(p.text for p in Document(str(dst)).paragraphs)
    assert out["generated"]["导语"] == "AI 写的导语"


def test_template_name_traversal_400(tmp_path, projects_root, monkeypatch):
    _template_root(tmp_path)
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr("api.state.ROOT", tmp_path)
    _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    with pytest.raises(app.HTTPException) as exc:
        api_activity_fill("惠民项目", "01-走访调研-第一期",
                              {"template": "../决算表"}, _Req(), ADMIN, None)
    assert exc.value.status_code == 400


# ---------------- D. 同步与检索 ----------------

def test_sync_feishu_schedules_background(projects_root, monkeypatch):
    called = []
    monkeypatch.setattr(_misc, "_run_feishu_sync", lambda project: called.append(project))
    bg = BackgroundTasks()

    out = api_sync_feishu({"project": "惠民项目"}, bg, _Req(), ADMIN, None)
    assert out["started"] is True
    assert [p["name"] for p in out["projects"]] == ["惠民项目"]
    assert called == []                    # 请求阶段不跑
    asyncio.run(bg())
    assert called == ["惠民项目"]           # 响应后才跑


def test_sync_feishu_unknown_project_404(projects_root):
    with pytest.raises(app.HTTPException) as exc:
        api_sync_feishu({"project": "不存在"}, BackgroundTasks(), _Req(), ADMIN, None)
    assert exc.value.status_code == 404


def test_cognee_index_schedules_background(projects_root, monkeypatch):
    called = []
    monkeypatch.setattr(_misc, "_run_cognee_index", lambda project: called.append(project))
    bg = BackgroundTasks()

    out = api_cognee_index({"project": ""}, bg, _Req(), ADMIN, None)
    assert out["started"] is True
    asyncio.run(bg())
    assert called == [""]


def test_cognee_datasets_with_stats(monkeypatch):
    monkeypatch.setattr(app._cognee_index, "list_datasets",
                        lambda root: [{"id": "d1", "name": "惠民",
                                       "createdAt": "2026-11-18"}])
    monkeypatch.setattr(app._cognee_index, "graph_summary",
                        lambda root: {"d1": {"numNodes": 278}})
    monkeypatch.setattr(app._cognee_index, "data_count", lambda root, did: 69)

    out = api_cognee_datasets(ADMIN)
    assert out["total"] == 1
    assert out["items"][0] == {"id": "d1", "name": "惠民", "count": 69,
                               "nodes": 278, "created_at": "2026-11-18"}


def test_cognee_datasets_upstream_error_502(monkeypatch):
    def boom(root):
        raise RuntimeError("连接失败")

    monkeypatch.setattr(app._cognee_index, "list_datasets", boom)
    with pytest.raises(app.HTTPException) as exc:
        api_cognee_datasets(ADMIN)
    assert exc.value.status_code == 502


# ---------------- E. 仪表盘 ----------------

def test_dashboard_three_projects(projects_root):
    _activity(projects_root, "惠民项目", "01-走访调研-第一期", complete=True)
    _activity(projects_root, "花都项目", "02-爱邻议事会-第一期", complete=False)
    # 激励项目没有活动，也应出现在项目列表里

    out = api_dashboard(ADMIN)
    assert len(out["projects"]) == 3
    by_name = {p["name"]: p for p in out["projects"]}
    assert by_name["惠民项目"]["activity_total"] == 1
    assert by_name["惠民项目"]["activity_complete"] == 1
    assert by_name["花都项目"]["activity_total"] == 1
    assert by_name["花都项目"]["activity_complete"] == 0
    assert by_name["激励项目"]["activity_total"] == 0

    assert len(out["recent"]) == 2
    assert {r["activity"] for r in out["recent"]} == {
        "01-走访调研-第一期", "02-爱邻议事会-第一期"}


def test_dashboard_filters_by_scope(projects_root):
    out = api_dashboard(VIEWER)
    assert [p["short"] for p in out["projects"]] == ["huimin"]
