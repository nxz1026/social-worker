#!/usr/bin/env python3
"""生成件覆盖保护：目标已存在且活动可能被人工改过时，必须 409 NEED_FORCE。

背景：engine/fill.py 的 doc.save 直接覆盖，Agent 调 /api/sw/run 时
会静默盖掉手工修改过的材料。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402


def _act_dir(tmp_path, status=None):
    """造一个活动目录，按需写 activity.yaml 的 status。"""
    act = tmp_path / "项目" / "活动" / "01-测试-第一期"
    act.mkdir(parents=True)
    if status is not None:
        (act / "activity.yaml").write_text(
            yaml.safe_dump({"status": status}, allow_unicode=True), encoding="utf-8")
    return act


def _tpl(tmp_path):
    tpl = tmp_path / "模板.docx"
    tpl.write_bytes(b"")
    return tpl


def test_new_file_allowed(tmp_path):
    """目标不存在：直接允许。"""
    act = _act_dir(tmp_path, "reviewed")
    dst = app._fill_output(act, {"output": "新文件.docx"}, _tpl(tmp_path))
    assert dst.name == "新文件.docx"


@pytest.mark.parametrize("status", ["reviewed", "signed", "edited", "imported"])
def test_existing_with_reviewed_status_blocked(tmp_path, status):
    """已存在且状态表明可能人工改过 → 409 NEED_FORCE。"""
    act = _act_dir(tmp_path, status)
    (act / "已有.docx").write_bytes(b"x")
    with pytest.raises(app.ApiError) as exc:
        app._fill_output(act, {"output": "已有.docx"}, _tpl(tmp_path))
    assert exc.value.status_code == 409
    assert exc.value.code == "NEED_FORCE"


def test_existing_with_generated_status_allowed(tmp_path):
    """纯生成件（status=generated）允许直接覆盖。"""
    act = _act_dir(tmp_path, "generated")
    (act / "生成件.docx").write_bytes(b"x")
    dst = app._fill_output(act, {"output": "生成件.docx"}, _tpl(tmp_path))
    assert dst.name == "生成件.docx"


def test_existing_without_status_allowed(tmp_path):
    """没有 activity.yaml 的活动按纯生成件处理。"""
    act = _act_dir(tmp_path, None)
    (act / "无名.docx").write_bytes(b"x")
    dst = app._fill_output(act, {"output": "无名.docx"}, _tpl(tmp_path))
    assert dst.name == "无名.docx"


def test_force_overrides_guard(tmp_path):
    """force=true 时放行。"""
    act = _act_dir(tmp_path, "reviewed")
    (act / "已有.docx").write_bytes(b"x")
    dst = app._fill_output(act, {"output": "已有.docx", "force": True}, _tpl(tmp_path))
    assert dst.name == "已有.docx"


def test_check_overwrite_false_skips_guard(tmp_path):
    """dry_run 场景不参与检查。"""
    act = _act_dir(tmp_path, "reviewed")
    (act / "已有.docx").write_bytes(b"x")
    dst = app._fill_output(act, {"output": "已有.docx"}, _tpl(tmp_path),
                           check_overwrite=False)
    assert dst.name == "已有.docx"


def test_force_accepts_string_true(tmp_path):
    """表单传来的 "true" 字符串也要认。"""
    act = _act_dir(tmp_path, "signed")
    (act / "已有.docx").write_bytes(b"x")
    dst = app._fill_output(act, {"output": "已有.docx", "force": "true"}, _tpl(tmp_path))
    assert dst.name == "已有.docx"
