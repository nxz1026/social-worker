"""plan task：计划书文字部分的骨架校验 + build_data 展开多行表格。

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


# --------------------------------------------------------------------------
# 骨架
# --------------------------------------------------------------------------

def _plan_result() -> dict:
    """一份合法的模型输出：6 段文字 + 环节 3 条 + 预计困难 2 条 + 物资 2 条。"""
    return {
        "活动背景内容": "民乐北巷电瓶车乱停乱放长期挤占通道。",
        "活动目的内容": "依据治理公约开展实地整治，疏通通道。",
        "活动目标内容": "1. 清理占道车辆；2. 划定临时停放区；3. 形成对比影像。",
        "目标人群": "沿线居民、商户与电瓶车车主。",
        "宣传方式": "电话通知、公告栏张贴、商户微信群。",
        "筛选方式": "优先居民代表、党员志愿者与商户代表。",
        # 2026-11-23 追加：没指定也按合理值生成
        "活动地点": "民乐北巷街区",
        "服务对象": "沿线居民、商户与电瓶车车主",
        "预计人数": "20",
        "志愿者人数": "5",
        "环节": [
            {"阶段": "环节", "时间": "9:00", "内容": "集合签到，宣读公约。", "物资": "签到表、公约", "人员": "聂聃", "备注": ""},
            {"阶段": "环节", "时间": "9:00", "内容": "分组挪车，疏通通道。", "物资": "手套、警示锥", "人员": "全体社工", "备注": ""},
            {"阶段": "环节", "时间": "9:00", "内容": "复查拍照，清点物资。", "物资": "相机、物资清单", "人员": "刘佳", "备注": ""},
        ],
        "预计困难": [
            {"困难": "车主不配合挪车", "方案": "提前张贴告知，联合社区劝导"},
            {"困难": "挪车时剐蹭受伤", "方案": "两人一组，备急救包"},
        ],
        "物资": [
            {"类别": "广告宣传费", "名称": "横幅", "规格": "5米", "单价": 12},
            {"类别": "活动物资费", "名称": "KT板", "规格": "4个", "单价": 40},
        ],
    }


def test_plan_task_registered_with_fields_in_order():
    spec = llm.TASKS["plan"]
    assert spec["fields"][:9] == ("活动背景内容", "活动目的内容", "活动目标内容",
                                  "目标人群", "宣传方式", "筛选方式", "环节",
                                  "预计困难", "物资")
    assert set(spec["fields"][9:]) == {"活动地点", "服务对象", "预计人数",
                                       "志愿者人数"}
    assert set(spec["lists"]) == {"环节", "预计困难", "物资"}


def test_build_prompt_plan_has_nested_skeleton_and_row_rules():
    prompt = llm.build_prompt("plan", {"活动名称": "共治行动（第一期）"}, "务实")
    for marker in ("【角色】", "【文风：务实】", "【参考样例】",
                   "【本次要写的内容】"):
        assert marker in prompt
    # 骨架里带上了数组元素的结构
    assert '"环节"' in prompt and '"内容"' in prompt and '"人员"' in prompt
    assert '"预计困难"' in prompt and '"方案"' in prompt
    assert '"物资"' in prompt and '"类别"' in prompt and '"单价"' in prompt
    assert "环节给 3~5 条" in prompt
    assert "预计困难给 2~4 条" in prompt
    assert "物资清单给 3~8 条" in prompt
    assert "预算上限" in prompt


def test_build_prompt_plan_uses_task_intro():
    prompt = llm.build_prompt("plan", {}, "务实")
    assert "需要生成 6 段文字与 3 个列表" in prompt
    assert "需要生成 8 段" not in prompt


# --------------------------------------------------------------------------
# normalize：schema 校验
# --------------------------------------------------------------------------

def test_normalize_plan_keeps_text_and_rows():
    out = llm._normalize("plan", _plan_result())
    assert list(out) == list(llm.TASKS["plan"]["fields"])
    assert out["目标人群"] == "沿线居民、商户与电瓶车车主。"
    assert len(out["环节"]) == 3
    assert out["环节"][0] == {"阶段": "环节", "时间": "9:00",
                              "内容": "集合签到，宣读公约。",
                              "物资": "签到表、公约", "人员": "聂聃", "备注": ""}
    assert out["预计困难"][1]["方案"] == "两人一组，备急救包"
    assert out["物资"][0]["类别"] == "广告宣传费"
    assert out["物资"][1]["规格"] == "4个"


def test_normalize_plan_allows_empty_materials():
    """预算为 0 的活动可以给空物资清单（下限 0）。"""
    data = _plan_result()
    data["物资"] = []
    out = llm._normalize("plan", data)
    assert out["物资"] == []


def test_normalize_plan_drops_extra_row_fields():
    data = _plan_result()
    data["环节"][0]["备注"] = "多余"
    out = llm._normalize("plan", data)
    assert set(out["环节"][0]) == {"阶段", "时间", "内容", "物资", "人员", "备注"}


def test_normalize_plan_rejects_short_rows():
    data = _plan_result()
    data["环节"] = data["环节"][:1]
    with pytest.raises(llm.LLMError, match="环节"):
        llm._normalize("plan", data)

    data = _plan_result()
    data["预计困难"] = []
    with pytest.raises(llm.LLMError, match="预计困难"):
        llm._normalize("plan", data)


def test_normalize_plan_rejects_non_list_rows():
    data = _plan_result()
    data["环节"] = "集合签到；分组挪车"
    with pytest.raises(llm.LLMError, match="应为数组"):
        llm._normalize("plan", data)


def test_normalize_plan_rejects_row_missing_column():
    data = _plan_result()
    data["环节"][1].pop("物资")
    with pytest.raises(llm.LLMError, match="第 2 条缺「物资」"):
        llm._normalize("plan", data)


def test_normalize_plan_rejects_missing_text_field():
    data = _plan_result()
    data["宣传方式"] = "  "
    with pytest.raises(llm.LLMError, match="宣传方式"):
        llm._normalize("plan", data)


def test_write_plan_accepts_rows(monkeypatch):
    monkeypatch.setattr(llm, "_chat", lambda prompt, max_tokens=None: (
        "```json\n" + json.dumps(_plan_result(), ensure_ascii=False) + "\n```"))
    result = llm.write("plan", {"活动名称": "共治行动（第一期）"}, style="务实",
                       max_rewrite=0)
    assert len(result["环节"]) == 3
    assert result["活动目标内容"].startswith("1. 清理占道车辆")


# --------------------------------------------------------------------------
# build_data：环节 / 预计困难 展开
# --------------------------------------------------------------------------

def _rows(*texts: str) -> list[dict]:
    """造 N 条环节。"""
    return [{"内容": text, "物资": f"物资{i}", "人员": f"人员{i}"}
            for i, text in enumerate(texts, start=1)]


def test_build_data_expands_steps_to_indexed_keys(tmp_path):
    notes: list[str] = []
    data = activity.build_data(
        "不存在的项目", str(tmp_path),
        extra={"环节": _rows("签到", "挪车", "收尾"),
               "预计困难": [{"困难": "车主不配合", "方案": "提前告知"}]},
        notes=notes,
    )

    assert data["环节内容_1"] == "签到"
    assert data["环节物资_2"] == "物资2"
    assert data["环节人员_3"] == "人员3"
    # 模板 5 行：不足的补空串，避免文档里留下 {{环节内容_4}}
    assert data["环节内容_4"] == ""
    assert data["环节内容_5"] == ""
    assert data["预计困难_1"] == "车主不配合"
    assert data["解决方案_1"] == "提前告知"
    assert data["预计困难_2"] == ""
    # 结构化字段不走通用列表展开
    assert "环节_1" not in data
    assert notes == []


def test_build_data_truncates_steps_to_template_rows(tmp_path):
    notes: list[str] = []
    data = activity.build_data(
        "不存在的项目", str(tmp_path),
        extra={"环节": _rows(*[f"环节{i}" for i in range(1, 8)])},
        notes=notes,
    )

    assert data["环节内容_5"] == "环节5"
    assert "环节内容_6" not in data
    assert len(notes) == 1 and "共 7 条" in notes[0] and "模板只有 5 行" in notes[0]


def test_build_data_truncates_difficulties_to_two_rows(tmp_path):
    notes: list[str] = []
    data = activity.build_data(
        "不存在的项目", str(tmp_path),
        extra={"预计困难": [{"困难": f"困难{i}", "方案": f"方案{i}"}
                            for i in range(1, 5)]},
        notes=notes,
    )

    assert data["预计困难_2"] == "困难2"
    assert "预计困难_3" not in data
    assert len(notes) == 1 and "预计困难共 4 条" in notes[0]


def test_expand_plan_rows_warns_without_notes(capsys, tmp_path):
    """不传 notes 时截断信息打到 stderr，不静默丢数据。"""
    activity.build_data("不存在的项目", str(tmp_path),
                        extra={"环节": _rows(*[f"环节{i}" for i in range(1, 8)])})
    assert "模板只有 5 行" in capsys.readouterr().err
