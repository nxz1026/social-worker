"""engine/llm.py：文风解析、prompt 拼装、JSON 解析与重试。

不调真实 LLM：网络调用一律 monkeypatch 掉，避免测试耗时耗钱。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import llm  # noqa: E402


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def test_load_style_config_has_three_styles_and_five_examples():
    config = llm.load_style_config()
    assert set(config["styles"]) == {"务实", "平实", "党建"}
    examples = [rel for spec in config["styles"].values()
                for rel in spec["examples"]]
    assert len(examples) == 5
    for rel in examples:
        assert (llm.STYLE_DIR / rel).is_file(), rel
    assert config["default"] == "务实"


def test_load_style_config_env_override(tmp_path, monkeypatch):
    path = tmp_path / "style.yaml"
    path.write_text(
        "default: 平实\nstyles:\n  平实:\n    desc: 白话\n"
        "    examples: []\n    extra: ''\n", encoding="utf-8")
    monkeypatch.setenv("SOCIAL_WORK_LLM_STYLE", str(path))
    config = llm.load_style_config()
    assert config["default"] == "平实"
    assert set(config["styles"]) == {"平实"}


# --------------------------------------------------------------------------
# 文风优先级
# --------------------------------------------------------------------------

def test_resolve_style_explicit_wins(monkeypatch):
    """explicit 覆盖项目配置与类型映射。"""
    monkeypatch.setattr(llm.projects, "load_project",
                        lambda name: {"llm_style": {"default": "党建"}})
    assert llm.resolve_style("某项目", "17-项目成果展-第一期", "平实") == "平实"


def test_resolve_style_project_default_before_type(monkeypatch):
    """项目 yaml 的 llm_style.default 优先于 type_style。"""
    monkeypatch.setattr(llm.projects, "load_project",
                        lambda name: {"llm_style": {"default": "平实"}})
    assert llm.resolve_style("某项目", "17-项目成果展-第一期") == "平实"


def test_resolve_style_matches_type_by_substring(monkeypatch):
    """活动名去掉编号与「-第X期」后，按子串匹配 type_style。"""
    monkeypatch.setattr(llm.projects, "load_project",
                        lambda name: {"llm_style": {}})
    assert llm.resolve_style("某项目", "11-共治行动-第一期") == "务实"
    assert llm.resolve_style("某项目", "17-项目成果展-第一期") == "党建"
    # 完整活动名里含类型词也能命中
    assert llm.resolve_style("", "惠民共治行动——街区通道疏通") == "务实"


def test_resolve_style_falls_back_to_config_default(monkeypatch):
    monkeypatch.setattr(llm.projects, "load_project",
                        lambda name: (_ for _ in ()).throw(FileNotFoundError))
    assert llm.resolve_style("", "没见过的活动") == "务实"


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

def _brief_facts() -> dict:
    return {
        "活动名称": "共治行动（第一期）",
        "活动时间": "2026年11月10日",
        "活动地点": "民乐北巷",
        "预计人数": 20,
        "活动目标": ["清理乱停车辆", "划定临时停放区"],
        "环节": ["集合签到", "分组整治"],
        "项目名称": "亲邻惠里项目",
    }


def test_build_prompt_contains_three_layers():
    prompt = llm.build_prompt("brief", _brief_facts(), "务实")
    for marker in ("【角色】", "【文风：务实】", "【参考样例】",
                   "【本次要写的内容】"):
        assert marker in prompt
    # 样例正文真的被拼进去了
    sample = (llm.STYLE_DIR / "务实" / "共治行动.txt").read_text(encoding="utf-8")
    assert sample.strip()[:40] in prompt
    assert "--- 样例 1 ---" in prompt


def test_build_prompt_renders_facts_and_skeleton():
    prompt = llm.build_prompt("brief", _brief_facts(), "务实")
    assert "- 活动地点：民乐北巷" in prompt
    assert "- 活动目标：清理乱停车辆、划定临时停放区" in prompt   # 列表用顿号连接
    assert '"简报导语"' in prompt and '"达到目标"' in prompt
    assert "全文合计 500~800 字" in prompt


def test_build_prompt_rejects_unknown_style():
    with pytest.raises(ValueError, match="未知文风"):
        llm.build_prompt("brief", _brief_facts(), "玄学")


def test_build_prompt_rejects_unknown_task():
    with pytest.raises(ValueError, match="未知任务"):
        llm.build_prompt("不存在的任务", _brief_facts(), "务实")


# --------------------------------------------------------------------------
# JSON 解析与重试（不打网络）
# --------------------------------------------------------------------------

def test_parse_json_tolerates_code_fence():
    text = '```json\n{"简报导语": "一", "诉求描述": "二", "达到目标": "三"}\n```'
    assert llm._parse_json(text)["简报导语"] == "一"


def test_parse_json_tolerates_surrounding_text():
    text = '以下是结果：\n{"简报导语": "一"}\n希望有帮助'
    assert llm._parse_json(text) == {"简报导语": "一"}


def test_parse_json_rejects_non_json():
    with pytest.raises(llm.LLMError):
        llm._parse_json("模型什么都没说")


def test_write_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_chat(prompt, max_tokens=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise llm.LLMError("上游 500")
        return '{"简报导语": "一", "诉求描述": "二", "达到目标": "三"}'

    monkeypatch.setattr(llm, "_chat", fake_chat)
    result = llm.write("brief", _brief_facts(), style="务实", max_rewrite=0)
    assert calls["n"] == 3
    assert {k: v for k, v in result.items() if not k.startswith("_")} == {
        "简报导语": "一", "诉求描述": "二", "达到目标": "三"}


def test_write_raises_after_three_failures(monkeypatch):
    calls = {"n": 0}

    def fake_chat(prompt, max_tokens=None):
        calls["n"] += 1
        return "不是 JSON"

    monkeypatch.setattr(llm, "_chat", fake_chat)
    with pytest.raises(llm.LLMError, match="已重试 2 次"):
        llm.write("brief", _brief_facts(), style="务实")
    assert calls["n"] == 3


def test_write_drops_extra_fields(monkeypatch):
    monkeypatch.setattr(llm, "_chat", lambda prompt, max_tokens=None: (
        '{"简报导语": "一", "诉求描述": "二", "达到目标": "三", "备注": "多余"}'))
    result = llm.write("brief", _brief_facts(), style="务实", max_rewrite=0)
    assert [key for key in result if not key.startswith("_")] == [
        "简报导语", "诉求描述", "达到目标"]


# --------------------------------------------------------------------------
# supervision（监督记录）
# --------------------------------------------------------------------------

def test_supervision_task_shape():
    """监督记录只有一个字段，字数区间与正式用途写在骨架里。"""
    spec = llm.TASKS["supervision"]

    assert spec["doc"] == "监督记录表"
    assert tuple(spec["fields"]) == ("活动内容",)
    assert "300~450" in spec["schema"]["活动内容"]
    assert any("首先/其次/再次/最后" in rule for rule in spec["rules"])


def test_supervision_prompt_has_rules_and_facts():
    facts = {"活动名称": "共治行动（第一期）", "活动时间": "2026年11月10日",
             "实际人数": 19, "决算金额": 1662}
    prompt = llm.build_prompt("supervision", facts, "务实")

    assert '"活动内容"' in prompt
    assert "以「年月日，某社区联合某机构在何处开展某事」开头" in prompt
    assert "必须写具体数字（人数、金额、数量）" in prompt
    assert "- 实际人数：19" in prompt


def test_supervision_normalize_requires_content():
    with pytest.raises(llm.LLMError, match="缺字段：活动内容"):
        llm._normalize("supervision", {"活动内容": "  "})


# --------------------------------------------------------------------------
# notice：活动通知正文（2026-11-24 用户要求「冷化正文 + 废话少」）
# --------------------------------------------------------------------------

def test_notice_task_shape():
    """只生成「活动简介」一个字段，字数写死在任务上（字符口径）。"""
    spec = llm.TASKS["notice"]

    assert spec["doc"] == "社区活动通知"
    assert tuple(spec["fields"]) == ("活动简介",)
    assert "60~100" in spec["schema"]["活动简介"]
    assert spec["max_chars"] == 120
    assert spec["min_chars"] == 40


def test_notice_rules_cover_hard_constraints():
    """工单 C 的六条硬约束一条都不能少（措辞可调整，意思不能弱）。"""
    rules = "\n".join(llm.TASKS["notice"]["rules"])

    assert "只写活动正文那一段" in rules and "不要分点" in rules
    assert "60~100 字" in rules and "120 字" in rules
    for slogan in ("贯彻落实", "浓厚氛围", "新格局", "双向奔赴"):
        assert slogan in rules, f"禁口号里漏了「{slogan}」"
    assert "不重复活动名称" in rules
    assert "不编造具体人名、金额、政策文号" in rules
    assert "这是什么活动、居民能来做什么、为什么值得来" in rules


def test_notice_prompt_renders_rules_facts_and_schema():
    facts = {"活动名称": "共治行动（第一期）", "活动时间_cn": "二〇二六年十一月十日",
             "活动地点": "民乐北巷", "服务对象": "院落居民"}
    prompt = llm.build_prompt("notice", facts, "务实")

    assert '"活动简介"' in prompt
    assert "- 活动地点：民乐北巷" in prompt
    assert "60~100 字" in prompt
    # 结尾那段是模型最先参照的，字数硬约束要在里面
    # 末条规则：禁止「我们」自称 —— 模板正文已有「我们将在…」，接「我们在…」会重复
    # （2026-11-24 实测生成「…进行志愿服务，我们在菜市设好…」抓到的重复）。
    tail = prompt.rstrip()[-160:]
    assert "不要用「我们」开头或自称" in tail
    assert "不评价活动效果" in tail


def test_notice_normalize_keeps_only_简介():
    """模型多给的字段一律丢掉（模板只吃 活动简介）。"""
    out = llm._normalize("notice", {"活动简介": "  居民可现场学换季选菜。 ",
                                    "标题": "多余字段"})
    assert out == {"活动简介": "居民可现场学换季选菜。"}


def test_notice_normalize_rejects_over_max_chars():
    """超过 120 字在归一化阶段就打回（触发重试），不是先落盘再救。"""
    text = "居民可以到现场参加活动，学习实用的生活知识。" * 8      # 远超 120 字
    with pytest.raises(llm.LLMError, match="超过上限 120 字"):
        llm._normalize("notice", {"活动简介": text})


def test_notice_normalize_allows_100_chars():
    """100 字正好在区间内，不能被打回。"""
    text = "居民可现场学换季选菜。" * 10
    assert 100 <= llm._char_count(text) <= 120
    assert llm._normalize("notice", {"活动简介": text})["活动简介"] == text


def test_notice_normalize_strips_leading_comma():
    """build_data 会补「，」再接进正文段：模型自带的行首逗号必须去掉，
    否则生成「进行X活动，，居民可……」（2026-11-24 实测踩到）。"""
    out = llm._normalize("notice", {"活动简介": "，居民可带上旧物到现场交换。"})
    assert out["活动简介"] == "居民可带上旧物到现场交换。"
    out2 = llm._normalize("notice", {"活动简介": "、居民可到现场登记诉求。"})
    assert out2["活动简介"] == "居民可到现场登记诉求。"
    # 句中标点不动
    out3 = llm._normalize("notice", {"活动简介": "居民可到现场，登记诉求。"})
    assert out3["活动简介"] == "居民可到现场，登记诉求。"
