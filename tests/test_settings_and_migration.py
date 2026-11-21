#!/usr/bin/env python3
"""工单 C：配置加载（api/settings.py）与启动迁移（_ensure_api_key_secret_column）。

两块零覆盖区域：
  1. api/settings.py —— YAML + 环境变量 + 默认值的合并规则。settings 一旦解析错，
     全服务用错配置（监听地址 / CORS / 数据库连接串）。
  2. api/app.py 的启动迁移 —— 给 api_keys 补 secret_enc 列；失败被 `except Exception: pass`
     吞掉，生产上可能长期缺列而无人发现（审计 C-07 已记录，本文件只确认现状）。

隔离约定（与仓库其它测试一致）：
  - 只新增本文件，不改任何现有文件；
  - 不读真实配置文件：一律显式把配置路径指向 tmp_path，或用假路径让 find_config_path 找不到；
  - 不连真实数据库：monkeypatch app.psycopg.connect / psycopg.connect 用假连接。

import app 会连带 import api.state，两者各自 import settings，形成**两个** SETTINGS 单例
（裸名 `settings` 一份、`api.settings` 一份）。本文件涉及 app.PG_DSN 的用例都基于
app 模块内的对象操作，不依赖两份单例是否同一。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402
import settings as _bare_settings  # noqa: E402  裸名单例（app.SETTINGS 就是它）


# --------------------------------------------------------------------------
# 公共夹具 / 替身
# --------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """干净环境：清掉所有会影响配置解析的环境变量，ROOT 指到 tmp_path。"""
    for key in ("SOCIALWORK_API_CONFIG", "SOCIAL_WORK_ENV", "SOCIALWORK_TEMPLATES",
                "SOCIALWORK_DB_DSN", "SOCIAL_WORK_ROOT", "PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SOCIAL_WORK_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("SOCIALWORK_DB_DSN", "host=127.0.0.1 dbname=fake user=u password=p")
    return monkeypatch


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


class FakeCursor:
    """够用的游标替身：只记录 execute 收到的 SQL。"""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append(sql)


class FakeConn:
    """够用的连接替身：记录 execute/commit，不触库。"""

    def __init__(self, dsn):
        self.dsn = dsn
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.calls.append("__commit__")


def _patch_connect(monkeypatch, *, target=None, counter=None):
    """把 psycopg.connect 换成返回 FakeConn 的假实现（不触库）。"""
    real = target if target is not None else app.psycopg

    def _connect(dsn=None, **kwargs):
        if counter is not None:
            counter.append(dsn)
        return FakeConn(dsn)

    monkeypatch.setattr(real, "connect", _connect)
    return _connect


# --------------------------------------------------------------------------
# 1. api/settings.py
# --------------------------------------------------------------------------

def test_defaults_when_config_missing_sections(tmp_path, env):
    """配置只写 projects：其余段落全部落内置默认值。"""
    cfg = _write(tmp_path, "min.yaml", "projects:\n  - 甲项目\n  - 乙项目\n")
    s = _bare_settings.load(cfg)
    assert s.server["host"] == "127.0.0.1"
    assert s.server["port"] == 8001
    assert s.timezone_offset_hours == 8
    assert s.token_ttl_hours == 12
    assert s.upload["max_mb"] == 50
    assert s.tasks == {"archive_timeout_seconds": 300,
                       "reconcile_timeout_seconds": 180,
                       "delete_timeout_seconds": 300}
    assert s.search == {"timeout_seconds": 90, "snippet_chars": 4000,
                        "material_chars": 3000}
    assert s.db_dsn == "host=127.0.0.1 dbname=fake user=u password=p"
    assert s.projects == ["甲项目", "乙项目"]


def test_env_overrides_root_templates_port_dsn(tmp_path, env):
    """SOCIAL_WORK_ROOT / SOCIALWORK_TEMPLATES / PORT / SOCIALWORK_DB_DSN 覆盖 YAML。"""
    cfg = _write(tmp_path, "cfg.yaml", (
        "server:\n"
        "  root: /yaml-root\n"
        "  templates_dir: /yaml-tpl\n"
        f"  port: {8123}\n"
        "db:\n"
        "  dsn: \"postgres://yaml\"\n"))
    env.setenv("SOCIAL_WORK_ROOT", "/tmp/from-env-root")
    env.setenv("SOCIALWORK_TEMPLATES", "/tmp/from-env-tpl")
    env.setenv("PORT", "9999")
    env.setenv("SOCIALWORK_DB_DSN", "postgres://from-env-dsn")

    s = _bare_settings.load(cfg)
    assert s.root == Path("/tmp/from-env-root")
    assert s.templates_dir == Path("/tmp/from-env-tpl")
    assert s.server["port"] == 9999
    assert s.db_dsn == "postgres://from-env-dsn"


def test_yaml_partial_section_merges_defaults(tmp_path, env):
    """YAML 只覆盖段内一个键，其余键仍取默认（不是整段替换）。"""
    cfg = _write(tmp_path, "partial.yaml", (
        "search:\n"
        "  timeout_seconds: 7\n"
        "tasks:\n"
        "  delete_timeout_seconds: 11\n"
        "llm:\n"
        "  temperature: 0.9\n"))
    s = _bare_settings.load(cfg)
    assert s.search["timeout_seconds"] == 7          # YAML 覆盖
    assert s.search["snippet_chars"] == 4000         # 段内默认保留
    assert s.tasks["delete_timeout_seconds"] == 11
    assert s.tasks["archive_timeout_seconds"] == 300
    assert s.llm["temperature"] == 0.9
    assert s.llm["model_id"] == ""                   # llm 段默认


def test_empty_yaml_file_loads_defaults(tmp_path, env):
    """空文件（yaml.safe_load -> None）应回退默认值，不是崩。"""
    cfg = _write(tmp_path, "empty.yaml", "")
    s = _bare_settings.load(cfg)
    assert s.path == cfg
    assert s.server["host"] == "127.0.0.1"
    assert s.cors_origins == ["*"]
    assert s.projects  # 默认三项目


def test_cors_default_is_wildcard_and_only_warns_in_production(tmp_path, env, caplog):
    """CORS 当前实际行为：默认就是 ['*']，生产环境也只打日志、不 fail-closed。

    这是审计 C-05/C-07 关注的点：`allow_origins=["*"]` 等于允许任意站点带
    Cookie/Token 调 API，而代码里没有任何收敛动作（仅告警）。
    """
    cfg = _write(tmp_path, "cors-default.yaml", "db:\n  dsn: \"x\"\n")
    with caplog.at_level(logging.WARNING, logger="settings"):
        s = _bare_settings.load(cfg)
    assert s.cors_origins == ["*"], "默认来源应为通配（当前实现，非 fail-closed）"

    env.setenv("SOCIAL_WORK_ENV", "production")
    with caplog.at_level(logging.WARNING, logger="settings"):
        s2 = _bare_settings.load(cfg)
    assert s2.cors_origins == ["*"], "生产环境也不阻断，来源仍是通配"
    assert any("CORS" in r.message for r in caplog.records), "生产环境应产生 CORS 告警"

    # 显式 allowlist + 生产：无告警
    caplog.clear()
    allow = _write(tmp_path, "cors-explicit.yaml",
                   'server:\n  cors_origins: ["https://a.example"]\ndb:\n  dsn: "x"\n')
    with caplog.at_level(logging.WARNING, logger="settings"):
        s3 = _bare_settings.load(allow)
    assert s3.cors_origins == ["https://a.example"]
    assert not [r for r in caplog.records if "CORS" in r.message]


def test_find_config_path_priority(tmp_path, env):
    """查找顺序：显式 path > SOCIALWORK_API_CONFIG > 仓库 config/api.yaml > /srv。"""
    explicit = _write(tmp_path, "explicit.yaml", "db:\n  dsn: \"x\"\n")
    assert _bare_settings.find_config_path(explicit) == explicit

    env.setenv("SOCIALWORK_API_CONFIG", str(explicit))
    assert _bare_settings.find_config_path() == explicit

    # 不指定且未设环境变量时，回落到仓库 config/api.yaml（存在即命中）
    env.delenv("SOCIALWORK_API_CONFIG")
    found = _bare_settings.find_config_path()
    assert found is None or found.is_file()


def test_missing_explicit_path_raises_filenotfound(tmp_path, env):
    """显式指定的配置不存在 → FileNotFoundError（不静默用默认值）。"""
    missing = tmp_path / "absent.yaml"
    with pytest.raises(FileNotFoundError):
        _bare_settings.load(missing)


def test_env_config_path_missing_raises_filenotfound(tmp_path, env):
    """SOCIALWORK_API_CONFIG 指向不存在的文件 → 同样抛 FileNotFoundError。"""
    env.setenv("SOCIALWORK_API_CONFIG", str(tmp_path / "also-absent.yaml"))
    with pytest.raises(FileNotFoundError):
        _bare_settings.load()


def test_invalid_yaml_syntax_raises_parser_error(tmp_path, env):
    """YAML 语法错 → yaml.parser.ParserError（未捕获，直接冒泡）。"""
    bad = _write(tmp_path, "broken.yaml", "server: [unclosed\n")
    with pytest.raises(Exception) as ei:
        _bare_settings.load(bad)
    assert type(ei.value).__name__ in {"ParserError", "ScannerError", "YAMLError"}


def test_missing_dsn_raises_runtimeerror(tmp_path, env):
    """必填项缺失：db.dsn 与 SOCIALWORK_DB_DSN 都空 → RuntimeError。"""
    env.delenv("SOCIALWORK_DB_DSN")
    cfg = _write(tmp_path, "no-dsn.yaml", "server:\n  host: 0.0.0.0\n")
    with pytest.raises(RuntimeError, match="数据库连接"):
        _bare_settings.load(cfg)


def test_wrong_type_port_raises_valueerror(tmp_path, env):
    """类型错：server.port 写成字符串 → int() 抛 ValueError（无友好错误信息）。"""
    cfg = _write(tmp_path, "bad-port.yaml",
                 'server:\n  port: "abc"\ndb:\n  dsn: "x"\n')
    with pytest.raises(ValueError):
        _bare_settings.load(cfg)


# --------------------------------------------------------------------------
# 2. 启动迁移 _ensure_api_key_secret_column
# --------------------------------------------------------------------------

def test_migration_executes_add_column_sql(tmp_path, env, monkeypatch):
    """正常路径：执行 ALTER TABLE ... ADD COLUMN IF NOT EXISTS，并 commit。"""
    seen: list[tuple] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            seen.append(("execute", sql, params))

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cursor()

        def commit(self):
            seen.append(("commit",))

    captured = {}

    def fake_connect(dsn=None, **kwargs):
        captured["dsn"] = dsn
        return Conn()

    monkeypatch.setattr(app.psycopg, "connect", fake_connect)

    app._ensure_api_key_secret_column()

    assert captured["dsn"] == app.PG_DSN
    stmts = [s for kind, *s in seen if kind == "execute"]
    assert len(stmts) == 1
    sql = stmts[0][0]
    assert "ALTER TABLE api_keys" in sql
    assert "ADD COLUMN IF NOT EXISTS" in sql
    assert "secret_enc" in sql
    assert ("commit",) in seen


def test_migration_survives_connection_failure(tmp_path, env, monkeypatch):
    """数据库连不上：函数不得抛错（否则启动事件里会把异常抛给 uvicorn）。"""
    def boom(dsn=None, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(app.psycopg, "connect", boom)
    app._ensure_api_key_secret_column()      # 不抛即通过


@pytest.mark.parametrize("where", ["connect", "execute", "commit"])
def test_migration_swallows_failures(tmp_path, env, monkeypatch, where):
    """连接 / 执行 / 提交任一环节失败都被吞掉 —— 可观测性缺陷（审计 C-07）。

    这是**现状确认**：失败无任何日志，线上缺列不会被发现。
    """
    class BoomCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if where == "execute":
                raise RuntimeError("alter failed")


    class BoomConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return BoomCursor()

        def commit(self):
            if where == "commit":
                raise RuntimeError("commit failed")

    def fake_connect(dsn=None, **kwargs):
        if where == "connect":
            raise OSError("connection refused")
        return BoomConn()

    monkeypatch.setattr(app.psycopg, "connect", fake_connect)
    app._ensure_api_key_secret_column()      # 三种失败都不抛


def test_migration_is_idempotent(tmp_path, env, monkeypatch):
    """重复调用不炸，每次都重跑同一条幂等 SQL。"""
    counter: list = []
    _patch_connect(monkeypatch, counter=counter)

    for _ in range(3):
        app._ensure_api_key_secret_column()

    assert len(counter) == 3
    assert all(dsn == app.PG_DSN for dsn in counter)


def test_migration_connects_with_pgdsk(tmp_path, env, monkeypatch):
    """用的是 app.PG_DSN（启动时固化的连接串），不是现读 SETTINGS.db_dsn。"""
    counter: list = []
    _patch_connect(monkeypatch, counter=counter)
    before = app.PG_DSN
    try:
        app.PG_DSN = "host=127.0.0.1 dbname=changed user=u password=p"
        app._ensure_api_key_secret_column()
        assert counter and counter[0] == "host=127.0.0.1 dbname=changed user=u password=p"
    finally:
        app.PG_DSN = before


# --------------------------------------------------------------------------
# 说明
# --------------------------------------------------------------------------
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
