"""删除脚本测试：find_entries / trash_md / main()。

不碰真实 Cognee / 飞书 / 通知（全部 monkeypatch），root 用 tmp_path。
跑法：
    python3 -m pytest tests/test_delete.py -q
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import delete as dele  # noqa: E402


def _write(p, content="x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _setup_root(tmp_path, entries, projects=None):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "manifest.yaml").write_text(
        yaml.safe_dump({"files": entries}, allow_unicode=True), encoding="utf-8")
    flow = {"projects": projects if projects is not None else {
        "huadu": {"label": "花都", "dataset": "huadu", "vault": "vault-huadu"}},
        "category_folders": {"花都": {"其他": "foldertok"}}}
    (tmp_path / "feishu-flow.yaml").write_text(
        yaml.safe_dump(flow, allow_unicode=True), encoding="utf-8")
    return tmp_path


def _fake_config(root):
    return type("C", (), {"root": root})()


# ---------- find_entries ----------

def test_find_entries_match_and_multi():
    manifest = {"files": {
        "tok1": {"name": "a.pdf", "md": "vault-huadu/md/其他/a/a.md", "feishu_token": "ft1"},
        "tok2": {"name": "b.pdf", "md": "vault-huadu/md/其他/b/b.md", "feishu_token": "ft2"},
        "tok3": {"name": "a.pdf", "md": "vault-huadu/md/其他/a2/a.pdf.md", "feishu_token": "ft3"},
    }}
    assert [t for t, _ in dele.find_entries(manifest, "tok1")] == ["tok1"]
    assert [t for t, _ in dele.find_entries(manifest, "a.pdf")] == ["tok1", "tok3"]
    assert [t for t, _ in dele.find_entries(manifest, "vault-huadu/md/其他/b/b.md")] == ["tok2"]
    assert [t for t, _ in dele.find_entries(manifest, "ft1")] == ["tok1"]
    assert dele.find_entries(manifest, "nope") == []


# ---------- trash_md ----------

def test_trash_md_moves_into_dated_trash(tmp_path):
    vault = tmp_path / "vault-huadu"
    md = _write(vault / "md" / "其他" / "doc" / "doc.md", "hello")
    moved = dele.trash_md(tmp_path, vault, "vault-huadu/md/其他/doc/doc.md", False)
    assert moved is not None
    assert (tmp_path / moved).is_file()
    assert not md.exists()
    assert ".trash" in moved


def test_trash_md_dry_run_keeps_file(tmp_path):
    vault = tmp_path / "vault-huadu"
    md = _write(vault / "md" / "其他" / "doc" / "doc.md", "hello")
    assert dele.trash_md(tmp_path, vault, "vault-huadu/md/其他/doc/doc.md", True) is None
    assert md.is_file()


def test_trash_md_missing_returns_none(tmp_path):
    vault = tmp_path / "vault-huadu"
    assert dele.trash_md(tmp_path, vault, "vault-huadu/md/其他/doc/doc.md", False) is None


def test_trash_md_tolerates_other_vault(tmp_path):
    """项目没配 vault 时 md_rel 不以 vault 目录名开头，不能炸。"""
    vault = tmp_path / "vault"
    md = _write(tmp_path / "vault-huadu" / "md" / "其他" / "doc" / "doc.md", "hello")
    moved = dele.trash_md(tmp_path, vault, "vault-huadu/md/其他/doc/doc.md", False)
    assert moved is not None
    assert (tmp_path / moved).is_file()
    assert not md.exists()


# ---------- main() ----------

def test_main_not_found(tmp_path, monkeypatch):
    _setup_root(tmp_path, {})
    monkeypatch.setattr(dele, "load_config", lambda path=None: _fake_config(tmp_path))
    monkeypatch.setattr(sys, "argv", ["delete.py", "nope"])
    with pytest.raises(SystemExit):
        dele.main()


def test_main_multiple_matches(tmp_path, monkeypatch):
    _setup_root(tmp_path, {
        "tok1": {"name": "a.pdf", "md": "vault-huadu/md/其他/a/a.md"},
        "tok2": {"name": "a.pdf", "md": "vault-huadu/md/其他/b/a.pdf.md"},
    })
    monkeypatch.setattr(dele, "load_config", lambda path=None: _fake_config(tmp_path))
    monkeypatch.setattr(sys, "argv", ["delete.py", "a.pdf"])
    with pytest.raises(SystemExit) as ei:
        dele.main()
    assert ei.value.code == 2


def test_main_dry_run_does_not_touch_files(tmp_path, monkeypatch):
    md = _write(tmp_path / "vault-huadu" / "md" / "其他" / "doc" / "doc.md", "hello")
    _setup_root(tmp_path, {
        "tok1": {"name": "doc.pdf", "project": "huadu", "category": "其他",
                 "md": "vault-huadu/md/其他/doc/doc.md", "feishu_token": "ft1",
                 "status": "done"},
    })
    monkeypatch.setattr(dele, "load_config", lambda path=None: _fake_config(tmp_path))
    monkeypatch.setattr(sys, "argv", ["delete.py", "doc.pdf"])
    called = []
    monkeypatch.setattr(dele._cg, "delete_by_name",
                        lambda *a, **k: called.append("delete_by_name") or 0)
    monkeypatch.setattr(dele._cg, "cognify",
                        lambda *a, **k: called.append("cognify") or True)
    monkeypatch.setattr(dele, "notify_send",
                        lambda *a, **k: called.append("notify_send") or True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert dele.main() == 0
    assert "dry-run" in buf.getvalue()
    assert md.is_file()
    assert called == []
    # 台账也没被改
    data = yaml.safe_load((tmp_path / "state" / "manifest.yaml").read_text(encoding="utf-8"))
    assert data["files"]["tok1"]["status"] == "done"


# ---------- 孤儿 md / 无 md 原件 ----------

def test_find_orphan_mds_same_stem_other_dir(tmp_path):
    vault = tmp_path / "vault-huadu"
    _write(vault / "md" / "其他" / "doc" / "doc.md")
    _write(vault / "md" / "其他" / "doc-local-" / "doc.md")
    _write(vault / ".trash" / "2026-11-14" / "md" / "其他" / "doc" / "doc.md")
    got = dele.find_orphan_mds(tmp_path, vault, "doc", "vault-huadu/md/其他/doc/doc.md")
    assert got == ["vault-huadu/md/其他/doc-local-/doc.md"]


def test_trash_source_moves_under_sources_trash(tmp_path):
    src = _write(tmp_path / "sources" / "_待确认" / "其他" / "x.jpg", "img")
    moved = dele.trash_source(tmp_path, "sources/_待确认/其他/x.jpg")
    assert not src.exists()
    assert (tmp_path / moved).is_file()
    assert ".trash" in moved and moved.endswith("_待确认/其他/x.jpg")


def test_main_trashes_source_for_pending_review(tmp_path, monkeypatch):
    """无 md 的待确认条目：原件是唯一产物，删除时要下架，不能留在 _待确认。"""
    src = _write(tmp_path / "sources" / "_待确认" / "其他" / "x.jpg", "img")
    _setup_root(tmp_path, {
        "tok1": {"name": "x.jpg", "project": "", "category": "其他",
                 "md": None, "original": "sources/_待确认/其他/x.jpg",
                 "status": "pending_review"},
    })
    monkeypatch.setattr(dele, "load_config", lambda path=None: _fake_config(tmp_path))
    monkeypatch.setattr(sys, "argv", ["delete.py", "tok1", "--yes", "--no-notify"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert dele.main() == 0
    assert not src.exists()
    assert "原件 →" in buf.getvalue()
    data = yaml.safe_load((tmp_path / "state" / "manifest.yaml").read_text(encoding="utf-8"))
    assert data["files"]["tok1"]["status"] == "deleted"


def test_main_removes_orphan_md(tmp_path, monkeypatch):
    """重复上传残留的孤儿 md 要一并下架，否则 reconcile 会把它重新索引。"""
    keep = _write(tmp_path / "vault-huadu" / "md" / "其他" / "doc" / "doc.md", "a")
    orphan = _write(tmp_path / "vault-huadu" / "md" / "其他" / "doc-local-" / "doc.md", "a")
    _setup_root(tmp_path, {
        "tok1": {"name": "doc.pdf", "project": "huadu", "category": "其他",
                 "md": "vault-huadu/md/其他/doc/doc.md", "status": "done"},
    })
    monkeypatch.setattr(dele, "load_config", lambda path=None: _fake_config(tmp_path))
    monkeypatch.setattr(sys, "argv", ["delete.py", "tok1", "--yes", "--no-notify"])
    monkeypatch.setattr(dele._cg, "load_key", lambda: "k")
    monkeypatch.setattr(dele._cg, "delete_by_name", lambda *a, **k: 1)
    monkeypatch.setattr(dele._cg, "cognify", lambda *a, **k: True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert dele.main() == 0
    assert not keep.exists() and not orphan.exists()
    assert buf.getvalue().count("md →") == 2


if __name__ == "__main__":
    for fn in (
        test_find_entries_match_and_multi,
        test_trash_md_moves_into_dated_trash,
        test_trash_md_dry_run_keeps_file,
        test_trash_md_missing_returns_none,
        test_trash_md_tolerates_other_vault,
    ):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        print(f"ok  {fn.__name__}")
    print("all passed")
