#!/usr/bin/env python3
"""M4 错误队列测试。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eq = _load("error_queue")


def test_add_first_time_is_new(tmp_path):
    entry, is_new = eq.add(tmp_path, file="x.pdf", error="转换失败：坏文件")
    assert is_new is True
    assert entry["file"] == "x.pdf"
    assert entry["status"] == "open"
    assert len(entry["id"]) == 16


def test_add_duplicate_is_not_new(tmp_path):
    eq.add(tmp_path, file="x.pdf", error="转换失败：坏文件")
    entry, is_new = eq.add(tmp_path, file="x.pdf", error="转换失败：坏文件")
    assert is_new is False
    assert len(eq.list_open(tmp_path)) == 1, "同一条错误不该重复入队"


def test_add_same_file_different_error(tmp_path):
    eq.add(tmp_path, file="x.pdf", error="转换失败：A")
    eq.add(tmp_path, file="x.pdf", error="转换失败：B")
    assert len(eq.list_open(tmp_path)) == 2


def test_open_yaml_written(tmp_path):
    eq.add(tmp_path, file="a.jpg", error="OCR 失败")
    data = yaml.safe_load((tmp_path / "state" / "errors" / "open.yaml").read_text("utf-8"))
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["file"] == "a.jpg"


def test_trail_appended(tmp_path):
    eq.add(tmp_path, file="a.jpg", error="OCR 失败")
    eq.add(tmp_path, file="a.jpg", error="OCR 失败")  # 重复也进流水
    trail = eq.trail_path(tmp_path)
    assert trail.is_file()
    assert len(trail.read_text("utf-8").strip().splitlines()) == 2


def test_resolve(tmp_path):
    entry, _ = eq.add(tmp_path, file="x.pdf", error="坏")
    assert eq.resolve(tmp_path, entry["id"]) is True
    assert eq.list_open(tmp_path) == []
    assert eq.resolve(tmp_path, entry["id"]) is False, "重复关闭应返回 False"


def test_close_by_file(tmp_path):
    eq.add(tmp_path, file="x.pdf", error="A")
    eq.add(tmp_path, file="x.pdf", error="B")
    eq.add(tmp_path, file="y.pdf", error="C")
    assert eq.close_by_file(tmp_path, "x.pdf") == 2
    assert len(eq.list_open(tmp_path)) == 1


def test_open_yaml_tolerates_garbage(tmp_path):
    d = tmp_path / "state" / "errors"
    d.mkdir(parents=True)
    (d / "open.yaml").write_text("这不是 yaml: [", "utf-8")
    assert eq.list_open(tmp_path) == []


def test_error_id_stable():
    assert eq.error_id("a", "b") == eq.error_id("a", "b")
    assert eq.error_id("a", "b") != eq.error_id("a", "c")


def test_brief_error_takes_first_line():
    exc = RuntimeError(
        "docling: Input document /x.pdf is not valid.\n"
        "Traceback (most recent call last):\n"
        "  File \"/app/docling_parse.py\", line 1706, in load\n"
        "    raise _load_er"
    )
    out = eq.brief_error(exc)
    assert out == "docling: Input document /x.pdf is not valid."
    assert len(out) < 100


def test_brief_error_truncates():
    assert len(eq.brief_error("x" * 1000, limit=50)) == 50


def test_brief_error_empty_uses_type_name():
    assert eq.brief_error(ValueError()) == "ValueError"
