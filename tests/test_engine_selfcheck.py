"""engine/llm.py 的文风自检与自动重写。

不调真实 LLM：_chat 一律 monkeypatch 成假函数。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import fill, llm  # noqa: E402

# 党建风样例：命中 required_any 的「为深入贯彻落实」，
# 且含 required_all 的四个短语
_DJ_BASE = (
    "为深入贯彻落实街道党工委关于社区治理的工作部署，惠民社区联同益嘉仁社工机构开展共治行动。"
    "活动前期，社工走访居民32户，梳理出通道堵塞、车辆乱停两类诉求。"
    "活动当天，20名居民与5名志愿者分为两组，清理占道车辆18辆，划定临时停放区2处。"
    "活动在社区内营造出人人参与、共建共享的浓厚氛围，进一步夯实了院落治理的群众基础。"
    "通过此次活动开展，通道从拥堵变为通畅，居民从观望变为主动参与，"
    "社区治理的抓手更加清晰，切实增强了居民的获得感、幸福感、安全感。"
    "下一步，社区将持续以院落议事为抓手，把整治成果固化为常态机制。"
)
_DJ_FILLER = "社工在现场逐户说明整治方案，记录居民意见并当场回应。"

# 务实风样例：不含任何违禁词
_WU_BASE = (
    "民乐北巷的通道长期被乱停车辆占去一半，居民推婴儿车要侧身过，这是本次共治行动要解决的问题。"
    "11月10日上午9点，社工与20名居民、5名志愿者在巷口集合，先按楼栋分组，再逐段清理。"
    "第一组从巷口往里推，登记车主电话，现场挪走12辆；第二组用白漆划出2处临时停放区，"
    "每处6个车位，划完当场有居民把车停进去。"
    "活动结束时通道宽度从1.2米恢复到2.6米，居民反映推车不再侧身。"
    "后续由院落自管小组每周巡查一次，发现乱停先贴提示再联系车主。"
)
_WU_FILLER = "社工把每辆车的车牌与车主电话记在本子上，方便第二天回访。"


def _pad(text: str, filler: str, total: int) -> str:
    """补到至少 total 字（按 llm 的字数口径），用于测字数上下限。"""
    while llm._word_count(text) < total:
        text += filler
    return text


def _brief(text: str) -> dict:
    """把一段文字放进「导语」，其余字段给短句，总字数基本等于 text 的字数。"""
    return {"简报导语": text, "诉求描述": "补充说明。",
            "达到目标": "补充说明。"}


def _style_config() -> dict:
    return llm.load_style_config()


# --------------------------------------------------------------------------
# self_check：规则判断
# --------------------------------------------------------------------------

def test_self_check_passes_with_all_markers():
    text = _pad(_DJ_BASE, _DJ_FILLER, 600)
    check = llm.self_check("brief", _brief(text), "党建", _style_config())

    assert check["ok"] is True, check["issues"]
    assert check["score"] == 100
    assert check["miss"]["required_all"] == []
    assert check["miss"]["required_any"] is False
    assert check["miss"]["forbidden"] == []
    assert "获得感、幸福感、安全感" in check["hits"]["required_all"]
    assert "为深入贯彻落实" in check["hits"]["required_any"]
    assert 550 <= check["words"] <= 900


def test_self_check_reports_missing_required_all():
    text = _pad(_DJ_BASE.replace("获得感、幸福感、安全感", "安全感"), _DJ_FILLER, 600)
    check = llm.self_check("brief", _brief(text), "党建", _style_config())

    assert check["ok"] is False
    assert "获得感、幸福感、安全感" in check["miss"]["required_all"]
    assert check["score"] < 100
    assert any("获得感、幸福感、安全感" in issue for issue in check["issues"])


def test_self_check_flags_forbidden_word():
    text = _pad(_WU_BASE + "这场整治成了社区与居民的双向奔赴。", _WU_FILLER, 600)
    check = llm.self_check("brief", _brief(text), "务实", _style_config())

    assert check["ok"] is False
    assert "双向奔赴" in check["miss"]["forbidden"]
    assert any("双向奔赴" in issue for issue in check["issues"])


def test_self_check_rejects_over_max_words():
    text = _pad(_DJ_BASE, _DJ_FILLER, 950)
    check = llm.self_check("brief", _brief(text), "党建", _style_config())

    assert check["words"] > 900
    assert check["ok"] is False
    assert any("超过上限" in issue for issue in check["issues"])


def test_self_check_ignores_underscore_keys():
    """_check 这类内部键不计入字数与命中统计。"""
    text = _pad(_WU_BASE, _WU_FILLER, 600)
    plain = llm.self_check("brief", _brief(text), "务实", _style_config())
    dirty = llm.self_check("brief", {**_brief(text), "_check": "浓厚氛围" * 50},
                           "务实", _style_config())

    assert dirty["words"] == plain["words"]
    assert dirty["miss"]["forbidden"] == []


def test_self_check_uses_style_scope_for_summary():
    """总结报告只有两段受文风影响：只查这两段，也不按整篇判字数。"""
    result = {field: "按事实陈述的一段内容。" * 3
              for field in llm.TASKS["summary"]["fields"]}
    result["工作员反思"] = _pad(_WU_BASE, _WU_FILLER, 600)

    check = llm.self_check("summary", result, "务实", _style_config())
    assert check["ok"] is True, check["issues"]
    assert check["words"] == llm._word_count(
        result["工作员反思"] + "\n" + result["宣传成效"])


# --------------------------------------------------------------------------
# write：生成 → 自检 → 重写
# --------------------------------------------------------------------------

def _facts() -> dict:
    return {"活动名称": "共治行动（第一期）", "活动地点": "民乐北巷",
            "项目名称": "亲邻惠里项目"}


def test_write_skips_rewrite_when_max_rewrite_zero(monkeypatch):
    calls = {"n": 0}
    short = "社工组织居民清理通道。"          # 远低于 550 字下限

    def fake_chat(prompt, max_tokens=None):
        calls["n"] += 1
        return json.dumps(_brief(short), ensure_ascii=False)

    monkeypatch.setattr(llm, "_chat", fake_chat)
    result = llm.write("brief", _facts(), style="务实", max_rewrite=0)

    assert calls["n"] == 1                  # 不重写就不多调一次 LLM
    assert result["_rewrites"] == 0
    assert result["_check"]["ok"] is False
    assert result["简报导语"] == short


def test_write_rewrites_once_when_check_fails(monkeypatch):
    calls = {"n": 0}
    prompts: list[str] = []
    short = "社工组织居民清理通道。"
    good = _pad(_WU_BASE, _WU_FILLER, 600)

    def fake_chat(prompt, max_tokens=None):
        calls["n"] += 1
        prompts.append(prompt)
        text = short if calls["n"] == 1 else good
        return json.dumps(_brief(text), ensure_ascii=False)

    monkeypatch.setattr(llm, "_chat", fake_chat)
    result = llm.write("brief", _facts(), style="务实", max_rewrite=1)

    assert calls["n"] == 2
    assert result["_rewrites"] == 1
    assert result["_check"]["ok"] is True
    # 重写 prompt 带上了自检意见
    assert "【上一次生成不合格，请修正后重新生成】" in prompts[1]
    assert "低于下限 550" in prompts[1]
    assert "请严格按文风要求重写，其余内容保持不变。" in prompts[1]


def test_write_keeps_original_when_rewrite_fails(monkeypatch):
    calls = {"n": 0}
    short = "社工组织居民清理通道。"

    def fake_chat(prompt, max_tokens=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps(_brief(short), ensure_ascii=False)
        raise llm.LLMError("上游 500")

    monkeypatch.setattr(llm, "_chat", fake_chat)
    result = llm.write("brief", _facts(), style="务实", max_rewrite=1)

    # 首次 1 次 + 重写的 3 次网络重试，全部失败后保留原稿
    assert calls["n"] == 4
    assert result["简报导语"] == short
    assert result["_check"]["ok"] is False


# --------------------------------------------------------------------------
# fill：忽略 _ 开头的键
# --------------------------------------------------------------------------

def test_render_docx_ignores_underscore_keys(tmp_path):
    src = tmp_path / "tpl.docx"
    doc = Document()
    doc.add_paragraph("{{项目名称}}|{{_check}}")
    doc.save(str(src))

    dst = tmp_path / "out.docx"
    stats = fill.render_docx(src, dst, {"项目名称": "测试项目",
                                        "_check": {"ok": True}})

    text = "\n".join(para.text for para in Document(str(dst)).paragraphs)
    assert "测试项目" in text
    assert "{{_check}}" in text             # 内部键不参与替换
    assert stats["missing"] == ["_check"]
