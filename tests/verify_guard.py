"""生产验证套件统一守卫：服务/数据库/资产不可用时整文件跳过。

三个 test_verify_* 都用真实 HTTP 打 127.0.0.1:8001、直连 PostgreSQL、读
/srv/social-work 真实资产，属于「生产验证套件」而非单元测试。
默认跳过；需要时显式 SW_RUN_LIVE=1 开启（审计 R-04）。
"""
from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

BASE_URL = os.environ.get("SW_TEST_BASE_URL", "http://127.0.0.1:8001").rstrip("/")


def _service_up() -> bool:
    host, _, port = BASE_URL.split("//")[-1].partition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or 80)),
                                      timeout=1.0):
            return True
    except OSError:
        return False


def _db_up() -> bool:
    """数据库是否可用：不猜 DSN 来源（可能在 systemd 单元里），直接探测。

    优先用 psql 走本地默认连接；失败则尝试常见 DSN 环境变量。
    """
    from shutil import which
    import subprocess

    if which("psql"):
        r = subprocess.run(
            ["psql", "-h", "127.0.0.1", "-U", "admin", "-d", "socialwork",
             "-Atc", "select 1"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "PGPASSWORD": os.environ.get("PGPASSWORD", "")})
        if r.returncode == 0:
            return True
    dsn = os.environ.get("SOCIALWORK_DB_DSN", "").strip()
    if dsn:
        try:
            import psycopg
            with psycopg.connect(dsn, connect_timeout=2):
                return True
        except Exception:
            return False
    return False


def _psql_available() -> bool:
    from shutil import which
    return which("psql") is not None


_LIVE = os.environ.get("SW_RUN_LIVE", "").strip() == "1"

pytestmark = pytest.mark.skipif(
    not (_LIVE and _service_up() and _db_up() and _psql_available()),
    reason=("生产验证套件：需要在线 API、真实 PostgreSQL 与 psql 客户端；"
            "设 SW_RUN_LIVE=1 显式开启"))
