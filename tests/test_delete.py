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
