"""E2：真实姓名 + 项目键中文迁移 + 用户增删改。

数据库用内存 FakeConn（只实现用户管理端点用到的 SQL），
项目根、审计全部用 tmp_path + monkeypatch。
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402

HUIMIN = "惠民社区保障资金项目"
JILI = "花都社区激励资金项目"

ADMIN = {"id": 4, "username": "admin", "real_name": "聂聃", "role": "admin",
         "projects": {HUIMIN: "write", JILI: "write"}}


class Req:
    """端点只用到 request.client.host。"""

    client = types.SimpleNamespace(host="127.0.0.1")
    headers: dict = {}


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
        params = tuple(params or ())
        self.rows = []
        self.rowcount = 0
        users = self.conn.users

        if text.startswith("select id, username, real_name, role, enabled, created_at"):
            self.rows = list(users)
        elif text.startswith("select project, access from user_projects"):
            self.rows = list(self.conn.projects_by_user.get(params[0], []))
        elif text.startswith("select project from user_projects"):
            self.rows = [(p,) for p, _a in self.conn.projects_by_user.get(params[0], [])]
        elif text.startswith("select id from users where username"):
            self.rows = [(u[0],) for u in users if u[1] == params[0]]
        elif text.startswith("select id from users where id"):
            self.rows = [(u[0],) for u in users if u[0] == params[0]]
        elif text.startswith("select role from users where id"):
            self.rows = [(u[3],) for u in users if u[0] == params[0]]
        elif "count(*) from users where role" in text:
            self.rows = [(sum(1 for u in users if u[3] == "admin"),)]
        elif text.startswith("insert into users"):
            self.conn.next_id += 1
            username, _pw, role, real_name = params
            users.append((self.conn.next_id, username, real_name, role, True,
                          "2026-11-19 00:00:00+08"))
            self.rows = [(self.conn.next_id,)]
        elif text.startswith("update users set real_name"):
            self.conn._update(params[1], real_name=params[0])
            self.rowcount = 1
        elif text.startswith("update users set role"):
            self.conn._update(params[1], role=params[0])
            self.rowcount = 1
        elif text.startswith("update users set enabled"):
            self.conn._update(params[1], enabled=params[0])
            self.rowcount = 1
        elif text.startswith("update users set password_hash"):
            self.rowcount = 1
        elif text.startswith("delete from users where id"):
            before = len(users)
            users[:] = [u for u in users if u[0] != params[0]]
            self.rowcount = before - len(users)
        elif text.startswith("delete from user_projects"):
            self.conn.projects_by_user[params[0]] = []
        elif text.startswith("insert into user_projects"):
            uid, name, access = params
            self.conn.projects_by_user.setdefault(uid, []).append((name, access))
        elif text.startswith("delete from sessions"):
            self.rowcount = 0
        elif "from api_keys" in text:
            self.rows = list(self.conn.key_rows)
        elif text.startswith("update api_keys"):
            self.rowcount = 1

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    """内存假库：users 里存 (id, username, real_name, role, enabled, created_at)。"""

    def __init__(self, users=None, projects_by_user=None, key_rows=None):
        self.users = list(users or [])
        self.projects_by_user = {k: list(v) for k, v in (projects_by_user or {}).items()}
        self.key_rows = list(key_rows or [])
        self.next_id = max([u[0] for u in self.users] or [0])
        self.executed: list[tuple[str, tuple]] = []

    def _update(self, uid, **fields):
        idx = {"real_name": 2, "role": 3, "enabled": 4}
        for i, u in enumerate(self.users):
            if u[0] != uid:
                continue
            row = list(u)
            for key, value in fields.items():
                row[idx[key]] = value
            self.users[i] = tuple(row)

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _silent_audit(monkeypatch):
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """两个中文项目：与生产目录名一致。"""
    root = tmp_path / "projects"
    for name in (HUIMIN, JILI):
        d = root / name
        (d / "活动").mkdir(parents=True)
        (d / f"{name}.yaml").write_text(
            yaml.safe_dump({"project": {"short": name, "code_rules": {}}},
                           allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    monkeypatch.setattr(app._projects, "PROJECTS_ROOT", root)
    return root


def _two_users():
    return [
        (4, "admin", "聂聃", "admin", True, "2026-01-01 00:00:00+08"),
        (5, "editor", "李四", "editor", True, "2026-01-02 00:00:00+08"),
    ]


# ---------------- 用户列表 ----------------

def test_admin_users_real_name_and_project_dict(projects_root):
    conn = FakeConn(_two_users(), {5: [ (HUIMIN, "readonly") ]})
    out = app.admin_users(conn, ADMIN)
    items = {i["username"]: i for i in out["items"]}

    assert items["admin"]["real_name"] == "聂聃"
    assert isinstance(items["admin"]["projects"], dict)
    assert items["admin"]["projects"][HUIMIN] == "write"
    assert items["editor"]["real_name"] == "李四"
    assert items["editor"]["projects"] == {HUIMIN: "readonly"}


# ---------------- 新建 / 修改 ----------------

def test_create_user_with_real_name_and_dict_projects(projects_root):
    conn = FakeConn(_two_users())
    out = app.admin_create_user(
        {"username": "wang", "password": "pw", "real_name": "王五",
         "role": "editor",
         "projects": {HUIMIN: "write", JILI: "readonly"}},
        Req(), conn, ADMIN)

    created = next(u for u in conn.users if u[0] == out["id"])
    assert created[1] == "wang"
    assert created[2] == "王五"
    assert set(conn.projects_by_user[out["id"]]) == {
        (HUIMIN, "write"), (JILI, "readonly")}


def test_create_user_accepts_legacy_project_list(projects_root):
    conn = FakeConn(_two_users())
    out = app.admin_create_user(
        {"username": "liu", "password": "pw", "role": "viewer",
         "projects": [HUIMIN]},
        Req(), conn, ADMIN)
    assert conn.projects_by_user[out["id"]] == [(HUIMIN, "write")]


def test_update_user_real_name_and_projects(projects_root):
    conn = FakeConn(_two_users(), {5: [(JILI, "write")]})
    app.admin_update_user(
        5, {"real_name": "李四改", "projects": {HUIMIN: "readonly"}},
        Req(), conn, ADMIN)

    updated = next(u for u in conn.users if u[0] == 5)
    assert updated[2] == "李四改"
    assert conn.projects_by_user[5] == [(HUIMIN, "readonly")]


def test_update_user_rejects_bad_access(projects_root):
    conn = FakeConn(_two_users())
    with pytest.raises(app.HTTPException) as exc:
        app.admin_update_user(5, {"projects": {HUIMIN: "owner"}}, Req(), conn, ADMIN)
    assert exc.value.status_code == 400


# ---------------- 删除 ----------------

def test_delete_user_hard(projects_root):
    conn = FakeConn(_two_users())
    out = app.admin_delete_user(5, Req(), conn, ADMIN)
    assert out["ok"] is True and out["removed"] is True
    assert [u[0] for u in conn.users] == [4]


def test_delete_self_rejected(projects_root):
    conn = FakeConn(_two_users())
    with pytest.raises(app.HTTPException) as exc:
        app.admin_delete_user(4, Req(), conn, ADMIN)
    assert exc.value.status_code == 400
    assert "自己" in exc.value.detail
    assert [u[0] for u in conn.users] == [4, 5]


def test_delete_last_admin_rejected(projects_root):
    conn = FakeConn([(4, "admin", "聂聃", "admin", True, "2026-01-01")])
    caller = {"id": 9, "username": "other", "role": "admin", "projects": {}}
    with pytest.raises(app.HTTPException) as exc:
        app.admin_delete_user(4, Req(), conn, caller)
    assert exc.value.status_code == 400
    assert "最后一个 admin" in exc.value.detail


def test_delete_missing_user_404(projects_root):
    conn = FakeConn(_two_users())
    with pytest.raises(app.HTTPException) as exc:
        app.admin_delete_user(99, Req(), conn, ADMIN)
    assert exc.value.status_code == 404


# ---------------- /api/sw/me、/api/sw/keys ----------------

def test_sw_me_uses_chinese_project_names():
    out = app.api_sw_me(ADMIN)
    assert out["real_name"] == "聂聃"
    assert out["projects"] == {HUIMIN: "write", JILI: "write"}
    assert all("huimin" != key and "huadu" != key for key in out["projects"])


def test_sw_keys_include_real_name():
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    row = (5, 4, "admin", "聂聃", "kratos-agent", "sk-sw-pnPz",
           None, None, now, now, None)
    out = app.api_list_keys(FakeConn(key_rows=[row]), ADMIN)
    assert out["items"][0]["real_name"] == "聂聃"
    assert out["items"][0]["user"] == "admin"
    assert out["items"][0]["revoked"] is False
    # 旧 Key 没有 secret_enc，不能假装能看到明文
    assert out["items"][0]["secret"] is None


def test_sw_keys_return_decrypted_secret(tmp_path, monkeypatch):
    """新 Key 的 secret_enc 能解出完整明文。"""
    monkeypatch.setattr(app, "ROOT", tmp_path)
    plain = "sk-sw-pnPzabcdefghijklmnopqrstuvwxyz0123456789"
    enc = app._keysafe.encrypt(plain, tmp_path)
    now = datetime(2026, 11, 19, tzinfo=timezone.utc)
    row = (6, 5, "editor", "李四", "web-签发", "sk-sw-pnPz",
           None, None, now, now, None, enc)

    out = app.api_list_keys(FakeConn(key_rows=[row]), ADMIN)

    assert out["items"][0]["secret"] == plain


# ---------------- 历史数据兼容（只读旧台账） ----------------

def test_legacy_project_name_maps_english_keys(projects_root):
    """旧台账里的 huimin/huadu 只在读取时归一，授权键本身仍是中文项目名。"""
    assert app._legacy_project_name("huimin") == HUIMIN
    assert app._legacy_project_name("huadu") == JILI
    assert app._legacy_project_name(HUIMIN) == HUIMIN
    assert app._legacy_project_name("不存在") == "不存在"


def test_list_docs_filters_legacy_manifest_project(projects_root, monkeypatch):
    monkeypatch.setattr(app, "_load_manifest", lambda: {
        "files": {
            "t1": {"name": "花都.docx", "project": "huadu", "status": "done"},
            "t2": {"name": "惠民.docx", "project": "huimin", "status": "done"},
        }})
    out = app.list_docs("", "", "", {"id": 1, "role": "editor",
                                     "projects": {HUIMIN: "write"}})
    assert [x["token"] for x in out["items"]] == ["t2"]


# ---------------- 归档人 ----------------

def test_build_data_archive_name_from_user(projects_root):
    act = projects_root / HUIMIN / "活动" / "01-走访调研-第一期"
    act.mkdir(parents=True)
    (act / "activity.yaml").write_text(
        yaml.safe_dump({"code": "C-1"}, allow_unicode=True), encoding="utf-8")

    with_user = app._activity.build_data(HUIMIN, str(act),
                                        user={"real_name": "聂聃"})
    without = app._activity.build_data(HUIMIN, str(act))
    assert with_user["归档人"] == "聂聃"
    assert without["归档人"] is None
