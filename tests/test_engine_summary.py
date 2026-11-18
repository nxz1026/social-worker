"""summary task：总结报告 19 个文字格的骨架、facts 解析与 人手分配 推导。

不调真实 LLM：网络调用一律 monkeypatch 掉。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import activity, llm  # noqa: E402
from scripts import gen_summary  # noqa: E402

SUMMARY_FIELDS = (
    "策划目标", "策划问题", "策划行动",
    "准备目标", "准备问题", "准备行动",
    "执行目标", "执行内容", "执行表现", "执行挑战", "执行行动",
    "宣传目标", "宣传成效",
    "总结目标", "总结挑战", "总结行动",
    "工作员表现", "工作员反思",
    "跟进工作",
)


# --------------------------------------------------------------------------
# 骨架
# --------------------------------------------------------------------------

def test_summary_task_has_19_fields():
    spec = llm.TASKS["summary"]
    assert len(spec["fields"]) == 19
    assert tuple(spec["fields"]) == SUMMARY_FIELDS
    assert set(spec["schema"]) == set(SUMMARY_FIELDS)


def test_summary_prompt_has_all_fields_and_style_scope():
    facts = {"活动名称": "共治行动（第一期）", "实际人数": 19,
             "环节": ["集合签到", "分组整治"], "满意度得分": 91.4}
    prompt = llm.build_prompt("summary", facts, "务实")

    for field in SUMMARY_FIELDS:
        assert f'"{field}"' in prompt
    # 文风只作用于两段
    assert "【文风适用范围】" in prompt
    assert "本次文风只作用于：工作员反思、宣传成效。" in prompt
    assert "- 实际人数：19" in prompt
    assert "- 环节：集合签到、分组整治" in prompt


def test_summary_normalize_requires_every_field():
    data = {field: "文字" for field in SUMMARY_FIELDS}
    data.pop("跟进工作")
    with pytest.raises(llm.LLMError, match="缺字段：跟进工作"):
        llm._normalize("summary", data)


def test_summary_write_uses_skeleton_order(monkeypatch):
    payload = {field: f"{field}内容" for field in SUMMARY_FIELDS}
    monkeypatch.setattr(llm, "_chat", lambda prompt, max_tokens=None: (
        "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"))
    result = llm.write("summary", {"活动名称": "共治行动（第一期）"}, style="务实")
    assert tuple(result) == SUMMARY_FIELDS


# --------------------------------------------------------------------------
# 人手分配
# --------------------------------------------------------------------------

def test_build_data_derives_staffing_from_staff_roles(tmp_path):
    """项目 yaml 读不到时按 1 人兜底；extra 给了人数就用 extra。"""
    data = activity.build_data("不存在的项目", str(tmp_path))
    assert data["人手分配"] == "1名项目社工"

    data = activity.build_data("不存在的项目", str(tmp_path),
                               extra={"社工人数": 3})
    assert data["人手分配"] == "3名项目社工"


def test_build_data_staffing_includes_grids(tmp_path):
    data = activity.build_data(
        "不存在的项目", str(tmp_path),
        extra={"社工人数": 3, "社区网格数": 2, "微网格数": 0})
    assert data["人手分配"] == "3名项目社工，2个社区网格、0个微网格"


def test_build_data_keeps_explicit_staffing(tmp_path):
    data = activity.build_data("不存在的项目", str(tmp_path),
                               extra={"人手分配": "3名项目社工（聂聃、刘佳、史佳红）"})
    assert data["人手分配"] == "3名项目社工（聂聃、刘佳、史佳红）"


# --------------------------------------------------------------------------
# facts 解析（纯文本，不碰 docx / LLM）
# --------------------------------------------------------------------------

def test_parse_person_skips_headcount_line():
    md = ("项目社工人数及分工：3人：①聂聃（现场总协调）；②刘佳（讲者）\n"
          "项目社工：聂聃                               项目负责人：刘佳\n")
    assert gen_summary._parse_person(md, "项目社工") == "聂聃"
    assert gen_summary._parse_person(md, "项目负责人") == "刘佳"


def test_parse_difficulties_from_table():
    md = ("| 预计困难 | 解决方案 |\n"
          "| --- | --- |\n"
          "| 1.车主不配合挪车 | 1.提前张贴告知 |\n"
          "| 2.挪车安全风险 | 2.两人一组 |\n"
          "\n正文结束\n")
    assert gen_summary._parse_difficulties(md) == ["车主不配合挪车", "挪车安全风险"]


def test_parse_survey_score_and_count():
    md = ("| 回收问卷份数 | 10 份 |\n"
          "| 平均得分 | 91.40 分 |\n")
    assert gen_summary._parse_score(md) == 91.4
    assert gen_summary._parse_questionnaires(md) == 10


def test_parse_staff_count():
    assert gen_summary._parse_staff_count("项目社工人数及分工：3人：①聂聃") == 3
    assert gen_summary._parse_staff_count("没有这一行") is None
