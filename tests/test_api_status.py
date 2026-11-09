#!/usr/bin/env python3
"""文档状态计算（api/status.py）测试。

钉住：md5 vs sha256、飞书五态、索引三态、[[实体]] 解析。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))

import status  # noqa: E402


def test_md5_and_sha256(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    assert status.md5_of(p) == hashlib.md5(b"hello").hexdigest()
    assert status.sha256_of(p) == hashlib.sha256(b"hello").hexdigest()


def test_extract_entities_dedup_and_alias():
    text = "见 [[惠民]]、[[民乐北巷|北巷]]，再 [[惠民]]"
    assert status.extract_entities(text) == ["惠民", "民乐北巷"]


def test_relations_group_by_roster():
    out = status.relations_status("[[惠民]] [[志愿者]]", {"惠民": "projects"})
    assert out["count"] == 2
    assert out["by_type"]["projects"] == ["惠民"]
    assert out["by_type"]["其他"] == ["志愿者"]


def test_feishu_states():
    tpl = "https://f/{token}"
    assert status.feishu_status({"status": "deleted", "feishu_token": "x"}, "local-1", tpl)["state"] == "deleted"
    assert status.feishu_status({"status": "done", "feishu_token": "x"}, "local-1", tpl)["state"] == "uploaded"
    assert status.feishu_status({"status": "done"}, "RCynb2", tpl)["state"] == "moved"
    assert status.feishu_status({"status": "done"}, "local-1", tpl)["state"] == "missing"
    assert status.feishu_status({"status": "pending"}, "local-1", tpl)["state"] == "none"
    moved = status.feishu_status({"status": "done"}, "RCynb2", tpl)
    assert moved["url"] == "https://f/RCynb2"


def test_index_states(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("hello", encoding="utf-8")
    md5 = status.md5_of(md)
    assert status.index_status(md, "a", [])["state"] == "missing"
    assert status.index_status(md, "a", None)["state"] == "unknown"
    assert status.index_status(md, "a", [{"name": "a", "contentHash": md5, "extension": "txt"}])["state"] == "indexed"
    assert status.index_status(md, "a", [{"name": "a", "contentHash": "old", "extension": "txt"}])["state"] == "stale"
    # 同名但非文本条目不算
    assert status.index_status(md, "a", [{"name": "a", "contentHash": md5, "extension": "png"}])["state"] == "missing"


def test_doc_status_full(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "vault").mkdir()
    src = tmp_path / "sources" / "a.pdf"
    src.write_bytes(b"PDF")
    md = tmp_path / "vault" / "a.md"
    md.write_text("[[惠民]]", encoding="utf-8")
    entry = {
        "original": "sources/a.pdf",
        "md": "vault/a.md",
        "sha256": "src-sha",
        "md_sha256": "md-sha",
        "status": "done",
        "feishu_token": "ft",
        "feishu_uploaded_at": "2026-11-08T10:00:00+08:00",
    }
    out = status.doc_status(
        tmp_path, "local-1", entry,
        [{"name": "a", "contentHash": status.md5_of(md), "extension": "txt"}],
        "https://f/{token}", {"惠民": "projects"},
    )
    assert out["source"]["state"] == "ok"
    assert out["md"]["state"] == "ok"
    assert out["feishu"]["state"] == "uploaded"
    assert out["index"]["state"] == "indexed"
    assert out["relations"]["by_type"]["projects"] == ["惠民"]


def test_backlinks_and_shared_entities(tmp_path):
    """反链：谁写了 [[这一篇]]；共享实体：谁和我提到同一个实体。"""
    (tmp_path / "md").mkdir()
    a = tmp_path / "md" / "甲.md"
    a.write_text("[[惠民]] [[民乐北巷]]", encoding="utf-8")
    b = tmp_path / "md" / "乙.md"
    b.write_text("参见 [[甲]] 和 [[惠民]]", encoding="utf-8")
    c = tmp_path / "md" / "丙.md"
    c.write_text("无关内容", encoding="utf-8")

    docs = [("t-a", "甲.md", "md/甲.md"),
            ("t-b", "乙.md", "md/乙.md"),
            ("t-c", "丙.md", "md/丙.md")]
    out = status.backlinks(tmp_path, "t-a", {"name": "甲.md", "md": "md/甲.md"}, docs)

    assert out["count"] == 1
    assert out["backlinks"][0]["token"] == "t-b"
    shared = {s["entity"]: [d["token"] for d in s["docs"]] for s in out["shared"]}
    assert shared["惠民"] == ["t-b"]
    assert "民乐北巷" not in shared        # 只有自己提到，不算共享


def test_backlinks_skips_self_and_deleted_md(tmp_path):
    (tmp_path / "md").mkdir()
    (tmp_path / "md" / "甲.md").write_text("[[甲]] [[惠民]]", encoding="utf-8")
    docs = [("t-a", "甲.md", "md/甲.md"), ("t-x", "无.md", "md/无.md")]
    out = status.backlinks(tmp_path, "t-a", {"name": "甲.md", "md": "md/甲.md"}, docs)
    assert out["count"] == 0 and out["shared"] == []


def test_doc_entities_cached_by_mtime(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("[[惠民]]", encoding="utf-8")
    assert status.doc_entities(p) == ["惠民"]
    p.write_text("[[花都]]", encoding="utf-8")
    assert status.doc_entities(p) == ["花都"]
