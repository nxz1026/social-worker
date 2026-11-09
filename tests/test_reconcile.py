"""对账逻辑测试：md_state / find_entries / reconcile_dataset。

只测纯函数与台账传播，不碰真实 Cognee / 飞书（全部 monkeypatch）。
跑法：
    python3 -m pytest tests/test_reconcile.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import reconcile as rec  # noqa: E402
from delete import find_entries  # noqa: E402


def _write_md(root, rel, content):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _entry(**kw):
    base = {
        "name": "doc.pdf",
        "project": "huadu",
        "category": "其他",
        "original": "sources/projects/huadu/其他/doc.pdf",
        "md": "vault-huadu/md/其他/doc/doc.md",
        "status": "done",
    }
    base.update(kw)
    return base


# ---------- md_state ----------

def test_md_state_no_md(tmp_path):
    assert rec.md_state(tmp_path, {}) == ("no_md", None)
    assert rec.md_state(tmp_path, {"md": ""}) == ("no_md", None)


def test_md_state_missing(tmp_path):
    entry = {"md": "vault-huadu/md/其他/doc/doc.md"}
    assert rec.md_state(tmp_path, entry) == ("missing", None)


def test_md_state_same(tmp_path):
    p = _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v1")
    h = rec.sha256_of(p)
    state, cur = rec.md_state(
        tmp_path, {"md": "vault-huadu/md/其他/doc/doc.md", "md_sha256": h})
    assert state == "same"
    assert cur == h


def test_md_state_changed(tmp_path):
    p = _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v1")
    old = rec.sha256_of(p)
    p.write_text("v2", encoding="utf-8")
    state, cur = rec.md_state(
        tmp_path, {"md": "vault-huadu/md/其他/doc/doc.md", "md_sha256": old})
    assert state == "changed"
    assert cur == rec.sha256_of(p)


def test_md_state_compares_sha256_not_md5(tmp_path):
    """台账里存的是 sha256（scan-inbox 写的也是 sha256），比对必须同算法。"""
    p = _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v1")
    state, _ = rec.md_state(
        tmp_path, {"md": "vault-huadu/md/其他/doc/doc.md",
                   "md_sha256": rec._cg.md5_of(p)})
    assert state == "changed", "md5 不能用来比对 md_sha256"


# ---------- find_entries ----------

def _manifest():
    return {"files": {
        "tok1": {"name": "a.pdf", "md": "vault-huadu/md/其他/a/a.md", "feishu_token": "ft1"},
        "tok2": {"name": "b.pdf", "md": "vault-huadu/md/其他/b/b.md", "feishu_token": "ft2"},
        "tok3": {"name": "a.pdf", "md": "vault-huadu/md/其他/a2/a.pdf.md", "feishu_token": "ft3"},
    }}


def test_find_entries_by_token_name_md_feishu():
    m = _manifest()
    assert [t for t, _ in find_entries(m, "tok1")] == ["tok1"]
    assert [t for t, _ in find_entries(m, "a.pdf")] == ["tok1", "tok3"]
    assert [t for t, _ in find_entries(m, "vault-huadu/md/其他/b/b.md")] == ["tok2"]
    assert [t for t, _ in find_entries(m, "ft1")] == ["tok1"]
    assert find_entries(m, "nope") == []


# ---------- reconcile_dataset ----------

def _run(tmp_path, monkeypatch, entry, dry_run=False, restore_token="newtok"):
    pcfg = {"dataset": "huadu", "vault": "vault-huadu"}
    flow = {
        "projects": {"huadu": {"label": "花都", "dataset": "huadu", "vault": "vault-huadu"}},
        "category_folders": {"花都": {"其他": "foldertok"}},
    }
    manifest = {"files": {"tok1": entry}}

    monkeypatch.setattr(rec._cg, "sync_dataset",
                        lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0,
                                         "cognified": False, "error": None})
    deleted, restored = [], []
    monkeypatch.setattr(rec, "delete_feishu",
                        lambda e, root, dry: deleted.append(e) or True)
    monkeypatch.setattr(rec, "restore_feishu",
                        lambda e, root, flow_, dry: restored.append(e) or restore_token)

    st = rec.reconcile_dataset(tmp_path, pcfg, flow, manifest, "huadu", "key", dry_run)
    return st, manifest, deleted, restored


def test_reconcile_missing_md_marks_deleted(tmp_path, monkeypatch):
    st, manifest, deleted, _ = _run(tmp_path, monkeypatch, _entry())
    e = manifest["files"]["tok1"]
    assert e["status"] == "deleted"
    assert e["deleted_at"]
    assert e["delete_mode"] == "auto"
    assert len(deleted) == 1
    assert st["deleted"] == 1
    assert st["changes"] == ["删除 doc.pdf"]


def test_reconcile_changed_md_updates_hash(tmp_path, monkeypatch):
    p = _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v1")
    old = rec.sha256_of(p)
    p.write_text("v2", encoding="utf-8")
    st, manifest, deleted, _ = _run(tmp_path, monkeypatch, _entry(md_sha256=old))
    e = manifest["files"]["tok1"]
    assert e["md_sha256"] == rec.sha256_of(p)
    assert e["status"] == "done"
    assert st["updated"] == 1
    assert deleted == []


def test_reconcile_same_md_noop(tmp_path, monkeypatch):
    p = _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v1")
    st, manifest, deleted, _ = _run(
        tmp_path, monkeypatch, _entry(md_sha256=rec.sha256_of(p)))
    assert st["updated"] == 0 and st["deleted"] == 0
    assert deleted == []
    assert manifest["files"]["tok1"]["md_sha256"] == rec.sha256_of(p)


def test_reconcile_restores_deleted_md(tmp_path, monkeypatch):
    _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v1")
    entry = _entry(status="deleted", md_sha256="",
                   deleted_at="2026-01-01T00:00:00+08:00", delete_mode="auto")
    st, manifest, deleted, restored = _run(tmp_path, monkeypatch, entry)
    e = manifest["files"]["tok1"]
    assert e["status"] == "done"
    assert e["restored_at"]
    assert e["feishu_token"] == "newtok"
    assert "deleted_at" not in e and "delete_mode" not in e
    assert st["restored"] == 1
    assert len(restored) == 1
    assert deleted == []


def test_reconcile_restore_failure_keeps_deleted(tmp_path, monkeypatch):
    """恢复失败（原件缺失 / 飞书上传失败）→ 保持 deleted 下轮重试，不静默丢文档。"""
    _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v1")
    entry = _entry(status="deleted", md_sha256="",
                   deleted_at="2026-01-01T00:00:00+08:00", delete_mode="auto")
    st, manifest, deleted, restored = _run(tmp_path, monkeypatch, entry, restore_token="")
    e = manifest["files"]["tok1"]
    assert e["status"] == "deleted"
    assert "restored_at" not in e
    assert st["restored"] == 0
    assert st["errors"] == 1
    assert deleted == []
    assert len(restored) == 1


def test_reconcile_old_manifest_backfill_not_counted(tmp_path, monkeypatch):
    """老台账没有 md_sha256 → 只补字段，不算变更。"""
    p = _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v1")
    st, manifest, deleted, _ = _run(tmp_path, monkeypatch, _entry(md_sha256=None))
    e = manifest["files"]["tok1"]
    assert e["md_sha256"] == rec.sha256_of(p)
    assert st["updated"] == 0
    assert st["changes"] == []
    assert deleted == []


def test_reconcile_skips_pending(tmp_path, monkeypatch):
    _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v1")
    st, manifest, deleted, _ = _run(tmp_path, monkeypatch, _entry(status="pending"))
    assert st["deleted"] == 0 and st["updated"] == 0
    assert deleted == []
    assert manifest["files"]["tok1"]["status"] == "pending"


def test_cognee_sync_error_does_not_touch_manifest(tmp_path, monkeypatch):
    """Cognee 入库失败时不能更新 md_sha256，否则差异被永久掩盖。"""
    p = _write_md(tmp_path, "vault-huadu/md/其他/doc/doc.md", "v2")
    old = "deadbeef"
    pcfg = {"dataset": "huadu", "vault": "vault-huadu"}
    flow = {"projects": {}, "category_folders": {}}
    manifest = {"files": {"tok1": _entry(md_sha256=old)}}
    monkeypatch.setattr(rec._cg, "sync_dataset",
                        lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0,
                                         "cognified": False, "error": "add 失败"})
    st = rec.reconcile_dataset(tmp_path, pcfg, flow, manifest, "huadu", "key", False)
    assert st["errors"] == 1
    assert manifest["files"]["tok1"]["md_sha256"] == old
    assert manifest["files"]["tok1"]["status"] == "done"
