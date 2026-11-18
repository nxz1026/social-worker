#!/usr/bin/env python3
"""统一 LLM 出口：文风解析 → prompt 拼装 → 调上游 → 解析 JSON。

本模块只依赖标准库和 yaml，不 import api 包（engine 不能依赖 FastAPI）。
LLM 的 endpoint / api_key / model 与 api/app.py 读同一份 config/api.yaml 的 llm 段。

任务用 TASKS 注册。目前有 brief（简报）与 plan（计划书文字部分）；summary 后续照同样结构追加即可。
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
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
        "fields": ("导语", "目的", "过程", "成效"),
        "schema": {
            "导语": "背景段：先说问题现状与谁有诉求，60~120 字",
            "目的": "以「本次活动旨在以「XX主题」为主题，通过……，达成……，"
                    "回应……的诉求，同时对应项目……指标」句式，80~150 字",
            "过程": "按环节顺序叙述，用「活动伊始……首先……其次……再者……最后……」"
                    "串联，200~350 字",
            "成效": "以「通过此次活动开展……」开头，讲清楚从什么变成什么，80~150 字",
        },
        "rules": (
            "只用上面给的事实，不编造人名、数字、政策文号",
            "不写「深入贯彻落实」这类开头（党建风除外，党建风按 extra 里的要求）",
            "全文合计 600~850 字",
        ),
    },
    "plan": {
        "doc": "社区活动计划书",
        "intro": "需要生成 6 段文字与 2 个列表，严格按下面骨架。"
                 "只输出 JSON，不要任何解释：",
        "fields": ("活动背景内容", "活动目的内容", "活动目标内容", "目标人群",
                   "宣传方式", "筛选方式", "环节", "预计困难"),
        "schema": {
            "活动背景内容": "分三层意思写：问题现状 + 需求来源 + 回应思路，"
                            "150~250 字，不要小标题，段内不分行",
            "活动目的内容": "一句话目的，以「依据……开展……，达成……」这类句式，30~60 字",
            "活动目标内容": "3~5 条可核对的结果，写成「1. ……；2. ……；3. ……」，"
                            "分号连接，一条一句，不换行",
            "目标人群": "从服务对象与招募要求推导出具体人群，20~50 字",
            "宣传方式": "从通知渠道推导，列 2~4 种具体渠道，30~80 字",
            "筛选方式": "从对象范围推导招募与筛选口径，30~80 字",
            "环节": [
                {"内容": "该环节做什么，40~80 字",
                 "物资": "该环节所需物资，顿号分隔",
                 "人员": "该环节由谁负责，写岗位或姓名"},
            ],
            "预计困难": [
                {"困难": "可能出现的困难，20~40 字",
                 "方案": "对应的可执行措施，30~60 字"},
            ],
        },
        "lists": {
            "环节": {"item": ("内容", "物资", "人员"), "min": 2},
            "预计困难": {"item": ("困难", "方案"), "min": 2},
        },
        "rules": (
            "只用上面给的事实，不编造人名、数字、政策文号，不写物资单价与金额",
            "环节给 2~5 条，按活动从准备、执行到收尾的真实顺序排列；"
            "每条都写清做什么、用什么物资、谁负责",
            "预计困难给 2~4 条，每条都要有能落地的解决措施，不写「加强沟通」这类空话",
            "活动目标 3~5 条，每条一个可核对的结果，不要写成口号",
            "背景段里的事由、诉求必须来自上面的事实，不能凭空造场景",
            "上面「写作要求」里的「导语段 / 过程段 / 成效段」是简报的说法，"
            "在本文分别对应活动背景内容、环节、活动目标内容",
            "不出现「营造出……浓厚氛围」「打造……新格局」「双向奔赴」这类套话",
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
        styles[str(name)] = {
            "desc": str(spec.get("desc") or "").strip(),
            "examples": [str(item) for item in examples],
            "extra": str(spec.get("extra") or "").strip(),
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
                 style_config: dict | None = None) -> str:
    """三层拼装 prompt：角色 → 文风与样例 → 本次内容与输出骨架。"""
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
        "【参考样例】（模仿其结构、节奏与用词，不要照抄内容）",
    ]
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
        for key in spec["item"]:
            raw = item.get(key)
            text = str(raw).strip() if raw is not None else ""
            if not text:
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


def write(task: str, facts: dict, *, style: str = "",
          style_config: dict | None = None) -> dict:
    """调 LLM 生成指定任务的字段 dict。失败重试 2 次（共 3 次），仍失败抛 LLMError。

    style 留空时按 facts 里的项目名称 / 活动名称自动定文风。
    """
    _task_spec(task)                       # 未知任务尽早报错，不浪费调用
    config = style_config if style_config is not None else load_style_config()
    chosen = str(style or "").strip() or resolve_style(
        str(facts.get("项目名称") or ""), str(facts.get("活动名称") or ""))
    prompt = build_prompt(task, facts, chosen, style_config=config)

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
