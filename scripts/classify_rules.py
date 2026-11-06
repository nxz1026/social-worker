#!/usr/bin/env python3
"""M2 分类规则层：在 LLM 分类之前先用关键词规则判一次。

判得了 → 省一次 LLM 调用；判不了（没命中 / 最高分并列）→ 返回 None，交 LLM。

设计原则：宁可交 LLM，不可判错。
- 文件名是人工起的标签，权重高（默认 10）
- 正文预览是原始内容，提及多、噪声高，权重低（默认 1）
- 必须唯一最高分才算命中；并列最高 → 判不了

配置在 config/ 下三个 yaml，改关键词不用改代码。
"""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULTS = {"filename": 10, "preview": 1}


def load_rules(config_dir) -> dict:
    """读 config/ 下的 project_aliases.yaml + category_keywords.yaml + rule_weights.yaml。"""
    config_dir = Path(config_dir)
    with open(config_dir / "project_aliases.yaml", encoding="utf-8") as f:
        proj_raw = yaml.safe_load(f) or {}
    with open(config_dir / "category_keywords.yaml", encoding="utf-8") as f:
        categories = yaml.safe_load(f) or {}
    with open(config_dir / "rule_weights.yaml", encoding="utf-8") as f:
        weights = yaml.safe_load(f) or {}

    fallback = proj_raw.pop("fallback", None) or {}
    projects = {k: v for k, v in proj_raw.items() if isinstance(v, list)}
    w = dict(DEFAULTS)
    w.update(weights)
    return {"projects": projects, "categories": categories,
            "fallback": fallback, "weights": w}


def _hits(text, keywords) -> int:
    """关键词在文本里的总出现次数。"""
    if not text:
        return 0
    return sum(text.count(k) for k in keywords if k)


def _best(scores: dict) -> str | None:
    """唯一最高分且 >0 才返回 key；否则 None（没命中或并列）。"""
    ranked = sorted(((v, k) for k, v in scores.items() if v > 0), reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def classify_rule(name, preview, rules: dict, projects=None, categories=None) -> dict | None:
    """规则分类。判不了返回 None。

    name       文件名（含扩展名）
    preview    正文预览，可为空串
    rules      load_rules() 的返回值
    projects   合法项目 key 列表（feishu-flow.yaml 的 projects），用于过滤
    categories 合法类别列表，用于过滤

    返回值形状与 scan-inbox.classify() 一致：
        {"project": <key>, "category": <类别>, "confidence": 1.0, "reason": "rule", ...}
    """
    if not isinstance(name, str):
        name = ""
    if not isinstance(preview, str):
        preview = ""

    proj_kw = rules.get("projects") or {}
    cat_kw = rules.get("categories") or {}
    if projects:
        proj_kw = {k: v for k, v in proj_kw.items() if k in projects}
    if categories:
        cat_kw = {k: v for k, v in cat_kw.items() if k in categories}

    wf = (rules.get("weights") or {}).get("filename", DEFAULTS["filename"])
    wp = (rules.get("weights") or {}).get("preview", DEFAULTS["preview"])

    p_scores = {k: _hits(name, kw) * wf + _hits(preview, kw) * wp
                for k, kw in proj_kw.items()}
    project = _best(p_scores)

    c_scores = {k: _hits(name, kw) * wf + _hits(preview, kw) * wp
                for k, kw in cat_kw.items()}
    category = _best(c_scores)

    # 兜底：没判出项目，但类别命中 fallback 规则 → 用 fallback 指定的项目
    if project is None and category is not None:
        for proj, cats in (rules.get("fallback") or {}).items():
            if category in (cats or []):
                project = proj
                break

    if project is None or category is None:
        return None
    return {"project": project, "category": category, "confidence": 1.0,
            "reason": "rule", "scores": {"project": p_scores, "category": c_scores}}
