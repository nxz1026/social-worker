#!/usr/bin/env python3
"""API 服务配置：读 config/api.yaml，环境变量优先。

查找顺序：
  1. 环境变量 SOCIALWORK_API_CONFIG 指定的文件
  2. 仓库根目录的 config/api.yaml
  3. /srv/social-work/config/api.yaml

环境变量 SOCIAL_WORK_ROOT / SOCIALWORK_TEMPLATES / SOCIALWORK_DB_DSN / PORT
分别覆盖 root / 模板目录 / 数据库连接 / 端口。LLM 的 endpoint、model、api_key
不在这里，仍由服务端 .env 提供。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

API_DIR = Path(__file__).resolve().parent
REPO_ROOT = API_DIR.parent
FALLBACK_CONFIG = Path("/srv/social-work/config/api.yaml")

DEFAULTS = {
    "server": {
        "host": "127.0.0.1",
        "port": 8001,
        "root": "/srv/social-work",
        "templates_dir": "~/.dsh/skills/social-work/templates",
        "cors_origins": ["*"],
        "timezone_offset_hours": 8,
    },
    "auth": {"token_ttl_hours": 12},
    "db": {"dsn": ""},
    "projects": ["huadu", "huimin"],
    "llm": {
        "endpoint": "",
        "model": "",
        "api_key": "",
        "max_tokens": 16000,
        "timeout_seconds": 240,
        "temperature": 0.3,
    },
    "search": {"timeout_seconds": 90, "snippet_chars": 4000, "material_chars": 3000},
    "tasks": {
        "archive_timeout_seconds": 600,
        "reconcile_timeout_seconds": 180,
        "delete_timeout_seconds": 300,
    },
}


@dataclass
class Settings:
    path: Path | None
    root: Path
    templates_dir: Path
    cors_origins: list[str]
    timezone_offset_hours: int
    token_ttl_hours: int
    projects: list[str]
    db_dsn: str
    server: dict
    llm: dict
    search: dict
    tasks: dict


def find_config_path(path: str | Path | None = None) -> Path | None:
    """按优先级找配置文件，找不到返回 None（走内置默认值）。"""
    if path:
        return Path(path).expanduser()
    env = os.environ.get("SOCIALWORK_API_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in (REPO_ROOT / "config" / "api.yaml", FALLBACK_CONFIG):
        if candidate.is_file():
            return candidate
    return None


def load(path: str | Path | None = None) -> Settings:
    """读取配置；显式指定路径但文件不存在时抛错，其余情况用默认值。"""
    cfg_path = find_config_path(path)
    raw: dict = {}
    if cfg_path is not None and cfg_path.is_file():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    elif path or os.environ.get("SOCIALWORK_API_CONFIG"):
        raise FileNotFoundError(f"配置文件不存在：{cfg_path}")

    def section(name: str) -> dict:
        return {**DEFAULTS[name], **(raw.get(name) or {})}

    server = section("server")
    auth = section("auth")
    db = section("db")
    llm = section("llm")
    search = section("search")
    tasks = section("tasks")

    root = Path(os.environ.get("SOCIAL_WORK_ROOT") or server["root"]).expanduser()
    templates = Path(
        os.environ.get("SOCIALWORK_TEMPLATES") or server["templates_dir"]).expanduser()
    dsn = os.environ.get("SOCIALWORK_DB_DSN") or str(db.get("dsn") or "").strip()
    if not dsn:
        raise RuntimeError(
            "未配置数据库连接：设置环境变量 SOCIALWORK_DB_DSN，或在 config/api.yaml 填 db.dsn")

    return Settings(
        path=cfg_path,
        root=root,
        templates_dir=templates,
        cors_origins=[str(x) for x in (server.get("cors_origins") or ["*"])],
        timezone_offset_hours=int(server["timezone_offset_hours"]),
        token_ttl_hours=int(auth["token_ttl_hours"]),
        projects=[str(x) for x in (raw.get("projects") or DEFAULTS["projects"])],
        db_dsn=dsn,
        server={**server, "port": int(os.environ.get("PORT") or server["port"])},
        llm=llm,
        search=search,
        tasks=tasks,
    )


SETTINGS = load()
