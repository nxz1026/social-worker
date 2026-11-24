"""E1：/api/sw/* Agent 接口层。

沿用仓库习惯：直接调用端点函数（不依赖 httpx / TestClient）。
数据库用最小 FakeConn（只实现 api_keys / users / user_projects 三条查询），
项目根、模板库、审计全部用 tmp_path + monkeypatch。
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from docx import Document

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402
from api.admin import admin_update_user
from api.auth import api_sw_me
from api.routes import agent as agent_routes  # noqa: E402
from api.deps import can_write, project_access
from api.deps import check_project
from api.deps import current_user
from api.deps import require_admin
from api.shared.paths import safe_rel_path  # noqa: E402
import apikey  # noqa: E402

ADMIN = {"id": 1, "username": "admin", "role": "admin",
         "projects": {"huimin": "write", "huadu": "write"},
         "auth_type": "api_key"}
VIEWER = {"id": 3, "username": "viewer", "role": "viewer",
          "projects": {"huimin": "readonly"}, "auth_type": "api_key"}
# editor 才有全局写权限；项目级 readonly 用来验 READONLY
READONLY_EDITOR = {"id": 4, "username": "ro", "role": "editor",
                   "projects": {"huimin": "readonly"}, "auth_type": "api_key"}
EDITOR = {"id": 2, "username": "editor", "role": "editor",
          "projects": {"huimin": "write"}, "auth_type": "api_key"}

ACT = "11-共治行动-第一期"


class Req:
    """端点只用到 request.headers / request.client.host。"""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}
        self.client = types.SimpleNamespace(host="127.0.0.1")


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split()).lower()
        self.rows = []
        self.rowcount = 0
        if "from api_keys" in text:
            self.rows = [self.conn.key_row] if self.conn.key_row else []
        elif "from user_projects" in text:
            self.rows = list(self.conn.project_rows)
        elif "from users" in text:
            self.rows = [self.conn.user_row] if self.conn.user_row else []
        elif "update api_keys" in text:
            self.rowcount = 1 if self.conn.key_row else 0

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    """假的 psycopg 连接：不连数据库，只按 SQL 关键字返回预设行。"""

    def __init__(self, *, key_row=None, user_row=None, projects=()):
        self.key_row = key_row
        self.user_row = user_row
        self.project_rows = list(projects)

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _silent_audit(monkeypatch):
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)
    monkeypatch.setattr("api.shared.audit.audit", lambda *a, **k: None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """一个项目 + 一个活动 + 一个模板库。"""
    root = tmp_path / "projects"
    project = root / "惠民项目"
    (project / "活动" / ACT).mkdir(parents=True)
    (project / "惠民项目.yaml").write_text(
        yaml.safe_dump({"project": {"short": "huimin", "code_rules": {}}},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    (project / "活动" / ACT / "activity.yaml").write_text(
        yaml.safe_dump({"code": "CDYJR2026008-C-16110001", "status": "imported"},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    lib = tmp_path / "templates" / "library"
    lib.mkdir(parents=True)
    doc = Document()
    doc.add_paragraph("项目：{{项目名称}}")
    doc.add_paragraph("编号：{{项目编号}}")
    doc.add_paragraph("自定义：{{自定义}}")
    # 原来这里还有一次同名保存（brief → 简报.docx），把上面 3 个占位符覆盖成
    # 只剩 1 个；决算表改成简报后就暴露了这个重复写入。
    doc.save(str(lib / "简报.docx"))
    monkeypatch.setattr(app._projects, "PROJECTS_ROOT", root)
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr("api.state.ROOT", tmp_path)
    return root


def _key_row(*, revoked=None, expires=None, user_id=1, username="admin",
             role="admin", enabled=True, real_name="聂聃"):
    return (7, user_id, expires, revoked, username, role, enabled, real_name)


# ---------------- 认证 ----------------

def test_no_auth_401():
    with pytest.raises(app.HTTPException) as exc:
        current_user(Req(), FakeConn())
    assert exc.value.status_code == 401


def test_wrong_key_401():
    with pytest.raises(app.HTTPException) as exc:
        current_user(Req({"authorization": "Bearer sk-sw-not-a-real-key"}),
                         FakeConn(key_row=None))
    assert exc.value.status_code == 401


def test_revoked_key_401():
    row = _key_row(revoked=datetime.now(timezone.utc))
    with pytest.raises(app.HTTPException) as exc:
        current_user(Req({"authorization": "Bearer sk-sw-revoked"}),
                         FakeConn(key_row=row))
    assert exc.value.status_code == 401


def test_expired_key_401():
    row = _key_row(expires=datetime.now(timezone.utc) - timedelta(days=1))
    with pytest.raises(app.HTTPException) as exc:
        current_user(Req({"x-api-key": "sk-sw-expired"}),
                         FakeConn(key_row=row))
    assert exc.value.status_code == 401


def test_valid_key_returns_user_with_project_dict():
    row = _key_row(expires=None)
    conn = FakeConn(key_row=row, user_row=(1, "admin", "admin", True),
                    projects=())
    user = current_user(Req({"authorization": "Bearer sk-sw-valid"}), conn)
    assert user["username"] == "admin"
    assert user["auth_type"] == "api_key"
    assert isinstance(user["projects"], dict)


def test_key_generate_and_hash_roundtrip():
    plain, digest, prefix = apikey.generate_key()
    assert plain.startswith("sk-sw-")
    assert len(plain) == len("sk-sw-") + 43
    assert prefix == plain[:12]
    assert digest == apikey.hash_key(plain)
    # 与标准 sha256 对齐，并验证不同输入产生不同哈希
    import hashlib
    assert apikey.hash_key(plain) == hashlib.sha256(plain.encode()).hexdigest()
    assert apikey.hash_key(plain) != apikey.hash_key(plain + "x")


# ---------------- 能力 / 权限 ----------------

def test_capabilities_no_auth():
    out = agent_routes.api_sw_capabilities()
    assert out["version"] == "1.0"
    assert out["auth"]["header"].startswith("Authorization: Bearer")
    paths = {item["path"] for item in out["endpoints"]}
    assert "/api/sw/run" in paths and "/api/sw/capabilities" not in paths


def test_readonly_project_write_403_readonly(env):
    with pytest.raises(app.ApiError) as exc:
        agent_routes.api_sw_run(
            {"project": "惠民项目", "activity": ACT, "template": "简报"},
            Req(), None, READONLY_EDITOR)
    assert exc.value.status_code == 403
    assert exc.value.code == "READONLY"


def test_cross_project_403_forbidden(env):
    outsider = {"id": 9, "username": "out", "role": "editor",
                "projects": {"huadu": "write"}, "auth_type": "api_key"}
    with pytest.raises(app.ApiError) as exc:
        agent_routes.api_sw_run(
            {"project": "惠民项目", "activity": ACT, "template": "简报"},
            Req(), None, outsider)
    assert exc.value.code == "FORBIDDEN"


def test_project_name_and_short_match_each_other(env):
    """user_projects 存项目名或 short 都能互认（旧的 huimin/huadu 映射已移除）。"""
    (env / "惠民项目" / "惠民项目.yaml").write_text(
        yaml.safe_dump({"project": {"short": "惠民社区保障资金项目"}},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    by_short = {"id": 5, "username": "a", "role": "editor",
                "projects": {"惠民社区保障资金项目": "write"}}
    by_name = {"id": 6, "username": "b", "role": "editor",
               "projects": {"惠民项目": "readonly"}}
    # 存 short，传目录名
    assert can_write(by_short, "惠民项目") is True
    # 存目录名，传 short
    assert project_access(by_name, "惠民社区保障资金项目") == "readonly"
    # 旧英文别名不再兜底
    legacy = {"id": 7, "username": "c", "role": "editor",
              "projects": {"huimin": "write"}}
    assert can_write(legacy, "惠民项目") is False


def test_can_write_and_check_project(env):
    assert can_write(READONLY_EDITOR, "惠民项目") is False
    assert can_write(EDITOR, "惠民项目") is True
    assert can_write(ADMIN, "任意项目") is True
    with pytest.raises(app.ApiError) as exc:
        check_project(READONLY_EDITOR, "惠民项目", write=True)
    assert exc.value.code == "READONLY"


# ---------------- 一步生成 ----------------

def test_run_template_writes_file(env):
    out = agent_routes.api_sw_run(
        {"project": "惠民项目", "activity": ACT, "template": "简报",
         "data": {"自定义": "测试值"}, "output": "07-简报.docx"},
        Req(), None, ADMIN)
    dst = env / "惠民项目" / "活动" / ACT / "07-简报.docx"
    assert dst.is_file()
    assert out["ok"] is True
    assert out["path"] == f"活动/{ACT}/07-简报.docx"
    assert out["filled"] >= 3
    assert "测试值" in out["preview"]


def test_run_template_dry_run_does_not_write(env):
    out = agent_routes.api_sw_run(
        {"project": "惠民项目", "activity": ACT, "template": "简报",
         "dry_run": True},
        Req(), None, ADMIN)
    assert out["dry_run"] is True
    assert out["path"] == f"活动/{ACT}/07-简报.docx"   # 默认名带序号
    assert not (env / "惠民项目" / "活动" / ACT / "简报.docx").exists()


def test_run_task_uses_llm_and_default_template(env, monkeypatch):
    monkeypatch.setattr(
        app._llm, "write",
        lambda task, facts, style="": {"自定义": "AI 生成内容",
                                       "_check": {"ok": True, "score": 100}})
    out = agent_routes.api_sw_run(
        {"project": "惠民项目", "activity": ACT, "task": "brief", "style": "务实"},
        Req(), None, ADMIN)
    assert "AI 生成内容" in out["preview"]
    assert out["self_check"]["ok"] is True
    assert (env / "惠民项目" / "活动" / ACT / "07-简报.docx").is_file()


def test_run_template_missing_code_is_code_missing(env):
    (env / "惠民项目" / "活动" / ACT / "activity.yaml").write_text(
        yaml.safe_dump({"status": "imported"}, allow_unicode=True),
        encoding="utf-8")
    with pytest.raises(app.ApiError) as exc:
        agent_routes.api_sw_run(
            {"project": "惠民项目", "activity": ACT, "template": "简报"},
            Req(), None, ADMIN)
    assert exc.value.code == "CODE_MISSING"


def test_run_template_not_found(env):
    with pytest.raises(app.ApiError) as exc:
        agent_routes.api_sw_run(
            {"project": "惠民项目", "activity": ACT, "template": "不存在的模板"},
            Req(), None, ADMIN)
    assert exc.value.code == "TEMPLATE_NOT_FOUND"


def test_sw_me_shape():
    out = api_sw_me(ADMIN)
    assert out["auth_type"] == "api_key"
    assert out["projects"]["huimin"] == "write"


def test_batch_check(env):
    out = agent_routes.api_sw_batch(
        {"action": "check", "project": "惠民项目", "activities": [ACT]},
        Req(), None, ADMIN)
    assert out["results"][0]["activity"] == ACT
    assert out["results"][0]["status"] == "ok"


def test_sw_task_stub_returns_501():
    """审计 C-04：不再假装 done，明确回 501。"""
    with pytest.raises(app.ApiError) as exc:
        agent_routes.api_sw_task("t-1", ADMIN)
    assert exc.value.status_code == 501
    assert exc.value.code == "TASKS_NOT_IMPLEMENTED"


# ---------------- 审计修复回归（2026-11-20） ----------------

def test_safe_rel_path_rejects_encoded_traversal(tmp_path):
    """审计 P1-6：`%2f` 编码后的 `..` 与绝对路径必须拒绝。"""
    from fastapi import HTTPException
    for bad in ["..%2f..%2fx.docx", "%2fescape.docx", "%252e%252e/x.docx", "../x.docx"]:
        with pytest.raises(HTTPException) as exc:
            safe_rel_path(tmp_path, bad)
        assert exc.value.status_code == 400, f"{bad} 应被拒绝"
    # 正常文件名仍然通过
    assert safe_rel_path(tmp_path, "ok.docx").name == "ok.docx"


def test_safe_rel_path_rejects_overlong_segment(tmp_path):
    """审计 P1-5：单段超 200 字节要 400，不能落到 500。"""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        safe_rel_path(tmp_path, "a" * 300 + ".docx")
    assert exc.value.status_code == 400
    assert safe_rel_path(tmp_path, "a" * 50 + ".docx").name.endswith(".docx")


def test_require_admin_blocks_api_key():
    """审计 P0-1：API Key 不能访问 /api/admin/*。"""
    from fastapi import HTTPException
    keyed = {"id": 4, "username": "admin", "role": "admin",
             "auth_type": "api_key", "projects": {}}
    with pytest.raises(HTTPException) as exc:
        require_admin(keyed)
    assert exc.value.status_code == 403
    assert "API Key" in str(exc.value.detail)

    # Web 会话仍可用
    session_user = {"id": 4, "username": "admin", "role": "admin",
                    "auth_type": "session", "projects": {}}
    assert require_admin(session_user) is session_user


def test_admin_cannot_disable_self():
    """不能禁用自己：禁用后 Web 与 API 都进不来（审计发现的实际事故）。

    直接调端点，验证 400 而不是靠条件推断。
    """
    from fastapi import HTTPException

    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, role FROM users WHERE role='admin' "
                        "ORDER BY id LIMIT 1")
            row = cur.fetchone()
        if not row:
            pytest.skip("库里没有 admin")
        uid = row[0]
        admin_user = {"id": uid, "username": row[1], "role": "admin",
                      "auth_type": "session", "projects": {}}
        req = _fake_request()

        with pytest.raises(HTTPException) as exc:
            admin_update_user(uid, {"enabled": False}, req, conn, admin_user)
        assert exc.value.status_code == 400
        assert "不能禁用自己" in str(exc.value.detail)

        with pytest.raises(HTTPException) as exc2:
            admin_update_user(uid, {"role": "viewer"}, req, conn, admin_user)
        assert exc2.value.status_code == 400
        assert "管理员角色" in str(exc2.value.detail)

        # 确认库里状态没被改坏
        with conn.cursor() as cur:
            cur.execute("SELECT role, enabled FROM users WHERE id=%s", (uid,))
            role, enabled = cur.fetchone()
        assert role == "admin" and enabled is True
    finally:
        conn.close()


def _fake_request():
    """构造 admin_update_user 需要的 Request（只用到 client.host）。"""
    class _Client:
        host = "127.0.0.1"

    class _Req:
        client = _Client()
    return _Req()


# 本文件的多数用例用假 DSN（离线跑），只有需要真实库的用例走这个
REAL_DSN = (os.environ.get("SOCIALWORK_REAL_TEST_DSN")
            or "host=127.0.0.1 port=5432 dbname=socialwork user=admin password=tjfjyjr")


def _db():
    """连真实库；连不上就 skip，不假通过。"""
    psycopg = pytest.importorskip("psycopg")
    try:
        return psycopg.connect(REAL_DSN)
    except Exception as exc:
        pytest.skip(f"连不上数据库：{exc}")
