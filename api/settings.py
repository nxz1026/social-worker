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

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

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
    "projects": ["惠民社区保障资金项目", "花都社区保障资金项目", "花都社区激励资金项目"],
    "llm": {
        "base_url": "",
        "model_id": "",
        "api_key": "",
        "max_tokens": 16000,
        "timeout_seconds": 240,
        "temperature": 0.3,
    },
    "search": {"timeout_seconds": 90, "snippet_chars": 4000, "material_chars": 3000},
    "upload": {"max_mb": 50},
    "tasks": {
        "archive_timeout_seconds": 300,
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
    upload: dict
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
        # 顶层必须是映射；原来是列表时报 AttributeError: 'list' object has no
        # attribute 'get'，看不出是配置问题。
        if not isinstance(raw, dict):
            raise ValueError(
                f"配置文件顶层必须是键值映射（YAML mapping），实际是 "
                f"{type(raw).__name__}：{cfg_path}")
    elif path or os.environ.get("SOCIALWORK_API_CONFIG"):
        raise FileNotFoundError(f"配置文件不存在：{cfg_path}")

    def section(name: str) -> dict:
        got = raw.get(name) or {}
        if not isinstance(got, dict):
            raise ValueError(
                f"配置项 {name} 必须是键值映射，实际是 {type(got).__name__}：{cfg_path}")
        return {**DEFAULTS[name], **got}

    def str_list(value, field: str) -> list[str]:
        """把配置里的列表字段规整成 list[str]。

        原来是 [str(x) for x in value]：YAML 里误写成字符串时会被**逐字符拆开**
        （"https://a.com" → ['h','t','t',...,'m']），导致 CORS 静默失效。
        """
        if value is None:
            return []
        if isinstance(value, str):
            raise ValueError(
                f"配置项 {field} 必须是列表，实际是字符串 {value!r}：{cfg_path}")
        if not isinstance(value, (list, tuple)):
            raise ValueError(
                f"配置项 {field} 必须是列表，实际是 {type(value).__name__}：{cfg_path}")
        out = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, (dict, list, tuple)):
                raise ValueError(
                    f"配置项 {field} 的元素必须是标量，实际是 {type(item).__name__}：{cfg_path}")
            out.append(str(item))
        return out

    server = section("server")
    auth = section("auth")
    db = section("db")
    llm = section("llm")
    search = section("search")
    upload = section("upload")
    tasks = section("tasks")

    root = Path(os.environ.get("SOCIAL_WORK_ROOT") or server["root"]).expanduser()
    templates = Path(
        os.environ.get("SOCIALWORK_TEMPLATES") or server["templates_dir"]).expanduser()
    dsn = os.environ.get("SOCIALWORK_DB_DSN") or str(db.get("dsn") or "").strip()
    if not dsn:
        raise RuntimeError(
            "未配置数据库连接：设置环境变量 SOCIALWORK_DB_DSN，或在 config/api.yaml 填 db.dsn")

    cors_origins = str_list(server.get("cors_origins") or ["*"], "server.cors_origins")
    # 生产环境通配来源等于把 API 暴露给任意站点。原来是只告警不阻断，
    # 意味着漏配一次就静默放行（审计 C-05）。现在改为 fail closed：
    # 生产 + 通配 → 直接拒绝启动，逼部署时显式配置。
    # 确实需要通配（例如内网全同源反代）时，显式设
    # SOCIAL_WORK_ALLOW_WILDCARD_CORS=1 放行，把风险变成一次有意识的决定。
    if cors_origins == ["*"] and os.environ.get(
            "SOCIAL_WORK_ENV", "").strip().lower() == "production":
        if os.environ.get("SOCIAL_WORK_ALLOW_WILDCARD_CORS", "").strip() != "1":
            raise RuntimeError(
                "生产环境（SOCIAL_WORK_ENV=production）不允许 CORS 通配来源 *。"
                "请在配置文件里把 server.cors_origins 改成显式 allowlist"
                f"（当前配置：{cfg_path or '内置默认值'}）；"
                "确需通配请显式设 SOCIAL_WORK_ALLOW_WILDCARD_CORS=1。")
        log.warning("生产环境使用通配 CORS（已通过 "
                    "SOCIAL_WORK_ALLOW_WILDCARD_CORS=1 显式放行）")

    def to_int(value, field: str) -> int:
        """带字段名的整数解析。原来直接 int(x)，类型错只报
        `invalid literal for int()`，看不出是哪个配置项。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"配置项 {field} 必须是整数，实际是 {value!r}：{cfg_path}") from None

    return Settings(
        path=cfg_path,
        root=root,
        templates_dir=templates,
        cors_origins=cors_origins,
        timezone_offset_hours=to_int(server["timezone_offset_hours"],
                                     "server.timezone_offset_hours"),
        token_ttl_hours=to_int(auth["token_ttl_hours"], "auth.token_ttl_hours"),
        projects=str_list(raw.get("projects") or DEFAULTS["projects"], "projects"),
        db_dsn=dsn,
        server={**server,
                "port": to_int(os.environ.get("PORT") or server["port"],
                               "server.port（或环境变量 PORT）")},
        llm=llm,
        search=search,
        upload=upload,
        tasks=tasks,
    )


SETTINGS = load()
