"""M6：`cognee.py sync` 去重逻辑测试。

只测纯函数 `plan_sync()` / `md5_of()`，不需要 Cognee 服务。
跑法：
    python3 -m pytest tests/test_sync.py -q
或：
    python3 tests/test_sync.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from cognee import md5_of, plan_sync  # noqa: E402


def entry(id_, name, content_hash, ext="txt"):
    return {"id": id_, "name": name, "contentHash": content_hash, "extension": ext}


def make_job(directory, filename, content):
    """写一个 md，返回 (Path, contentHash)。"""
    p = Path(directory) / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return (p, md5_of(p))


def test_md5_is_stable_and_content_sensitive():
    with tempfile.TemporaryDirectory() as d:
        a = make_job(d, "a.md", "hello")
        b = make_job(d, "b.md", "hello")
        c = make_job(d, "c.md", "world")
    assert len(a[1]) == 32
    assert a[1] == b[1], "同内容应得到同 hash"
    assert a[1] != c[1], "不同内容必须得到不同 hash"


def test_second_run_is_noop():
    """连跑两次：第二次什么都不加、什么都不改、什么都不删。"""
    with tempfile.TemporaryDirectory() as d:
        j = make_job(d, "doc.md", "v1")
        entries = [entry("e1", "doc", j[1])]
        to_add, to_update, to_delete = plan_sync(entries, [j])
    assert to_add == []
    assert to_update == []
    assert to_delete == []


def test_changed_content_replaces_old_entry():
    """同名 md 内容变了 → 旧条目进 to_update（删旧条目重加），不是 to_delete。"""
    with tempfile.TemporaryDirectory() as d:
        old = make_job(d, "doc.md", "v1")
        entries = [entry("e1", "doc", old[1])]
        new = make_job(d, "doc.md", "v2")
        to_add, to_update, to_delete = plan_sync(entries, [new])
    assert to_delete == []
    assert [e["id"] for e, _, _ in to_update] == ["e1"]
    assert [str(m) for _, m, _ in to_update] == [str(new[0])]
    assert to_add == []


def test_orphan_entry_removed():
    """数据集里有、本地 md 已删/已移走 → 删掉。"""
    with tempfile.TemporaryDirectory() as d:
        j = make_job(d, "keep.md", "x")
        e2 = entry("e2", "gone", "abc")
        entries = [entry("e1", "keep", j[1]), e2]
        to_add, to_update, to_delete = plan_sync(entries, [j])
    assert to_delete == [e2]
    assert to_add == []
    assert to_update == []


def test_duplicate_entry_deduped():
    """同名同内容的重复条目只留一条。"""
    with tempfile.TemporaryDirectory() as d:
        j = make_job(d, "doc.md", "v1")
        entries = [entry("e1", "doc", j[1]), entry("e2", "doc", j[1])]
        to_add, to_update, to_delete = plan_sync(entries, [j])
    assert [e["id"] for e in to_delete] == ["e2"]
    assert to_add == []
    assert to_update == []


def test_non_text_entries_preserved():
    """图片等非文本条目不参与镜像（M1 会把图片加进同一个数据集）。"""
    with tempfile.TemporaryDirectory() as d:
        j = make_job(d, "doc.md", "v1")
        entries = [
            entry("e1", "doc", j[1]),
            entry("img1", "photo", "deadbeef", ext="jpg"),
            entry("img2", "报销单", "cafebabe", ext="png"),
        ]
        to_add, to_update, to_delete = plan_sync(entries, [j])
    assert to_add == []
    assert to_update == []
    assert to_delete == []


def test_missing_hash_treated_as_stale():
    """老条目没有 contentHash → 算「改」：删旧条目重加，不重复入库。"""
    with tempfile.TemporaryDirectory() as d:
        j = make_job(d, "doc.md", "v1")
        entries = [entry("e1", "doc", None)]
        to_add, to_update, to_delete = plan_sync(entries, [j])
    assert [e["id"] for e, _, _ in to_update] == ["e1"]
    assert len(to_add) == 0
    assert to_delete == []


def test_same_stem_in_two_dirs():
    """不同目录下的同名 md 各有条目，不能互相误删。"""
    with tempfile.TemporaryDirectory() as d:
        a = make_job(d, "sub1/doc.md", "aaa")
        b = make_job(d, "sub2/doc.md", "bbb")
        entries = [entry("e1", "doc", a[1]), entry("e2", "doc", b[1])]
        to_add, to_update, to_delete = plan_sync(entries, [a, b])
    assert to_add == []
    assert to_update == []
    assert to_delete == []


def test_new_file_added():
    with tempfile.TemporaryDirectory() as d:
        old = make_job(d, "old.md", "x")
        entries = [entry("e1", "old", old[1])]
        new = make_job(d, "new.md", "y")
        to_add, to_update, to_delete = plan_sync(entries, [old, new])
    assert to_delete == []
    assert to_update == []
    assert to_add == [new]


def test_identical_jobs_added_once():
    """本地多份同名同内容的 md（或库里已保留同名同内容）只入一条，避免重复入库。"""
    with tempfile.TemporaryDirectory() as d:
        a = make_job(d, "sub1/doc.md", "same")
        b = make_job(d, "sub2/doc.md", "same")
        assert a[1] == b[1]
        # 库里什么都没有：只该加一条
        to_add, to_update, to_delete = plan_sync([], [a, b])
        assert len(to_add) == 1
        assert to_update == [] and to_delete == []
        # 库里已有同名同内容：两条本地 md 都不该再入
        entries = [entry("e1", "doc", a[1])]
        to_add, to_update, to_delete = plan_sync(entries, [a, b])
        assert to_add == []
        assert to_update == [] and to_delete == []


if __name__ == "__main__":
    for fn in (
        test_md5_is_stable_and_content_sensitive,
        test_second_run_is_noop,
        test_changed_content_replaces_old_entry,
        test_orphan_entry_removed,
        test_duplicate_entry_deduped,
        test_non_text_entries_preserved,
        test_missing_hash_treated_as_stale,
        test_same_stem_in_two_dirs,
        test_new_file_added,
        test_identical_jobs_added_once,
    ):
        fn()
        print(f"ok  {fn.__name__}")
    print("all passed")
