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
    assert '"导语"' in prompt and '"成效"' in prompt
    assert "全文合计 600~850 字" in prompt


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
    text = '```json\n{"导语": "一", "目的": "二", "过程": "三", "成效": "四"}\n```'
    assert llm._parse_json(text)["导语"] == "一"


def test_parse_json_tolerates_surrounding_text():
    text = '以下是结果：\n{"导语": "一"}\n希望有帮助'
    assert llm._parse_json(text) == {"导语": "一"}


def test_parse_json_rejects_non_json():
    with pytest.raises(llm.LLMError):
        llm._parse_json("模型什么都没说")


def test_write_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_chat(prompt, max_tokens=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise llm.LLMError("上游 500")
        return '{"导语": "一", "目的": "二", "过程": "三", "成效": "四"}'

    monkeypatch.setattr(llm, "_chat", fake_chat)
    result = llm.write("brief", _brief_facts(), style="务实", max_rewrite=0)
    assert calls["n"] == 3
    assert {k: v for k, v in result.items() if not k.startswith("_")} == {
        "导语": "一", "目的": "二", "过程": "三", "成效": "四"}


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
        '{"导语": "一", "目的": "二", "过程": "三", "成效": "四", "备注": "多余"}'))
    result = llm.write("brief", _brief_facts(), style="务实", max_rewrite=0)
    assert [key for key in result if not key.startswith("_")] == [
        "导语", "目的", "过程", "成效"]
