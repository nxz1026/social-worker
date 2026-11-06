#!/usr/bin/env python3
"""集中读取 config/config.yaml，收口服务端路径、容器名与数据集名。

查找顺序：
  1. 环境变量 SOCIAL_WORK_CONFIG 指定的文件
  2. 本仓库根目录（scripts/ 的上一级）的 config/config.yaml
  3. /srv/social-work/config/config.yaml

环境变量 SOCIAL_WORK_ROOT 覆盖 root（临时换根目录时不必改配置文件）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_CONFIG = Path("/srv/social-work/config/config.yaml")

DEFAULTS = {
    "root": "/srv/social-work",
    "container": "social-work-cognee",
    "container_root": "/host/social-work",
    "key_file": ".cognee-key",
    "audit_retention_days": 90,
    "datasets": {
        "huadu": "huadu",
        "huimin": "huimin",
    },
    "llm": {
        "provider": "openai",
        "model": "",
        "endpoint": "",
        "api_key_env": "LLM_API_KEY",
    },
}


@dataclass
class Config:
    root: Path
    container: str
    container_root: str
    key_file: Path
    audit_retention_days: int
    datasets: dict
    llm: dict
    path: Path | None = None


def find_config_path(path: str | Path | None = None) -> Path | None:
    """按优先级找配置文件，找不到返回 None（走内置默认值）。"""
    if path:
        return Path(path).expanduser()
    env = os.environ.get("SOCIAL_WORK_CONFIG")
    if env:
        return Path(env).expanduser()
    local = REPO_ROOT / "config" / "config.yaml"
    if local.is_file():
        return local
    if FALLBACK_CONFIG.is_file():
        return FALLBACK_CONFIG
    return None


def load_config(path: str | Path | None = None) -> Config:
    """读取配置。path 或 SOCIAL_WORK_CONFIG 指定的文件必须存在。"""
    cfg_path = find_config_path(path)
    raw: dict = {}
    if cfg_path is not None and cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    elif path or os.environ.get("SOCIAL_WORK_CONFIG"):
        raise FileNotFoundError(f"配置文件不存在：{cfg_path}")

    root = Path(os.environ.get("SOCIAL_WORK_ROOT") or raw.get("root") or DEFAULTS["root"])
    key_file = Path(str(raw.get("key_file") or DEFAULTS["key_file"])).expanduser()
    if not key_file.is_absolute():
        key_file = root / key_file

    return Config(
        root=root,
        container=str(raw.get("container") or DEFAULTS["container"]),
        container_root=str(raw.get("container_root") or DEFAULTS["container_root"]),
        key_file=key_file,
        audit_retention_days=int(raw.get("audit_retention_days") or DEFAULTS["audit_retention_days"]),
        datasets={**DEFAULTS["datasets"], **(raw.get("datasets") or {})},
        llm={**DEFAULTS["llm"], **(raw.get("llm") or {})},
        path=cfg_path,
    )


if __name__ == "__main__":
    cfg = load_config()
    print(f"config: {cfg.path}")
    print(f"root: {cfg.root}")
    print(f"container: {cfg.container}")
    print(f"container_root: {cfg.container_root}")
    print(f"key_file: {cfg.key_file}")
