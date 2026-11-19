"""图片处置策略（M1 闸门）：按目录决定图片怎么处理。

纯函数模块，不依赖 config.py，方便单测。
"""
from __future__ import annotations

from pathlib import Path

import yaml

# 兜底策略：判不了就只存档（成本最低的默认）
DEFAULT_POLICY = {"rules": [], "default": "archive_only"}


def load_policy(config_dir) -> dict:
    """读 config/image_policy.yaml；缺文件或坏文件时返回只存档的兜底策略。"""
    path = Path(config_dir) / "image_policy.yaml"
    if not path.is_file():
        return dict(DEFAULT_POLICY)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return dict(DEFAULT_POLICY)
    if not isinstance(data, dict):
        return dict(DEFAULT_POLICY)
    rules = [r for r in data.get("rules") or [] if isinstance(r, dict)]
    return {"rules": rules, "default": data.get("default", "archive_only")}


def decide_action(rel_path: str, ext: str, policy: dict | None, image_exts) -> str | None:
    """按目录决定图片处置。

    返回 "ocr" / "vision" / "archive_only"；非图片或没有策略 → None（走原流程）。

    rel_path 是相对「未分类」的路径，第一段即所在目录。
    """
    if policy is None or ext not in image_exts:
        return None
    parts = Path(rel_path).parts
    first = parts[0] if parts else ""
    for rule in policy.get("rules", []):
        if not rule.get("enabled", True):
            continue
        if rule.get("dir") == first:
            return rule.get("action")
    return policy.get("default", "archive_only")
