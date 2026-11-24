#!/usr/bin/env python3
"""活动编号生命周期：补编号、split 建目录自动带 code、act_year 不截断。

2026-11-24 客户端实测：活动缺 code 时所有生成接口报 CODE_MISSING，
而 hint 指的 PATCH 活动端点其实是重命名端点，强依赖 new_name ——
按提示操作必然失败，活动进入无法自愈的死状态。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

from engine import activities  # noqa: E402


def test_split_writes_activity_yaml_with_code(tmp_path):
    """split 建目录时必须写 activity.yaml，否则产出无编号活动。"""
    planned = [{"dirname": "01-测试-第一期"}, {"dirname": "02-测试-第二期"}]
    seq = iter(["CDYJR2026008-C-1111001", "CDYJR2026008-C-1111002"])

    def maker(item, used):
        return next(seq)

    created = activities.create_activity_dirs(tmp_path, planned, code_maker=maker)
    assert len(created) == 2
    for dirname in created:
        yaml_path = tmp_path / "活动" / dirname / "activity.yaml"
        assert yaml_path.is_file(), f"{dirname} 没写 activity.yaml"
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert data["code"].startswith("CDYJR")
        assert data["status"] == "created"


def test_split_without_code_maker_still_creates(tmp_path):
    """不给 code_maker 时行为与原来一致（只建目录），不因缺编号失败。"""
    created = activities.create_activity_dirs(tmp_path, [{"dirname": "01-x-第一期"}])
    assert created == ["01-x-第一期"]
    assert not (tmp_path / "活动" / "01-x-第一期" / "activity.yaml").exists()


def test_code_maker_failure_does_not_block_dir(tmp_path):
    """编号生成抛错不该阻断建目录。"""
    def boom(item, used):
        raise RuntimeError("编号服务挂了")

    created = activities.create_activity_dirs(tmp_path, [{"dirname": "01-y-第一期"}],
                                              code_maker=boom)
    assert created == ["01-y-第一期"]


def test_parse_code_act_year_is_single_digit_by_design():
    """编号格式只存活动年个位；这是格式约束，不是 bug。"""
    from engine import coding
    code = coding.make_code(year=2026, establish_month=8, act_type=1,
                            act_month=11, serial=1, act_year=2026)
    parsed = coding.parse_code(code)
    assert parsed["act_year"] == 6, "编号里存的是个位"


def test_api_code_endpoint_exposes_full_year():
    """API 返回值要能给完整年份，避免调用方以为 2026 被截断。"""
    src = (REPO / "api" / "routes" / "projects.py").read_text(encoding="utf-8")
    assert "act_year_full" in src
    assert "act_year_note" in src


def test_code_missing_hint_points_to_real_endpoint():
    """CODE_MISSING 的 hint 必须指向真实存在的补编号端点。"""
    src = (REPO / "api" / "routes" / "agent.py").read_text(encoding="utf-8")
    assert "/code" in src
    assert "PATCH /api/projects/{p}/activities/{a}/code" in src
    # 不该再指向那个其实是重命名的 PATCH 活动端点
    assert "PATCH 活动接口补编号" not in src


def test_backfill_endpoint_exists():
    src = (REPO / "api" / "routes" / "materials.py").read_text(encoding="utf-8")
    assert "activities/backfill-code" in src
    assert "def api_backfill_codes" in src
