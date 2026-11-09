#!/usr/bin/env python3
"""`convert-md.py --relink-only` 的护栏。

名册补了人/机构/地点之后，已有 md 不会自动长链接（增量转换只看源文件时间戳）。
补链必须满足：
  1. 只加 `[[实体]]`，不重转、不改其它正文；
  2. 已有的 `[[...]]` 和代码块里不动；
  3. 幂等 —— 跑第二遍不再改动。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def _load():
    spec = importlib.util.spec_from_file_location("convert_md", REPO / "scripts" / "convert-md.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "entities.yaml").write_text("projects:\n  - 惠民\norgs:\n  - 益嘉仁\n",
                                            encoding="utf-8")
    md = tmp_path / "vault" / "md" / "项目执行" / "a" / "a.md"
    md.parent.mkdir(parents=True)
    md.write_text("# 记录\n\n惠民社区开会，益嘉仁承办。\n\n已有 [[惠民]] 不动。\n",
                  encoding="utf-8")
    cfg = {"vault": tmp_path / "vault", "sources": tmp_path / "sources"}
    return mod, md, cfg


def test_relink_adds_links_without_touching_existing(tmp_path, monkeypatch):
    mod, md, cfg = _fixture(tmp_path, monkeypatch)
    changed, links = mod.relink(cfg)

    text = md.read_text(encoding="utf-8")
    assert changed == 1 and links == 2
    assert "[[惠民]]社区开会，[[益嘉仁]]承办。" in text
    assert "已有 [[惠民]] 不动。" in text          # 已有链接不重复包


def test_relink_is_idempotent(tmp_path, monkeypatch):
    mod, md, cfg = _fixture(tmp_path, monkeypatch)
    mod.relink(cfg)
    before = md.read_text(encoding="utf-8")
    assert mod.relink(cfg) == (0, 0)
    assert md.read_text(encoding="utf-8") == before


def test_relink_skips_code_blocks(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "entities.yaml").write_text("projects:\n  - 惠民\n", encoding="utf-8")
    md = tmp_path / "vault" / "md" / "a" / "a.md"
    md.parent.mkdir(parents=True)
    md.write_text("```\n惠民\n```\n", encoding="utf-8")
    cfg = {"vault": tmp_path / "vault", "sources": tmp_path / "sources"}

    assert mod.relink(cfg) == (0, 0)
    assert md.read_text(encoding="utf-8") == "```\n惠民\n```\n"


def test_relink_skips_pending_and_trash(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "entities.yaml").write_text("projects:\n  - 惠民\n", encoding="utf-8")
    base = tmp_path / "vault" / "md"
    for rel in ("_待确认/a/a.md", ".trash/a.md"):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("惠民", encoding="utf-8")
    cfg = {"vault": tmp_path / "vault", "sources": tmp_path / "sources"}

    assert mod.relink(cfg) == (0, 0)
    assert (base / "_待确认" / "a" / "a.md").read_text(encoding="utf-8") == "惠民"
