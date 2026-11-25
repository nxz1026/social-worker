#!/usr/bin/env python3
"""活动目录创建 API 与命名校验（第 2 步：开放建目录 + 服务端验证）。

用户要求：「开放 API 建立目录，但是目录的名称等要符合要求，服务端要验证」。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

from engine import activities  # noqa: E402


@pytest.mark.parametrize("name", [
    "12-安全隐患专项排查-七彩花都B1",
    "04-爱邻议事会-第一期",
    "01-项目启动-第一期",
    "12-安全隐患专项排查-朝阳时代西锦",
])
def test_valid_dirnames_pass(name):
    warnings = activities.validate_activity_dirname(name)
    assert isinstance(warnings, list)


@pytest.mark.parametrize("name,reason", [
    ("安全隐患-第一期", "缺序号"),
    ("12-安全隐患专项排查", "缺期次或小区"),
    ("1-太短-第一期", "序号不是两位"),
    ("12--第一期", "活动名为空"),
    ("", "空串"),
    ("../etc/passwd", "路径穿越"),
    ("12-含/斜杠-第一期", "路径分隔符"),
    ("12-安全*隐患-第一期", "非法字符"),
])
def test_invalid_dirnames_rejected(name, reason):
    with pytest.raises(ValueError):
        activities.validate_activity_dirname(name)


def test_too_long_dirname_rejected():
    with pytest.raises(ValueError, match="过长"):
        activities.validate_activity_dirname("12-" + "活" * 90 + "-第一期")


def test_duplicate_activity_key_warns():
    """同一活动换序号：允许但要告警（方案 A）。"""
    warnings = activities.validate_activity_dirname(
        "13-安全隐患专项排查-七彩花都B1",
        existing=["12-安全隐患专项排查-七彩花都B1"])
    assert any("同活动已存在" in w for w in warnings)


def test_duplicate_seq_warns():
    warnings = activities.validate_activity_dirname(
        "12-新活动-第一期",
        existing=["12-安全隐患专项排查-七彩花都B1"])
    assert any("已被" in w and "目录使用" in w for w in warnings)


def test_activity_key_strips_seq():
    assert activities.activity_key("12-安全隐患专项排查-B1") == "安全隐患专项排查-B1"
    assert activities.activity_key("01-项目启动-第一期") == "项目启动-第一期"


def test_match_existing_ignores_seq():
    """方案 A：按活动名匹配，忽略序号。"""
    planned = ["13-安全隐患专项排查-七彩花都B1", "16-新活动-第一期"]
    existing = ["12-安全隐患专项排查-七彩花都B1"]
    to_create, to_skip = activities.match_existing(planned, existing)
    assert to_skip == ["13-安全隐患专项排查-七彩花都B1"]
    assert to_create == ["16-新活动-第一期"]


def test_match_existing_no_fuzzy_confusion():
    """第一期与第十期不能互相误配。"""
    to_create, to_skip = activities.match_existing(
        ["04-爱邻议事会-第十期"], ["04-爱邻议事会-第一期"])
    assert to_create == ["04-爱邻议事会-第十期"]
    assert to_skip == []


def test_create_api_route_registered():
    """POST /activities 与 POST /activities/split 都要在（且 split 不能被 helper 占住）。"""
    src = (REPO / "api" / "routes" / "activities.py").read_text(encoding="utf-8")
    assert '@router.post("/api/projects/{name}/activities")' in src
    assert '@router.post("/api/projects/{name}/activities/split")' in src
    assert "def api_create_activity" in src
    # helper 必须在装饰器之前，不能夹在装饰器与函数之间
    i_deco = src.index('@router.post("/api/projects/{name}/activities/split")')
    i_fn = src.index("def api_split_activities")
    between = src[i_deco:i_fn]
    assert "def _bump_serial" not in between
