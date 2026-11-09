"""dry-run 必须纯只读：不建目录、不写台账。"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location("scan_inbox_dry", REPO / "scripts" / "scan-inbox.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_archive_one_dry_run_does_not_touch_manifest_or_dirs(tmp_path, monkeypatch):
    si = _load()
    monkeypatch.setattr(si, "ROOT", tmp_path)
    monkeypatch.setattr(si, "PENDING", tmp_path / "sources" / "_待确认")

    src = tmp_path / "inbox" / "2026-11-06-惠民-计划.txt"
    src.parent.mkdir(parents=True)
    src.write_text("内容", encoding="utf-8")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()

    cfg = {
        "feishu": {"url_template": "https://x.feishu.cn/file/{token}"},
        "projects": {"huimin": {"label": "惠民", "vault": "vault-huimin",
                                "dataset": "huimin"}},
        "categories": ["项目执行"],
        "category_folders": {"惠民": {"项目执行": "cf1"}},
    }
    decision = {"project": "huimin", "category": "项目执行", "confidence": 1.0}
    manifest = {"files": {}}

    status = si.archive_one(
        src, "tok", digest, decision, cfg, manifest,
        {"converter": "copy", "text": "内容", "outdir": tmp_path / "tmp"},
        dry_run=True, llm={}, action=None, name_info={})

    assert status == "dry-run"
    assert manifest == {"files": {}}
    assert not (tmp_path / "sources").exists()
    assert not (tmp_path / "vault-huimin").exists()
