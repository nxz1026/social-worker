"""engine P1：项目主数据读取 + 活动拆分。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import activities, projects  # noqa: E402

HUADU_JILI = "花都社区激励资金项目"
HUIMIN = "惠民社区保障资金项目"
HUADU_BAOZHANG = "花都社区保障资金项目"


def test_ordinal_basic():
    assert activities.ordinal(1) == "一"
    assert activities.ordinal(10) == "十"
    assert activities.ordinal(12) == "十二"


def test_ordinal_tens_and_out_of_range():
    assert activities.ordinal(11) == "十一"
    assert activities.ordinal(20) == "二十"
    assert activities.ordinal(21) == "二十一"
    assert activities.ordinal(0) == "0"
    assert activities.ordinal(100) == "100"


def test_plan_activities_huadu_jili():
    data = projects.load_project(HUADU_JILI)
    acts = activities.plan_activities(data)
    assert len(acts) == 11
    assert acts[0]["dirname"].endswith("01-队伍招募与组建-第一期")
    assert acts[0]["seq"] == 1
    # count=3 的指标拆成三期的预算均摊
    assert acts[1]["dirname"].endswith("02-队伍赋能培训-第一期")
    assert acts[2]["dirname"].endswith("03-队伍赋能培训-第二期")
    assert acts[3]["dirname"].endswith("04-队伍赋能培训-第三期")
    assert acts[3]["activity_budget"] == round(4400 / 3, 2)


def test_plan_activities_huimin():
    data = projects.load_project(HUIMIN)
    acts = activities.plan_activities(data)
    assert len(acts) == 17
    assert acts[-1]["dirname"].endswith("17-项目成果展-第一期")
    assert [a["seq"] for a in acts] == list(range(1, 18))


def test_plan_activities_empty_indicators():
    assert activities.plan_activities({}) == []
    assert activities.plan_activities({"indicators": []}) == []


def test_load_project_rejects_traversal():
    with pytest.raises(ValueError):
        projects.load_project("..")
    with pytest.raises(ValueError):
        projects.load_project("../花都社区激励资金项目")
    with pytest.raises(ValueError):
        projects.load_project("")
    with pytest.raises(ValueError):
        projects.load_project("/etc")


def test_list_projects_includes_dir_without_yaml():
    items = projects.list_projects()
    assert len(items) == 3
    by_name = {p["name"]: p for p in items}
    assert by_name[HUADU_BAOZHANG]["has_config"] is False
    assert by_name[HUADU_BAOZHANG]["data"] is None
    assert by_name[HUADU_JILI]["has_config"] is True
    assert by_name[HUADU_JILI]["data"]["project"]["short"] == HUADU_JILI


def test_list_existing_activities_missing_dir_returns_empty(tmp_path):
    assert activities.list_existing_activities(tmp_path) == []


def test_no_extras_strips():
    assert activities.no_extras("  CDYJR2026001  ") == "CDYJR2026001"
