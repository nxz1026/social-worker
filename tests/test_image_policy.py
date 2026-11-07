"""M1 图片闸门 + 审计日志 测试。

跑法：
    python3 -m pytest tests/test_image_policy.py -q
或：
    python3 tests/test_image_policy.py

验收标准（计划里定的）：根目录放 10 张图跑一次，OCR 调用次数 = 0，
审计日志 10 条 action=archive_only。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"}


def _load(name, filename=None):
    """按文件名加载 scripts/ 下的模块（带连字符的文件名不能直接 import）。"""
    path = ROOT / "scripts" / (filename or f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


image_policy = _load("image_policy")
audit_log = _load("audit_log")

POLICY = image_policy.load_policy(ROOT / "config")

CONFIG = {
    "min_confidence": 0.7,
    "projects": {
        "huadu": {"label": "花都", "vault": "vault-huadu", "dataset": "huadu"},
        "huimin": {"label": "惠民", "vault": "vault-huimin", "dataset": "huimin"},
    },
    "categories": ["发票票据", "项目计划", "项目执行", "日常工作佐证", "政策文件", "其他"],
    "feishu": {"inbox": "test-inbox", "url_template": "https://example.com/file/{token}"},
    "category_folders": {},
}


# ---------- 单元：策略加载 ----------

def test_default_is_archive_only(tmp_path):
    assert image_policy.load_policy(tmp_path)["default"] == "archive_only"


def test_broken_yaml_falls_back(tmp_path):
    (tmp_path / "image_policy.yaml").write_text("rules: [this is not closed", encoding="utf-8")
    assert image_policy.load_policy(tmp_path)["default"] == "archive_only"


def test_non_dict_yaml_falls_back(tmp_path):
    (tmp_path / "image_policy.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    assert image_policy.load_policy(tmp_path)["default"] == "archive_only"


# ---------- 单元：decide_action ----------

def test_root_image_is_archive_only():
    assert image_policy.decide_action("photo.jpg", ".jpg", POLICY, IMAGE_EXTS) == "archive_only"


def test_ocr_dir():
    assert image_policy.decide_action("要OCR/photo.jpg", ".jpg", POLICY, IMAGE_EXTS) == "ocr"


def test_vision_dir_disabled_falls_back_to_default():
    # 「要识图」默认关闭 → 视为未授权，按 default 处理
    assert image_policy.decide_action("要识图/photo.jpg", ".jpg", POLICY, IMAGE_EXTS) == "archive_only"


def test_document_not_affected():
    assert image_policy.decide_action("要OCR/报告.docx", ".docx", POLICY, IMAGE_EXTS) is None


def test_no_policy_not_affected():
    assert image_policy.decide_action("photo.jpg", ".jpg", None, IMAGE_EXTS) is None


def test_unknown_ext_not_affected():
    assert image_policy.decide_action("photo.xyz", ".xyz", POLICY, IMAGE_EXTS) is None


# ---------- 单元：审计日志 ----------

def test_audit_append(tmp_path):
    audit_log.append(tmp_path, {"file": "a.jpg", "action": "archive_only", "status": "done"})
    audit_log.append(tmp_path, {"file": "b.jpg", "action": "archive_only", "status": "done"})
    day = next((tmp_path / "state" / "audit").glob("*.jsonl"))
    lines = [json.loads(x) for x in day.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["action"] == "archive_only"
    assert lines[0]["file"] == "a.jpg"
    assert "ts" in lines[0]


def test_audit_prune(tmp_path):
    import datetime as dt

    d = tmp_path / "state" / "audit"
    d.mkdir(parents=True)
    today = dt.date.today()
    keep = d / f"{today}.jsonl"
    keep.write_text("{}\n", encoding="utf-8")
    old = d / f"{today - dt.timedelta(days=200)}.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    junk = d / "not-a-date.jsonl"
    junk.write_text("{}\n", encoding="utf-8")

    assert audit_log.prune(tmp_path, 90) == 1
    assert keep.exists() and junk.exists() and not old.exists()


def test_audit_prune_disabled(tmp_path):
    assert audit_log.prune(tmp_path, 0) == 0


# ---------- 集成：计划验收 ----------

def test_root_images_skip_ocr(tmp_path, monkeypatch):
    """根目录 10 张图 → OCR 调用 0 次，审计 10 条 action=archive_only。"""
    si = _load("scan_inbox", "scan-inbox.py")

    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True)
    (tmp_path / "sources").mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    shutil.copy2(ROOT / "config" / "image_policy.yaml", cfg_dir / "image_policy.yaml")
    (cfg_dir / "feishu-flow.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")

    rels = []
    for i in range(10):
        rel = f"photo{i}.jpg"
        (inbox / rel).write_bytes(b"\xff\xd8\xff\xe0" + bytes([i]) * 64)
        rels.append(rel)

    monkeypatch.setattr(si, "ROOT", tmp_path)
    monkeypatch.setattr(si, "INBOX", inbox)
    monkeypatch.setattr(si, "PENDING", tmp_path / "sources" / "_待确认")
    monkeypatch.setattr(si, "MANIFEST_PATH", tmp_path / "state" / "manifest.yaml")
    monkeypatch.setattr(si, "CONFIG_PATH", cfg_dir / "feishu-flow.yaml")
    monkeypatch.setattr(si, "CFG", type("C", (), {"path": cfg_dir / "config.yaml"})())
    monkeypatch.setattr(si, "pull_inbox",
                        lambda token: [{"file_token": f"t{i}", "rel_path": r} for i, r in enumerate(rels)])
    monkeypatch.setattr(si, "load_llm", lambda: {})
    monkeypatch.setattr(si, "classify",
                        lambda *a, **k: {"project": "", "category": "其他",
                                         "confidence": 0.1, "title": ""})
    monkeypatch.setattr(si, "move_to_archive", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["scan-inbox.py", "--no-cognify"])

    ocr_calls = []
    monkeypatch.setattr(si, "ocr_image", lambda src: ocr_calls.append(src) or "")

    assert si.main() == 0

    # 验收 1：OCR 一次都没跑
    assert ocr_calls == [], f"OCR 被调用了 {len(ocr_calls)} 次"

    # 验收 2：审计 10 条 archive_only
    day = next((tmp_path / "state" / "audit").glob("*.jsonl"))
    lines = [json.loads(x) for x in day.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 10
    assert {x["action"] for x in lines} == {"archive_only"}

    # 附带：10 个文件都落到了 sources/_待确认/<类别>/（文件名无项目关键词，规则判不了）
    # 落点必须与 PENDING 常量、fallback._locate 一致 —— 以前写成 `_待确认/` 会落到部署根
    # 目录下另起一个目录，兜底清单按 sources/_待确认/ 找就找不到原件。
    assert len(list((tmp_path / "sources" / "_待确认" / "其他").iterdir())) == 10


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
