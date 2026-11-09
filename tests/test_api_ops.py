#!/usr/bin/env python3
"""管理界面的写操作护栏（api/app.py 里的纯辅助函数）。

钉住三件**会伤到数据**的事：
  1. `_待确认` 的路径参数不能越界（否则一个 `../` 就能删系统文件）；
  2. 丢弃 / 清理只移到 `sources/.trash`，不能直接 unlink；
  3. 回收站路径 → 原位置的反推（delete.py 与 `_trash_file` 两种布局）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
# import app 会读配置（SETTINGS = load()）⇒ 先给一份假 DSN，避免连库
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402


def test_pending_path_accepts_only_under_pending_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ROOT", tmp_path)
    base = tmp_path / "sources" / "_待确认"
    base.mkdir(parents=True)
    f = base / "a.docx"
    f.write_bytes(b"x")

    assert app._safe_pending_path("sources/_待确认/a.docx") == f.resolve()

    for bad in ("../etc/passwd", "sources/huimin/a.docx", "", "sources/_待确认/../../x"):
        with pytest.raises(app.HTTPException):
            app._safe_pending_path(bad)


def test_trash_file_moves_instead_of_deleting(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ROOT", tmp_path)
    p = tmp_path / "sources" / "_待确认" / "a.docx"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")

    rel = app._trash_file(p)

    assert rel.startswith("sources/.trash/")
    assert (tmp_path / rel).is_file()
    assert not p.exists()


def test_trash_file_does_not_overwrite_same_name(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ROOT", tmp_path)
    base = tmp_path / "sources" / "_待确认"
    base.mkdir(parents=True)
    (base / "a.docx").write_bytes(b"1")
    first = app._trash_file(base / "a.docx")
    (base / "a.docx").write_bytes(b"2")
    second = app._trash_file(base / "a.docx")

    assert first != second
    assert (tmp_path / first).read_bytes() == b"1"
    assert (tmp_path / second).read_bytes() == b"2"


def test_restore_dest_parts_for_both_layouts():
    # delete.py：<vault>/.trash/<日期>/<vault 内相对路径>
    assert app._restore_dest_parts(
        ("vault-huimin", ".trash", "2026-11-08", "md", "项目计划", "a", "a.md")
    ) == ("vault-huimin", ("md", "项目计划", "a", "a.md"))
    # _trash_file：sources/.trash/<日期>/<文件名>
    assert app._restore_dest_parts(
        ("sources", ".trash", "2026-11-08", "a.docx")
    ) == ("sources", ("a.docx",))
    # 残缺路径 ⇒ 不猜
    assert app._restore_dest_parts(("a", ".trash", "2026-11-08")) is None
    assert app._restore_dest_parts((".trash", "2026-11-08", "a")) is None
    assert app._restore_dest_parts(("a", "b", "c")) is None


def test_save_manifest_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    app._save_manifest({"files": {"t1": {"name": "a.docx", "status": "done"}}})
    assert app._load_manifest()["files"]["t1"]["name"] == "a.docx"


def test_roster_summary_reports_missing_types(monkeypatch):
    monkeypatch.setattr(app, "_roster", lambda project="": {"花都": "projects", "惠民": "projects",
                                                             "益嘉仁": "orgs"})
    out = app._roster_summary("huadu")
    assert out["counts"] == {"projects": 2, "orgs": 1, "persons": 0, "places": 0}
    assert out["missing"] == ["persons", "places"]
    assert out["path"] == "entities.yaml"
    assert "--relink-only" in out["rebuild"]


def test_suggest_project_uses_filename_rules(tmp_path):
    f = tmp_path / "2026-11-11_惠民_活动计划书.docx"
    f.write_bytes(b"")
    project, why = app._suggest_project(f)
    assert project == "huimin"
    assert why == "规则判定"


def test_suggest_project_is_quiet_when_no_signal(tmp_path):
    f = tmp_path / "未命名文件.bin"
    f.write_bytes(b"")
    assert app._suggest_project(f) == ("", "")


def _feishu_env(tmp_path, monkeypatch):
    """飞书重传的最小环境：原件 + 台账条目 + 假 lark。"""
    import types
    monkeypatch.setattr(app, "ROOT", tmp_path)
    orig = tmp_path / "sources/projects/huimin/项目计划/a.docx"
    orig.parent.mkdir(parents=True)
    orig.write_bytes(b"x")
    entry = {"name": "a.docx", "project": "huimin", "category": "项目计划",
             "original": "sources/projects/huimin/项目计划/a.docx",
             "feishu_token": "old", "feishu_type": "file"}
    monkeypatch.setattr(app, "_manifest_entry", lambda tok: dict(entry))
    monkeypatch.setattr(app, "_flow", lambda: {
        "projects": {"huimin": {"label": "惠民"}},
        "category_folders": {"惠民": {"项目计划": "folderX"}}})
    manifest = {"files": {"t": dict(entry)}}
    monkeypatch.setattr(app, "_load_manifest", lambda: manifest)
    monkeypatch.setattr(app, "_save_manifest", lambda m: manifest.update(m))
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))
    return entry, manifest, req


def test_feishu_reupload_renames_old_then_trashes_after_upload(tmp_path, monkeypatch):
    entry, manifest, req = _feishu_env(tmp_path, monkeypatch)
    calls = []

    def fake_lark(args, timeout=300):
        calls.append(args)
        if args[1] == "+upload":
            return {"data": {"file_token": "new"}}
        return {}

    monkeypatch.setattr(app, "_lark", fake_lark)
    out = app.feishu_reupload("t", req, {"id": 4, "projects": ["huimin"]}, None)

    assert out["file_token"] == "new" and out["old_trashed"] is True
    assert [c[1] for c in calls] == ["+update-title", "+upload", "+delete"]
    # 旧副本先改名（带时间戳），新副本传成功后才删
    assert calls[0][calls[0].index("--title") + 1].startswith("a.旧-")
    assert calls[2][calls[2].index("--file-token") + 1] == "old"
    assert manifest["files"]["t"]["feishu_token"] == "new"


def test_feishu_reupload_restores_old_title_when_upload_fails(tmp_path, monkeypatch):
    entry, manifest, req = _feishu_env(tmp_path, monkeypatch)
    calls = []

    def fake_lark(args, timeout=300):
        calls.append(args)
        if args[1] == "+upload":
            raise app.HTTPException(502, "上传失败")
        return {}

    monkeypatch.setattr(app, "_lark", fake_lark)
    with pytest.raises(app.HTTPException):
        app.feishu_reupload("t", req, {"id": 4, "projects": ["huimin"]}, None)

    assert [c[1] for c in calls] == ["+update-title", "+upload", "+update-title"]
    assert calls[-1][calls[-1].index("--title") + 1] == "a"   # 改回原名
    assert manifest["files"]["t"]["feishu_token"] == "old"     # 台账没动


def test_trash_file_keeps_relative_path_for_restore(tmp_path, monkeypatch):
    """丢弃/归档残留要保留 `_待确认/<子目录>/`，恢复时才能放回原位。"""
    monkeypatch.setattr(app, "ROOT", tmp_path)
    p = tmp_path / "sources" / "_待确认" / "其他" / "a.docx"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")

    rel = app._trash_file(p)

    assert rel == "sources/.trash/" + rel.split("/")[2] + "/_待确认/其他/a.docx"
    assert (tmp_path / rel).is_file()
    assert app._restore_dest_parts(tuple(Path(rel).parts)) == (
        "sources", ("_待确认", "其他", "a.docx"))
