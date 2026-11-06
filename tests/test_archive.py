"""M5 archive.py 与「本地文件进 inbox/」流程测试。"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import archive as ar  # noqa: E402


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / fname)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rules():
    return ar._cr.load_rules(ROOT / "config")


# ---------- 定项目 ----------

def test_project_explicit_wins():
    assert ar.decide_project("随便什么.txt", _rules(), None, "huadu")[0] == "huadu"


def test_project_from_filename():
    project, decision = ar.decide_project("2026-11-06-惠民-活动方案.docx", _rules(), None, None)
    assert project == "huimin"
    assert decision["reason"] == "rule"


def test_project_undecidable_returns_none():
    assert ar.decide_project("无项目线索.txt", _rules(), None, None)[0] is None


def test_project_filtered_by_known_projects():
    """projects 白名单会过滤掉配置里的未知 key。"""
    project, _ = ar.decide_project("2026-11-06-惠民-x.docx", _rules(),
                                   ["huadu", "huimin"], None)
    assert project == "huimin"


# ---------- 动作与落位 ----------

def test_action_non_image_is_none():
    policy = {"rules": [], "default": "archive_only"}
    assert ar.resolve_action("auto", ".docx", policy) is None


def test_action_image_defaults_to_archive_only():
    policy = {"rules": [{"dir": "要OCR", "action": "ocr"}], "default": "archive_only"}
    assert ar.resolve_action("auto", ".jpg", policy) == "archive_only"


def test_action_explicit_overrides_policy():
    policy = {"rules": [], "default": "archive_only"}
    assert ar.resolve_action("ocr", ".jpg", policy) == "ocr"


def test_dest_rel():
    assert ar.dest_rel("ocr", "a.jpg") == Path("要OCR") / "a.jpg"
    assert ar.dest_rel("vision", "a.jpg") == Path("要识图") / "a.jpg"
    assert ar.dest_rel("archive_only", "a.jpg") == Path("a.jpg")
    assert ar.dest_rel(None, "a.docx") == Path("a.docx")


# ---------- 校验 ----------

def test_verify_ok(tmp_path):
    (tmp_path / "vault-huimin/md/项目执行/x").mkdir(parents=True)
    md = tmp_path / "vault-huimin/md/项目执行/x/x.md"
    md.write_text("# x", encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "state/manifest.yaml").write_text(yaml.safe_dump(
        {"files": {"t1": {"name": "x.docx", "project": "huimin",
                          "category": "项目执行", "md": "vault-huimin/md/项目执行/x/x.md"}}},
        allow_unicode=True), encoding="utf-8")
    ok, lines = ar.verify(tmp_path, "x.docx")
    assert ok, lines
    assert any("台账有记录" in ln for ln in lines)


def test_verify_missing_md(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state/manifest.yaml").write_text(yaml.safe_dump(
        {"files": {"t1": {"name": "x.docx", "md": "vault-huimin/md/x.md"}}}), encoding="utf-8")
    ok, lines = ar.verify(tmp_path, "x.docx")
    assert not ok
    assert any("md 缺失" in ln for ln in lines)


def test_verify_no_manifest_entry(tmp_path):
    (tmp_path / "state").mkdir()
    ok, lines = ar.verify(tmp_path, "不存在.docx")
    assert not ok
    assert any("台账里没有" in ln for ln in lines)


# ---------- 端到端：本地文件进 inbox/ 被处理 ----------

def test_local_file_in_inbox_gets_archived(tmp_path, monkeypatch):
    """手工放进 inbox/ 的文件也要被处理（M5 的核心）。"""
    si = _load("scan_inbox_local", "scan-inbox.py")

    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    (tmp_path / "sources").mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for f in ("image_policy.yaml", "project_aliases.yaml",
              "category_keywords.yaml", "rule_weights.yaml"):
        (cfg_dir / f).write_text(
            (ROOT / "config" / f).read_text(encoding="utf-8"), encoding="utf-8")
    (cfg_dir / "feishu-flow.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")

    (inbox / "2026-11-06-惠民-本地放入.txt").write_text("民乐北巷活动记录\n", encoding="utf-8")

    monkeypatch.setattr(si, "ROOT", tmp_path)
    monkeypatch.setattr(si, "INBOX", inbox)
    monkeypatch.setattr(si, "PENDING", tmp_path / "sources" / "_待确认")
    monkeypatch.setattr(si, "MANIFEST_PATH", tmp_path / "state" / "manifest.yaml")
    monkeypatch.setattr(si, "CONFIG_PATH", cfg_dir / "feishu-flow.yaml")
    monkeypatch.setattr(si, "CFG", type("C", (), {"path": cfg_dir / "config.yaml"})())
    monkeypatch.setattr(si, "pull_inbox", lambda token: [])
    monkeypatch.setattr(si, "load_llm", lambda: {})
    monkeypatch.setattr(si, "classify",
                        lambda *a, **k: {"project": "", "category": "其他",
                                         "confidence": 0.1, "title": ""})
    monkeypatch.setattr(si, "move_to_archive", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["scan-inbox.py", "--no-cognify"])

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert si.main() == 0
    out = buf.getvalue()
    assert "inbox/ 里还有 1 个本地文件待处理" in out

    # 台账里是 local- 开头的 key
    manifest = yaml.safe_load((tmp_path / "state/manifest.yaml").read_text(encoding="utf-8"))
    keys = [k for k in (manifest.get("files") or {}) if k.startswith("local-")]
    assert len(keys) == 1
    rec = manifest["files"][keys[0]]
    assert rec["project"] == "huimin"
    assert rec["status"] == "done"          # 本地文件没有飞书可移，直接 done
    assert (tmp_path / rec["md"]).is_file()
    # 处理完 inbox/ 里的副本要清掉
    assert not (inbox / "2026-11-06-惠民-本地放入.txt").exists()


def test_local_file_md_has_no_feishu_link(tmp_path, monkeypatch):
    """本地文件 md 顶部不该有飞书链接（没有 file_token）。"""
    si = _load("scan_inbox_hdr", "scan-inbox.py")

    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    (tmp_path / "sources").mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for f in ("image_policy.yaml", "project_aliases.yaml",
              "category_keywords.yaml", "rule_weights.yaml"):
        (cfg_dir / f).write_text(
            (ROOT / "config" / f).read_text(encoding="utf-8"), encoding="utf-8")
    (cfg_dir / "feishu-flow.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    (inbox / "2026-11-06-惠民-无链接.txt").write_text("内容\n", encoding="utf-8")

    monkeypatch.setattr(si, "ROOT", tmp_path)
    monkeypatch.setattr(si, "INBOX", inbox)
    monkeypatch.setattr(si, "PENDING", tmp_path / "sources" / "_待确认")
    monkeypatch.setattr(si, "MANIFEST_PATH", tmp_path / "state" / "manifest.yaml")
    monkeypatch.setattr(si, "CONFIG_PATH", cfg_dir / "feishu-flow.yaml")
    monkeypatch.setattr(si, "CFG", type("C", (), {"path": cfg_dir / "config.yaml"})())
    monkeypatch.setattr(si, "pull_inbox", lambda token: [])
    monkeypatch.setattr(si, "load_llm", lambda: {})
    monkeypatch.setattr(si, "classify",
                        lambda *a, **k: {"project": "", "category": "其他",
                                         "confidence": 0.1, "title": ""})
    monkeypatch.setattr(si, "move_to_archive", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["scan-inbox.py", "--no-cognify"])
    with redirect_stdout(io.StringIO()):
        assert si.main() == 0

    manifest = yaml.safe_load((tmp_path / "state/manifest.yaml").read_text(encoding="utf-8"))
    md = tmp_path / next(iter(manifest["files"].values()))["md"]
    head = (tmp_path / md).read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith("> 原件：2026-11-06-惠民-无链接.txt")
    assert "](http" not in head


CONFIG = {
    "feishu": {"root": "r", "inbox": "i", "archive": "a",
               "url_template": "https://x.feishu.cn/file/{token}"},
    "projects": {
        "huadu": {"label": "花都", "folder": "f1", "dataset": "huadu",
                  "vault": "vault-huadu"},
        "huimin": {"label": "惠民", "folder": "f2", "dataset": "huimin",
                   "vault": "vault-huimin"},
    },
    "categories": ["发票票据", "项目计划", "项目执行", "日常工作佐证", "政策文件", "其他"],
    "min_confidence": 0.7,
    "category_folders": {"惠民": {"项目执行": "cf1"}},
}
