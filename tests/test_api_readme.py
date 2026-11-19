"""D1：README 相关 API 端点。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402

ADMIN = {"id": 1, "username": "admin", "role": "admin", "projects": {"测试项目": "write"}}
VIEWER = {"id": 3, "username": "viewer", "role": "viewer", "projects": {"测试项目": "readonly"}}

ACT = "01-走访调研-第一期"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """一个项目、一个活动，项目根指向 tmp。"""
    base = tmp_path / "projects"
    project = base / "测试项目"
    (project / "活动" / ACT).mkdir(parents=True)
    (project / "测试项目.yaml").write_text(
        yaml.safe_dump({"project": {"short": "测试项目"}},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    monkeypatch.setattr(app._projects, "PROJECTS_ROOT", base)
    return base


def test_regen_readme_endpoint(root):
    """单活动重生成：首次 written=True，再调 written=False。"""
    first = app.api_regen_readme("测试项目", ACT, ADMIN)
    assert first == {"ok": True, "written": True, "path": "README.md"}
    assert (root / "测试项目" / "活动" / ACT / "README.md").is_file()

    second = app.api_regen_readme("测试项目", ACT, ADMIN)
    assert second["written"] is False


def test_refresh_all_readme_endpoint(root):
    """全量刷新返回 {written: n, skipped: n}。"""
    out = app.api_refresh_all_readme(ADMIN)
    assert out == {"written": 2, "skipped": 0}     # 1 活动 + 1 项目


def test_regen_readme_requires_write(root):
    """viewer 无写权限。"""
    with pytest.raises(Exception) as exc:
        app.api_regen_readme("测试项目", ACT, VIEWER)
    assert getattr(exc.value, "status_code", None) == 403
