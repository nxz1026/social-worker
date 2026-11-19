#!/usr/bin/env python3
"""图注：程序挑图 + LLM 写注。

程序只按文件名挑图（哪张更像横幅 / 合影 / 发放现场），LLM 只拿到文件名与活动
事实，看不到图本身，所以 prompt 里明确要求「不确定就写通用描述，不许瞎猜细节」。

生成结果可以直接交给 engine.fill 填 {{图注_1}}~{{图注_8}}：

    captions = write_captions(pick_photos(act_dir), facts)
    fill.render_docx(模板, 输出, as_fill_data(captions))
"""
from __future__ import annotations

import re
from pathlib import Path

from . import llm

# 活动照片目录
PHOTO_SUBDIR = "11-活动照片资料"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# 规范命中词：名字里带这些词的优先（顺序即优先级，全景 / 合影这类更适合做首图）
KEYWORDS = ("全景", "合影", "横幅", "签到", "议事", "发放", "展示", "特写")
# 微信默认名（微信图片_20261110131250_175_11.jpg）：没改过名，排在后面
WECHAT_PREFIX = "微信图片"


def _is_image(path: Path) -> bool:
    """只收常见图片格式，跳过隐藏文件。"""
    return (path.is_file() and not path.name.startswith(".")
            and path.suffix.lower() in IMAGE_EXTS)


def _rank(path: Path) -> tuple:
    """挑图排序键：命中词 > 非微信名 > 命中词先后 > 文件名。

    第一段 0/1 表示有没有规范命中词；第二段 0/1 表示是不是微信默认名。
    """
    name = path.name
    hits = [index for index, word in enumerate(KEYWORDS) if word in name]
    return (
        0 if hits else 1,
        1 if name.startswith(WECHAT_PREFIX) else 0,
        hits[0] if hits else len(KEYWORDS),
        name,
    )


def pick_photos(act_dir: Path, sub: str = PHOTO_SUBDIR,
                limit: int = 8) -> list[Path]:
    """挑图：规范命名的优先，微信默认名靠后，按文件名排序取前 limit 张。

    目录不存在或没有图片返回空列表；limit <= 0 时返回空列表。
    """
    folder = Path(act_dir) / sub
    if not folder.is_dir() or int(limit) <= 0:
        return []
    photos = [path for path in folder.rglob("*") if _is_image(path)]
    return sorted(photos, key=_rank)[:int(limit)]


def _build_prompt(photos: list[Path], facts: dict, style: str) -> str:
    """拼图注 prompt：活动事实 + 文件名清单 + 输出骨架。"""
    blocks = [
        "【角色】",
        "你是成都市益嘉仁社会工作服务中心的项目社工，正在给活动简报配图注。",
        "你只能看到照片的文件名和活动事实，看不到照片本身。",
        "",
        "【活动事实】",
        llm._facts_text(facts) or "- （未提供）",
        "",
        "【照片清单】（按顺序编号，一条对应一张）",
    ]
    blocks += [f"{seq}. {path.name}" for seq, path in enumerate(photos, start=1)]
    if str(style or "").strip():
        blocks += ["", "【文风】", str(style).strip()]

    blocks += [
        "",
        "【写作要求】",
        "- 只根据文件名与上面的事实推断画面，不确定就写通用描述（如「活动现场」）",
        "- 不许瞎猜细节：不写没给出的人名、数字、店铺名、具体动作",
        "- 每条 15~40 字，以「图N」开头，N 与清单序号一致",
        "- 每条一句话，不加句号以外的标点堆叠",
        "",
        f"只输出 JSON，不要任何解释：{{\"图注\": [\"图1 …\", …]}}，共 {len(photos)} 条。",
    ]
    return "\n".join(blocks)


def _fallback_caption(seq: int, facts: dict) -> str:
    """模型少给一条时的兜底：用活动名称写通用描述，不编细节。"""
    name = str(facts.get("活动名称") or "活动").strip()
    return f"图{seq} {name}现场"


def write_captions(photos: list[Path], facts: dict, *,
                   style: str = "") -> list[dict]:
    """调 LLM 为每张图写图注，返回 [{"file": 文件名, "caption": "图1 …"}]。

    LLM 拿不到图片，只拿文件名与 facts；返回条数不足时用通用描述补齐，
    保证调用方按序号填 {{图注_N}} 时不会缺行。
    """
    photos = [Path(path) for path in photos]
    if not photos:
        return []

    content = llm._chat(_build_prompt(photos, facts, style))
    data = llm._parse_json(content)
    raw = data.get("图注")
    if not isinstance(raw, list):
        raise llm.LLMError(f"图注输出不是数组：{content[:300]}")

    out: list[dict] = []
    for seq, path in enumerate(photos, start=1):
        text = str(raw[seq - 1]).strip() if seq - 1 < len(raw) else ""
        # 模型可能自带「图1：」，先去掉再统一编号，避免出现「图1 图1 …」
        text = re.sub(r"^图\s*\d+\s*[.、:：]?\s*", "", text)
        caption = f"图{seq} {text}".strip() if text else _fallback_caption(seq, facts)
        out.append({"file": path.name, "caption": caption})
    return out


def as_fill_data(captions: list[dict]) -> dict:
    """图注列表 → 填充数据：{"图注": ["图1 …", …]}，对应模板 {{图注_1}}…"""
    return {"图注": [item.get("caption", "") for item in captions]}
