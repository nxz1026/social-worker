#!/usr/bin/env python3
"""统一 LLM 出口：文风解析 → prompt 拼装 → 调上游 → 解析 JSON。

本模块只依赖标准库和 yaml，不 import api 包（engine 不能依赖 FastAPI）。
LLM 的 endpoint / api_key / model 与 api/app.py 读同一份 config/api.yaml 的 llm 段。

任务用 TASKS 注册。目前有 brief（简报）、plan（计划书文字部分）、
summary（总结报告表格文字）与 supervision（监督记录的活动内容）。
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
from typing import Callable
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import projects

REPO_ROOT = Path(__file__).resolve().parent.parent
# 文风样例目录：yaml 里的 examples 路径都相对这里
STYLE_DIR = Path(__file__).resolve().parent / "llm_styles"

STYLE_CONFIG_FALLBACK = Path("/srv/social-work/config/llm_style.yaml")
API_CONFIG_FALLBACK = Path("/srv/social-work/config/api.yaml")

# 文风配置缺项时的兜底，避免 yaml 少写一个字段就崩
_DEFAULT_STYLE = "务实"

# 任务注册表。schema 即要求模型输出的 JSON 骨架，字段顺序就是段落顺序。
# lists 描述结构化字段：item 是每条必填的子字段，min 是条数下限（上限由模板行数决定，
# 在 engine/activity.py 展开时截断，不在 normalize 阶段拒绝）。
TASKS: dict[str, dict] = {
    "brief": {
        "doc": "社区活动简报",
        # 字段名必须与「简报.docx」的占位符一致，否则生成内容填不进模板
        "fields": ("简报导语", "诉求描述", "达到目标"),
        "schema": {
            "简报导语": "背景+目的合并，150~250 字：先说问题现状与谁有诉求，"
                        "再写「本次活动旨在以「XX主题」为主题，通过……，达成……，"
                        "回应……的诉求，同时对应项目……指标」",
            "诉求描述": "过程段，200~350 字：按环节顺序，用"
                        "「活动伊始……首先……其次……再者……最后……」串联",
            "达到目标": "成效段，100~180 字：以「通过此次活动开展……」开头，"
                        "讲清楚从什么变成什么",
        },
        "rules": (
            "只用上面给的事实，不编造人名、数字、政策文号",
            "不写「深入贯彻落实」这类开头（党建风除外，党建风按 extra 里的要求）",
            "全文合计 500~800 字",
        ),
    },
    "plan": {
        "doc": "社区活动计划书",
        "intro": "需要生成 6 段文字与 3 个列表（环节 / 预计困难 / 物资），"
                 "严格按下面骨架。只输出 JSON，不要任何解释：",
        "fields": ("活动背景内容", "活动目的内容", "活动目标内容", "目标人群",
                   "宣传方式", "筛选方式", "环节", "预计困难", "物资",
                   # 下面三个原本只从 activity.yaml 取，没填就留空 —— 计划书
                   # 因此到处是空行。用户 2026-11-23 要求：只要符合活动实际需求，
                   # 没指定的也先按合理值生成，用户再改或删。
                   "活动地点", "服务对象", "预计人数", "志愿者人数"),
        "schema": {
            "活动背景内容": "分三层意思写：问题现状 + 需求来源 + 回应思路，"
                            "150~250 字，不要小标题，段内不分行",
            "活动目的内容": "一句话目的，以「依据……开展……，达成……」这类句式，30~60 字",
            "活动目标内容": "3~5 条可核对的结果，写成「1. ……；2. ……；3. ……」，"
                            "分号连接，一条一句，不换行",
            "目标人群": "从服务对象与招募要求推导出具体人群，20~50 字",
            "活动地点": "合理的活动场地，10~25 字，如「社区党群服务中心一楼活动室」；"
                        "按活动类型给最常见也最稳妥的场地",
            "服务对象": "活动面向的人群，15~30 字，与目标人群一致但更口语，"
                        "如「辖区 55 岁以上老年居民、菜市经营户」",
            "预计人数": "合理的参与人数，只写数字，不带单位；按活动类型给常见规模"
                        "（社区小型活动 15~30 人）",
            "志愿者人数": "合理的志愿者人数，只写数字；按活动规模给 3~8 人量级",
            "宣传方式": "从通知渠道推导，列 2~4 种具体渠道，30~80 字",
            "筛选方式": "从对象范围推导招募与筛选口径，30~80 字",
            "环节": [
                {"阶段": "环节名，如 签到入场 / 开场致辞 / 分组行动 / 分享总结 / 合影离场",
                 "时间": "该环节的钟点或时长，如 9:00-9:15、约 20 分钟",
                 "内容": "该环节做什么，40~80 字",
                 "物资": "该环节所需物资，顿号分隔",
                 "人员": "该环节由谁负责，写岗位或姓名",
                 "备注": "补充说明（如「视天气调整」）；没有就留空"},
            ],
            "预计困难": [
                {"困难": "可能出现的困难，20~40 字",
                 "方案": "对应的可执行措施，30~60 字"},
            ],
            "物资": [
                {"类别": "只能是 广告宣传费 / 活动物资费 / 劳务费 / 培训物资费 之一",
                 "名称": "具体物品名称，如 横幅、KT板、矿泉水",
                 "规格": "数量 + 单位，如 5米、4个、2箱；开头的数字即数量",
                 "单价": "单个数量的价格（元），数字，不带单位"},
            ],
        },
        "lists": {
            # item 是「必须存在且非空」的子字段白名单；不在这里的会被丢弃。
            # 阶段/时间/备注是模板 {{环节阶段_N}} {{环节时间_N}} {{环节备注_N}} 要的，
            # 原来漏了 → 模型就算生成了也会被归一化丢掉（2026-11-23 实测）。
            "环节": {"item": ("阶段", "时间", "内容", "物资", "人员", "备注"),
                     "min": 3,
                     "optional": ("备注",)},
            "预计困难": {"item": ("困难", "方案"), "min": 2},
            # 预算为 0 的活动可以给空清单，所以下限是 0，条数由 rules 约束
            "物资": {"item": ("类别", "名称", "规格", "单价"), "min": 0},
        },
        "rules": (
            "只用上面给的事实，不编造人名、数字、政策文号；正文里不写物资单价与金额",
            "环节给 3~5 条，按活动从准备、执行到收尾的真实顺序排列；"
            "每条都要写：阶段（环节名，如签到入场/开场致辞/分组行动/分享总结）、"
            "时间（钟点或时长）、做什么、用什么物资、谁负责、备注（没有就留空）",
            "预计困难给 2~4 条，每条都要有能落地的解决措施，不写「加强沟通」这类空话",
            "活动目标 3~5 条，每条一个可核对的结果，不要写成口号",
            "背景段里的事由、诉求必须来自上面的事实，不能凭空造场景",
            "物资清单给 3~8 条，类别只能从「广告宣传费」「活动物资费」"
            "「劳务费」「培训物资费」里选；名称写具体物品，不写「活动物资」这类笼统说法",
            "物资的规格必须以数字开头并带单位（如 5米、4个、2箱），"
            "单价是单个数量的价格；单价×数量的合计不得超过事实里的「预算上限」；"
            "预算上限为空或为 0 时，按活动类型的常见市场价合理估算，不要留空"
            "（用户会自行删减，计划赶不上变化）",
            "上面「写作要求」里的「导语段 / 过程段 / 成效段」是简报的说法，"
            "在本文分别对应活动背景内容、环节、活动目标内容",
            "不出现「营造出……浓厚氛围」「打造……新格局」「双向奔赴」这类套话",
        ),
    },
    "summary": {
        "doc": "社区活动总结报告",
        "intro": "需要生成 19 个格子的文字，按「阶段 × 内容类型」排列。"
                 "严格按下面骨架，只输出 JSON，不要任何解释：",
        # 顺序即模板表 1 从上到下的行序
        "fields": (
            "策划目标", "策划问题", "策划行动",
            "准备目标", "准备问题", "准备行动",
            "执行目标", "执行内容", "执行表现", "执行挑战", "执行行动",
            "宣传目标", "宣传成效",
            "总结目标", "总结挑战", "总结行动",
            "工作员表现", "工作员反思",
            "跟进工作",
        ),
        "schema": {
            "策划目标": "活动策划阶段的具体目标：这次活动想解决什么、做到什么程度，40~80 字",
            "策划问题": "活动策划阶段所面临的问题：需求怎么摸清、方案怎么定、"
                        "资源与指标怎么对上，50~100 字",
            "策划行动": "活动策划阶段工作员的工作重点与专业行动：做了哪些调研、"
                        "协商、方案设计与报批，80~150 字",
            "准备目标": "活动准备阶段的具体目标：场地、人员、物资、通知要准备到什么状态，40~80 字",
            "准备问题": "活动准备阶段所面临的问题：通知到不到人、志愿者够不够、"
                        "物资与场地有什么变数，50~100 字",
            "准备行动": "活动准备阶段工作员的工作重点与专业行动：怎么通知招募、"
                        "怎么分工、怎么备物资与预案，80~150 字",
            "执行目标": "活动执行阶段的具体目标：现场要完成哪些动作、达到什么结果，40~80 字",
            "执行内容": "活动内容及合适度：先用一句话概括实际做了什么（不要逐环节复述），"
                        "再用一句话说明安排与对象需求、场地条件的匹配程度，60~120 字",
            "执行表现": "参加者表现：到场情况、参与动作、互动与配合程度，"
                        "有具体场景就写场景，60~120 字",
            "执行挑战": "活动执行阶段所面临的挑战与问题：现场出现的阻力、"
                        "突发状况与没做到位的地方，50~100 字",
            "执行行动": "活动执行阶段工作员的工作重点与专业行动：怎么分工、"
                        "怎么劝导协调、怎么处置突发，80~150 字",
            "宣传目标": "活动宣传的具体目标：想让哪些人知道、知道什么、"
                        "留下什么宣传材料，40~80 字",
            "宣传成效": "宣传成效：用了哪些渠道、覆盖了谁、形成了哪些可留存的材料，"
                        "60~120 字（这一段受文风影响）",
            "总结目标": "活动总结阶段的具体目标：资料、评估与归档要做到什么程度，40~80 字",
            "总结挑战": "活动总结阶段所面临的挑战与问题：资料收不齐、"
                        "成效难量化、遗留问题怎么交接，50~100 字",
            "总结行动": "活动总结阶段工作员的工作重点与专业行动：怎么整理资料、"
                        "怎么做评估、怎么形成结论与后续安排，80~150 字",
            "工作员表现": "工作员表现：按分工写每个人/每个岗位做了什么，"
                          "协作与临场处理如何，60~120 字",
            "工作员反思": "工作员反思：这次做得不够的地方、下次怎么改，"
                          "60~120 字（这一段受文风影响）",
            "跟进工作": "跟进工作或其他：活动后要持续做的事、责任人与节奏，60~120 字",
        },
        # 总结报告整体平实陈述，文风只作用于这两段
        "style_scope": ("工作员反思", "宣传成效"),
        "rules": (
            "只用上面给的事实，不编造人名、数字、政策文号；细节不确定时用概括说法",
            "19 个字段都要写，不能空，不能把两个阶段合并成一段",
            "每个字段严格按骨架标注的字数区间写，写完逐条自检：超上限必须删到上限内，"
            "低于下限补一个具体细节；宁可写短，不要超长",
            "按「策划—准备—执行—宣传—总结」的时间顺序写，前后事实要能对上",
            "目标写活动前定下的可核对结果，不要写成事后的口号或升华",
            "问题与挑战写具体表现和原因，行动写谁做了什么、结果如何，不写「加强沟通」这类空话",
            "除「工作员反思」「宣传成效」外，其余字段按事实陈述，不用文风句式",
            "不出现「营造出……浓厚氛围」「打造……新格局」「双向奔赴」这类套话",
        ),
    },
    # 监督记录要交街道办监委会审阅签字，比简报正式：只写一段完整叙述
    "supervision": {
        "doc": "监督记录表",
        "fields": ("活动内容",),
        "schema": {
            "活动内容": "一段 300~450 字的正式叙述：活动过程（按环节）、取得的收获、"
                        "发现的问题与整改方向。用于街道办监委会审阅签字。",
        },
        "rules": (
            "以「年月日，某社区联合某机构在何处开展某事」开头",
            "过程按环节顺序，用「首先/其次/再次/最后」串联",
            "必须写具体数字（人数、金额、数量）",
            "问题要写真实发现的问题，不写套话",
            "不编造未提供的事实",
        ),
    },
}


class LLMError(RuntimeError):
    """LLM 调用失败或返回内容不可用（已含上游返回体，便于排查）。"""


# --------------------------------------------------------------------------
# 配置定位
# --------------------------------------------------------------------------

def _style_config_path(path: Path | str | None = None) -> Path:
    """文风配置查找顺序：显式入参 → 环境变量 → 仓库 config → 部署根 config。"""
    if path:
        return Path(path).expanduser()
    env = os.environ.get("SOCIAL_WORK_LLM_STYLE")
    if env:
        return Path(env).expanduser()
    for candidate in (REPO_ROOT / "config" / "llm_style.yaml", STYLE_CONFIG_FALLBACK):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "找不到文风配置：设置 SOCIAL_WORK_LLM_STYLE，或放置 "
        f"{REPO_ROOT / 'config' / 'llm_style.yaml'}")


def load_style_config(path: Path | None = None) -> dict:
    """读 config/llm_style.yaml，返回 {default, styles, type_style}。

    styles 里的 examples 仍是相对 STYLE_DIR 的路径字符串，读取样例在 build_prompt 里做，
    这样配置文件本身可以原样看、原样改。
    """
    config_path = _style_config_path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"文风配置解析失败：{config_path}：{exc}")
    if not isinstance(raw, dict):
        raise ValueError(f"文风配置根节点不是映射：{config_path}")

    styles: dict[str, dict] = {}
    for name, spec in (raw.get("styles") or {}).items():
        if not isinstance(spec, dict):
            continue
        examples = spec.get("examples") or []
        if isinstance(examples, str):
            examples = [examples]
        check = spec.get("check")
        styles[str(name)] = {
            "desc": str(spec.get("desc") or "").strip(),
            "examples": [str(item) for item in examples],
            "extra": str(spec.get("extra") or "").strip(),
            # 生成后的规则自检配置（required_any / required_all / forbidden /
            # min_words / max_words）；没写就是不自检
            "check": check if isinstance(check, dict) else {},
            # 文风要求覆盖段落骨架的开关，build_prompt 里用
            "overwrite_skeleton": bool(spec.get("overwrite_skeleton")),
        }

    type_style = {
        str(key): str(value)
        for key, value in (raw.get("type_style") or {}).items()
        if value
    }
    default = str(raw.get("default") or "").strip() or _DEFAULT_STYLE
    if default not in styles and styles:
        # default 写了个不存在的名字：退回第一个文风，别让调用方拿到空值
        default = next(iter(styles))
    return {"path": str(config_path), "default": default,
            "styles": styles, "type_style": type_style}


def _activity_type(activity_name: str) -> str:
    """活动名 → 活动类型：去掉目录编号与「-第X期」后缀。

    「11-共治行动-第一期」→「共治行动」；
    「惠民共治行动——民乐北巷…」保持原样，靠子串匹配落到「共治行动」。
    """
    raw = re.sub(r"^\d+\s*[-_]", "", str(activity_name or "").strip())
    return re.sub(r"\s*[-_]第[一二三四五六七八九十百\d]+期$", "", raw).strip()


def _project_default_style(project_name: str) -> str:
    """项目 yaml 的 llm_style.default；没配或读不到就返回空串。"""
    if not project_name:
        return ""
    try:
        data = projects.load_project(project_name)
    except (FileNotFoundError, ValueError):
        return ""
    spec = data.get("llm_style")
    if not isinstance(spec, dict):
        return ""
    return str(spec.get("default") or "").strip()


def resolve_style(project_name: str, activity_name: str,
                  explicit: str = "") -> str:
    """定文风。优先级：explicit > 项目 yaml llm_style.default
    > config 的 type_style[活动类型] > config 的 default。
    """
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit

    config = load_style_config()
    project_style = _project_default_style(project_name)
    if project_style:
        return project_style

    act_type = _activity_type(activity_name)
    for key, style in config["type_style"].items():
        if key and key in act_type:
            return style
    return config["default"]


# --------------------------------------------------------------------------
# prompt 拼装
# --------------------------------------------------------------------------

def _load_examples(paths: list[str]) -> list[str]:
    """按 yaml 给的相对路径读样例正文；缺文件只警告，不中断。"""
    texts: list[str] = []
    for rel in paths:
        path = STYLE_DIR / rel
        if not path.is_file():
            print(f"警告：文风样例缺失 {path}", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            texts.append(text)
    return texts


def _facts_text(facts: dict) -> str:
    """facts → 「- 键：值」列表；列表值用「、」连接，空值跳过。"""
    lines: list[str] = []
    for key, value in (facts or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = "、".join(str(item).strip() for item in value
                              if str(item).strip())
        if isinstance(value, float) and value.is_integer():
            value = int(value)                 # 1662.0 → 1662，别让模型照抄小数
        text = str(value).strip()
        if not text:
            continue
        lines.append(f"- {key}：{text}")
    return "\n".join(lines)


def _task_spec(task: str) -> dict:
    """取任务定义；未注册的任务直接报错，避免拼出空骨架。"""
    spec = TASKS.get(str(task or "").strip())
    if spec is None:
        raise ValueError(f"未知任务：{task}；已注册：{'、'.join(sorted(TASKS))}")
    return spec


def build_prompt(task: str, facts: dict, style: str,
                 style_config: dict | None = None,
                 context_block: str = "") -> str:
    """三层拼装 prompt：角色 → 文风与样例 → 本次内容与输出骨架。

    context_block 是「冷化材料」（项目书/协议/知识库片段），插在事实之后、
    骨架之前，让模型有据可依。为空时行为与原来完全一致。
    """
    spec = _task_spec(task)
    config = style_config if style_config is not None else load_style_config()
    style_spec = (config.get("styles") or {}).get(style)
    if style_spec is None:
        available = "、".join(sorted(config.get("styles") or {}))
        raise ValueError(f"未知文风：{style}；可用：{available}")

    examples = _load_examples(style_spec["examples"])
    blocks: list[str] = [
        "【角色】",
        f"你是成都市益嘉仁社会工作服务中心的项目社工，负责写{spec['doc']}。",
        "机构写作习惯：用具体动作和数字说话，不写空话。",
        "",
        f"【文风：{style}】",
        style_spec["desc"],
        "",
        "写作要求：",
        style_spec["extra"],
        "",
    ]
    # 部分任务只允许文风作用于少数段落（如总结报告），其余字段必须按事实陈述
    scope = spec.get("style_scope")
    if scope:
        blocks += [
            "【文风适用范围】",
            f"本次文风只作用于：{'、'.join(scope)}。",
            "其余字段一律按事实陈述，不使用该文风的句式、套话与升华句。",
            "",
        ]
    blocks.append("【参考样例】（模仿其结构、节奏与用词，不要照抄内容）")
    for seq, text in enumerate(examples, start=1):
        blocks += [f"--- 样例 {seq} ---", text]
    if not examples:
        blocks.append("（该文风暂无样例，按上面的写作要求写）")

    blocks += [
        "",
        "【本次要写的内容】",
        "活动基本信息：",
        _facts_text(facts) or "- （未提供）",
        "",
    ]
    # 冷化材料（项目书/协议/知识库）：有才插，没有就不占 prompt
    if context_block.strip():
        blocks += [context_block.strip(), ""]
    blocks += [
        spec.get("intro") or (f"需要生成 {len(spec['fields'])} 段，"
                              "严格按下面骨架。只输出 JSON，不要任何解释："),
        "",
        json.dumps(spec["schema"], ensure_ascii=False, indent=2),
        "",
        "硬性要求：",
    ]
    blocks += [f"- {rule}" for rule in spec["rules"]]
    # 文风要求覆盖骨架时：把「禁止…开头」这类与文风冲突的规则压掉，
    # 并把 extra 里的硬性要求在末尾再强调一次（长上下文中末位权重更高）
    if style_spec.get("overwrite_skeleton"):
        blocks = [b for b in blocks
                  if not (b.startswith("- ") and "深入贯彻落实" in b and "不写" in b)]
        blocks += [
            "",
            "【再次强调：上面「写作要求」中标注为硬性要求的句式，优先级高于段落骨架。】",
            "凡与骨架措辞冲突处，一律以写作要求为准。",
        ]
    return "\n".join(blocks)


# --------------------------------------------------------------------------
# 文风自检（纯规则判断，不调 LLM）
# --------------------------------------------------------------------------

# 每命中一类问题扣的分；分数只用于展示，ok 只看有没有问题
_PENALTY_REQUIRED_ALL = 20
_PENALTY_REQUIRED_ANY = 20
_PENALTY_FORBIDDEN = 15
_PENALTY_WORDS = 15

# 字数口径：中文字符 + 英文单词
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")


def _collect_text(value) -> str:
    """把结果里所有字符串拼成一段文本：列表展开、字典展开、跳过 _ 开头的内部键。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_collect_text(item) for key, item in value.items()
                         if not str(key).startswith("_"))
    if isinstance(value, (list, tuple)):
        return "\n".join(_collect_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _word_count(text: str) -> int:
    """字数：中文字符数 + 英文单词数（数字与标点不计）。"""
    return len(_CJK_RE.findall(text)) + len(_WORD_RE.findall(text))


def self_check(task: str, result: dict, style: str,
               style_config: dict | None = None) -> dict:
    """检查生成结果是否符合文风要求。

    返回 {
      "ok": bool,
      "score": int,            # 0~100
      "hits": {"required_all": [...命中...], "required_any": [...命中...]},
      "miss": {"required_all": [...缺...], "required_any": bool, "forbidden": [...命中违禁词...]},
      "words": int,
      "issues": [str, ...]     # 人读的问题列表
    }

    统计口径：把 result 里所有字符串值拼起来算（列表也要展开）。
    字数按中文字符 + 英文单词计。
    带 style_scope 的任务（如总结报告）文风只作用于少数段落，因此只统计那几段，
    也不做整篇字数判定——文风的字数区间是给整篇简报写的。
    """
    config = style_config if style_config is not None else load_style_config()
    style_spec = (config.get("styles") or {}).get(str(style)) or {}
    check = style_spec.get("check") or {}

    scope = (_task_spec(task).get("style_scope")
             if str(task or "") in TASKS else None)
    if scope:
        text = _collect_text({field: (result or {}).get(field) for field in scope})
        check_words = False
    else:
        text = _collect_text(result or {})
        check_words = True
    words = _word_count(text)

    required_any = [str(item) for item in check.get("required_any") or []]
    required_all = [str(item) for item in check.get("required_all") or []]
    forbidden = [str(item) for item in check.get("forbidden") or []]
    min_words = check.get("min_words")
    max_words = check.get("max_words")

    hit_all = [item for item in required_all if item in text]
    hit_any = [item for item in required_any if item in text]
    miss_all = [item for item in required_all if item not in text]
    hit_forbidden = [item for item in forbidden if item in text]
    # 配了 required_any 却一个都没命中，才算「缺」
    any_missed = bool(required_any) and not hit_any

    issues: list[str] = []
    score = 100
    for item in miss_all:
        issues.append(f"缺少必需短语：{item}")
        score -= _PENALTY_REQUIRED_ALL
    if any_missed:
        issues.append("缺少开头句式：至少命中一个（" + " / ".join(required_any) + "）")
        score -= _PENALTY_REQUIRED_ANY
    for item in hit_forbidden:
        issues.append(f"出现违禁词：{item}")
        score -= _PENALTY_FORBIDDEN
    if check_words and min_words and words < int(min_words):
        issues.append(f"字数 {words}，低于下限 {min_words}")
        score -= _PENALTY_WORDS
    elif check_words and max_words and words > int(max_words):
        issues.append(f"字数 {words}，超过上限 {max_words}")
        score -= _PENALTY_WORDS

    return {
        "ok": not issues,
        "score": max(0, min(100, score)),
        "hits": {"required_all": hit_all, "required_any": hit_any},
        "miss": {"required_all": miss_all, "required_any": any_missed,
                 "forbidden": hit_forbidden},
        "words": words,
        "issues": issues,
    }


def _rewrite_note(check: dict) -> str:
    """自检结论 → 追加在原 prompt 末尾的重写说明。"""
    lines = ["【上一次生成不合格，请修正后重新生成】", "未满足的项："]
    lines += [f"- {issue}" for issue in check.get("issues") or []]
    lines.append("请严格按文风要求重写，其余内容保持不变。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 上游调用
# --------------------------------------------------------------------------

def _api_config() -> dict:
    """读 config/api.yaml；找不到就返回空 dict（后面按未配置处理）。"""
    env = os.environ.get("SOCIALWORK_API_CONFIG")
    candidates = [Path(env).expanduser()] if env else [
        REPO_ROOT / "config" / "api.yaml", API_CONFIG_FALLBACK]
    for path in candidates:
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return data if isinstance(data, dict) else {}
    return {}


def _llm_settings() -> dict:
    """llm 段 + 环境变量覆盖。环境变量优先，与 api/app.py 一致。"""
    llm = _api_config().get("llm")
    if not isinstance(llm, dict):
        llm = {}

    def pick(key: str, *env_names: str) -> str:
        for name in env_names:
            value = os.environ.get(name)
            if value:
                return value
        return str(llm.get(key) or "")

    return {
        "base_url": pick("base_url", "LLM_BASE_URL", "LLM_ENDPOINT"),
        "api_key": pick("api_key", "LLM_API_KEY"),
        "model_id": pick("model_id", "LLM_MODEL_ID", "LLM_MODEL").split("/")[-1],
        "max_tokens": int(llm.get("max_tokens") or 16000),
        "timeout_seconds": int(llm.get("timeout_seconds") or 240),
        "temperature": float(llm.get("temperature") if llm.get("temperature") is not None else 0.3),
    }


def _chat(prompt: str, max_tokens: int | None = None) -> str:
    """调上游 /chat/completions，返回正文。

    与 api/app.py 的 llm_chat 同一套参数来源，但去掉 FastAPI 依赖。
    失败一律抛 LLMError，并把上游返回体带进异常消息。
    """
    settings = _llm_settings()
    if not (settings["base_url"] and settings["api_key"] and settings["model_id"]):
        raise LLMError(
            "LLM 未配置：在 config/api.yaml 的 llm 段填 base_url / api_key / model_id，"
            "或设置环境变量 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL_ID")

    body = {
        "model": settings["model_id"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": settings["temperature"],
        "max_tokens": max_tokens or settings["max_tokens"],
    }
    request = urllib.request.Request(
        settings["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {settings['api_key']}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(
                request, timeout=settings["timeout_seconds"]) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMError(f"上游返回 HTTP {exc.code}：{detail[:2000]}") from exc
    except Exception as exc:                      # 含 socket.timeout / URLError
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout) or isinstance(exc, TimeoutError):
            raise LLMError(f"上游超时：{exc}") from exc
        raise LLMError(f"调用上游失败：{exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"上游返回不是 JSON：{raw[:2000]}") from exc

    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if not content.strip():
        raise LLMError(f"上游没有返回正文（finish_reason="
                       f"{choice.get('finish_reason')}）：{raw[:2000]}")
    return content


def _parse_json(text: str) -> dict:
    """从模型输出里取出 JSON 对象：容忍 ```json 围栏和前后多余说明。"""
    cleaned = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"模型输出里找不到 JSON：{text[:500]}")
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"JSON 解析失败：{exc}；原文：{text[:500]}") from exc
    if not isinstance(data, dict):
        raise LLMError(f"模型输出的 JSON 不是对象：{text[:500]}")
    return data


def _normalize_rows(field: str, value, spec: dict) -> list[dict]:
    """校验结构化字段（环节 / 预计困难）：必须是对象数组，每条子字段非空。

    条数只校验下限，上限交给模板展开阶段截断——模型多给一条不该整份重写。
    """
    if not isinstance(value, list):
        raise LLMError(f"字段「{field}」应为数组，实际是 {type(value).__name__}")
    if len(value) < spec["min"]:
        raise LLMError(f"字段「{field}」至少 {spec['min']} 条，实际 {len(value)} 条")

    rows: list[dict] = []
    for seq, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise LLMError(f"「{field}」第 {seq} 条应为对象，实际是 {type(item).__name__}")
        row: dict[str, str] = {}
        optional = set(spec.get("optional") or ())
        for key in spec["item"]:
            raw = item.get(key)
            text = str(raw).strip() if raw is not None else ""
            if not text and key not in optional:
                raise LLMError(f"「{field}」第 {seq} 条缺「{key}」")
            row[key] = text
        rows.append(row)
    return rows


def _normalize(task: str, data: dict) -> dict:
    """只保留任务骨架里的字段，按骨架顺序返回；缺字段抛 LLMError 触发重试。

    结构化字段（spec["lists"] 里声明的）返回对象数组，其余字段返回去空白后的字符串。
    """
    spec = _task_spec(task)
    lists = spec.get("lists") or {}
    out: dict = {}
    missing: list[str] = []
    for field in spec["fields"]:
        if field in lists:
            out[field] = _normalize_rows(field, data.get(field), lists[field])
            continue
        value = data.get(field)
        text = str(value).strip() if value is not None else ""
        if not text:
            missing.append(field)
        out[field] = text
    if missing:
        raise LLMError(f"模型输出缺字段：{'、'.join(missing)}")
    return out


def _generate(task: str, prompt: str) -> dict:
    """调一次 LLM 并归一化；网络 / 解析失败重试 2 次（共 3 次），仍失败抛 LLMError。"""
    attempts = 3                           # 首次 + 2 次重试
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            content = _chat(prompt)
            return _normalize(task, _parse_json(content))
        except LLMError as exc:
            last_error = exc
            print(f"LLM 第 {attempt}/{attempts} 次失败：{exc}", file=sys.stderr)
    raise LLMError(f"{task} 生成失败，已重试 {attempts - 1} 次：{last_error}")


def write(task: str, facts: dict, *, style: str = "",
          style_config: dict | None = None,
          max_rewrite: int = 1,
          on_progress: "Callable[[str, int], None] | None" = None,
          context_block: str = "") -> dict:
    """生成 → 自检 → 不达标则带自检意见重写 → 再自检。

    返回字段 dict，另带两个内部键（调用方 fill 时会忽略以 _ 开头的键）：
    "_check" 自检结果，"_rewrites" 实际重写次数。
    style 留空时按 facts 里的项目名称 / 活动名称自动定文风。
    """
    _task_spec(task)                       # 未知任务尽早报错，不浪费调用
    config = style_config if style_config is not None else load_style_config()
    chosen = str(style or "").strip() or resolve_style(
        str(facts.get("项目名称") or ""), str(facts.get("活动名称") or ""))
    prompt = build_prompt(task, facts, chosen, style_config=config,
                          context_block=context_block)

    # 进度回调：一次 LLM 调用实测 50~100 秒，前端需要知道「还活着」。
    # stage: generating / checking / rewriting / done
    def _emit(stage: str, percent: int) -> None:
        if on_progress is None:
            return
        try:
            on_progress(stage, percent)
        except Exception:            # 进度上报失败绝不能影响生成
            pass

    _emit("generating", 10)
    result = _generate(task, prompt)
    _emit("checking", 55)
    check = self_check(task, result, chosen, style_config=config)

    rewrites = 0
    limit = max(0, int(max_rewrite or 0))
    while not check["ok"] and rewrites < limit:
        rewrites += 1
        _emit("rewriting", 60 + rewrites * 10)
        try:
            rewritten = _generate(task, prompt + "\n\n" + _rewrite_note(check))
        except LLMError as exc:
            # 重写失败不该把已经生成好的结果丢掉，保留原稿并在 stderr 说明
            print(f"自检重写失败，保留上一次结果：{exc}", file=sys.stderr)
            break
        result = rewritten
        _emit("checking", 80)
        check = self_check(task, result, chosen, style_config=config)
        print(f"文风自检第 {rewrites} 次重写后：{check['score']} 分，"
              f"{'通过' if check['ok'] else '仍未通过'}", file=sys.stderr)

    _emit("done", 100)
    out = dict(result)
    out["_check"] = check
    out["_rewrites"] = rewrites
    return out
