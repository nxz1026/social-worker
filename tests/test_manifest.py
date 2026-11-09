"""台账统一模块：锁、原子写、损坏检测、POSIX 路径。"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import manifest as mf  # noqa: E402
import migrate_manifest_paths as mig  # noqa: E402


def _path(tmp_path):
    return tmp_path / "state" / "manifest.yaml"


def test_missing_manifest_is_empty(tmp_path):
    assert mf.load(_path(tmp_path)) == {"files": {}}


def test_corrupt_manifest_raises(tmp_path):
    p = _path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{{{ not yaml", encoding="utf-8")
    with pytest.raises(mf.ManifestCorrupt):
        mf.load(p)
    assert mf.load(p, strict=False) == {"files": {}}


def test_atomic_save_roundtrip(tmp_path):
    p = _path(tmp_path)
    mf.save(p, {"files": {"t": {"name": "a.docx"}}})
    assert mf.load(p)["files"]["t"]["name"] == "a.docx"
    assert not list(p.parent.glob(".*.tmp-*"))


def test_concurrent_update_keeps_all_entries(tmp_path):
    p = _path(tmp_path)
    mf.save(p, {"files": {}})

    def worker(i):
        mf.update_entry(p, f"t{i}", {"status": "done", "n": i})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = mf.load(p)
    assert len(data["files"]) == 20
    assert {v["n"] for v in data["files"].values()} == set(range(20))


def test_lock_timeout(tmp_path):
    p = _path(tmp_path)
    p.parent.mkdir(parents=True)
    with mf.lock(p):
        with pytest.raises(TimeoutError):
            with mf.lock(p, timeout=0.05):
                pass


def test_posix_path_helpers(tmp_path):
    assert mf.normalize_rel("a\\b\\c.md") == "a/b/c.md"
    assert mf.normalize_rel("./a/b.md") == "a/b.md"
    assert mf.rel_posix(tmp_path / "a" / "b.md", tmp_path) == "a/b.md"
    assert mf.entry_path(tmp_path, {"md": "a\\b.md"}) == tmp_path / "a" / "b.md"


def test_migrate_backslashes(tmp_path):
    p = _path(tmp_path)
    mf.save(p, {"files": {"t": {"md": "vault-huadu\\md\\a.md",
                                "original": "sources\\a.docx"}}})
    assert mig.migrate(tmp_path, dry_run=True) == 0
    assert "\\" in mf.load(p)["files"]["t"]["md"]      # dry-run 不写
    assert mig.migrate(tmp_path, dry_run=False) == 0
    data = mf.load(p)
    assert data["files"]["t"]["md"] == "vault-huadu/md/a.md"
    assert data["files"]["t"]["original"] == "sources/a.docx"
    assert list(p.parent.glob("manifest.yaml.bak-*"))
