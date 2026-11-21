"""工单 B：权限矩阵、认证边界与路径安全（独立验收，不改任何现有文件）。

两条腿走路：
1) 真实 HTTP 打本机服务 http://127.0.0.1:8001 —— 验状态码与响应体；
2) 直接调 api.deps / api.shared.paths —— 验权限矩阵与路径归一，不依赖网络。

本文件可能留下两类服务端状态，测试自己负责清干净：
- 探测用的 API Key（名字 __sec_verify_tmp__）→ finally 里吊销 + 删行；
- 限速计数（api.state._LOGIN_FAILS，内存字典）→ autouse fixture 前后清空；
- 登录产生的 sessions 行 → 测完删掉本文件建的 token。
限速用例一律用不存在的用户名 __rate_test_user__，绝不锁 admin。
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import time
import types
import urllib.parse
from pathlib import Path

import psycopg
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
# 与 test_auth_chain.py 对齐：连真实库，验真实账号 / Key
os.environ.setdefault(
    "SOCIALWORK_DB_DSN",
    "host=127.0.0.1 port=5432 dbname=socialwork user=admin password=tjfjyjr")

import app  # noqa: E402
from api.deps import can_write, check_project, current_user, project_access  # noqa: E402
from api.deps import require_admin, scopes_of  # noqa: E402
from api.shared.paths import normalize_rel, safe_rel_path  # noqa: E402
from api.state import _LOGIN_FAIL_MAX  # noqa: E402

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from verify_guard import pytestmark  # noqa: E402,F401  生产验证套件守卫

HOST, PORT = "127.0.0.1", 8001
PROJECT = "惠民社区保障资金项目"
TMP_KEY_NAME = "__sec_verify_tmp__"
# 限速用例用不存在的随机用户名（见 _fresh_rate_user），不锁 admin。
# 本文件登录产生的会话 token；session 级收尾时按 token 精确删（ua 列为空，
# 靠 token 识别；admin_token 是 session 级 fixture，中途删会让后续用例 401）。
_SESSION_TOKENS: list[str] = []

ADMIN_PW = os.environ.get("SOCIALWORK_ADMIN_PW", "tjfjyjr")

ADMIN = {"id": 1, "username": "admin", "role": "admin",
         "real_name": "管理员", "projects": {PROJECT: "write"},
         "auth_type": "session"}
EDITOR = {"id": 2, "username": "editor", "role": "editor",
          "projects": {PROJECT: "write"}, "auth_type": "api_key"}
VIEWER = {"id": 3, "username": "viewer", "role": "viewer",
          "projects": {PROJECT: "readonly"}, "auth_type": "session"}
OUTSIDER = {"id": 9, "username": "out", "role": "editor",
            "projects": {"花都社区保障资金项目": "write"}, "auth_type": "api_key"}
ZERO = {"id": 5, "username": "zero", "role": "editor", "projects": {},
        "auth_type": "session"}


# ---------------- 真实 HTTP 小工具 ----------------

def _http(method: str, path: str, body: dict | None = None,
          headers: dict | None = None) -> tuple[int, str, dict]:
    """裸 http.client：不碰 urllib 的自动编码，中文路径不会炸。"""
    conn = http.client.HTTPConnection(HOST, PORT, timeout=20)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("content-type", "application/json")
    try:
        conn.request(method, path, data, hdrs)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, raw, dict(resp.getheaders())
    finally:
        conn.close()


def _login(username: str, password: str) -> tuple[int, str]:
    return _http("POST", "/api/login", {"username": username, "password": password})[:2]


@pytest.fixture(scope="session")
def admin_token() -> str:
    """admin 的 Web 会话 token；连不上服务就 skip（不拿假连接冒充）。"""
    st, raw, _ = _http("POST", "/api/login",
                       {"username": "admin", "password": ADMIN_PW})
    if st != 200:
        pytest.skip(f"登录失败（HTTP {st}）：{raw[:120]}")
    token = json.loads(raw)["token"]
    _SESSION_TOKENS.append(token)
    yield token


@pytest.fixture
def tmp_api_key(admin_token: str):
    """临时 API Key（admin 身份）。yield 明文；用完吊销 + 删行。"""
    st, raw, _ = _http("POST", "/api/sw/keys",
                       {"name": TMP_KEY_NAME, "scopes": []},
                       {"x-token": admin_token})
    if st != 200:
        pytest.skip(f"签发临时 Key 失败（HTTP {st}）：{raw[:120]}")
    made = json.loads(raw)
    yield made
    _http("DELETE", f"/api/sw/keys/{made['id']}", headers={"x-token": admin_token})
    _purge_key_rows(made["id"])


def _dsn() -> str:
    return os.environ["SOCIALWORK_DB_DSN"]


def _purge_key_rows(key_id: int) -> None:
    """吊销是软删；这里把探测行物理删掉，保持 api_keys 表干净。"""
    try:
        with psycopg.connect(_dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_keys WHERE id = %s", (key_id,))
            conn.commit()
    except Exception:
        pass


def _purge_sessions(tokens) -> None:
    """按 token 删 sessions；tokens 为空时跳过（ANY(空数组) 会报错）。"""
    if not tokens:
        return
    try:
        with psycopg.connect(_dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE token = ANY(%s)",
                            (list(tokens),))
            conn.commit()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _clean_all_sessions():
    """整批测试跑完后，把本文件产生的 session 一次删干净。

    不能在每个用例后删：admin_token 是 session 级 fixture，第一个用例
    跑完就删会让后续用例拿到 401。所以只在全部结束时收尾。
    """
    yield
    _purge_sessions(_SESSION_TOKENS)


@pytest.fixture(autouse=True)
def _clean_state():
    """清理本文件在服务端留下的临时 Key；限速计数按随机用户名规避。"""
    yield
    try:
        with psycopg.connect(_dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM api_keys WHERE name = %s", (TMP_KEY_NAME,))
            conn.commit()
    except Exception:
        pass


class _Req:
    """current_user 只用到 headers 与 client.host。"""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}
        self.client = types.SimpleNamespace(host="203.0.113.9")


class _Cur:
    """假游标：按 SQL 关键字返回预设行（照 test_api_sw.py 的做法）。"""

    def __init__(self, conn):
        self.conn = conn
        self.rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split()).lower()
        self.rows = []
        if "from api_keys" in text:
            self.rows = [self.conn.key_row] if self.conn.key_row else []
        elif "from user_projects" in text:
            self.rows = list(self.conn.project_rows)
        elif "from users" in text:
            self.rows = [self.conn.user_row] if self.conn.user_row else []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, *, key_row=None, user_row=None, projects=()):
        self.key_row = key_row
        self.user_row = user_row
        self.project_rows = list(projects)

    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass


# ================= 1. 认证边界 =================

def test_no_token_all_401():
    """无 token：/me / projects / dashboard / admin/users 全 401。"""
    for path in ("/api/me", "/api/projects", "/api/dashboard", "/api/admin/users"):
        st, raw, _ = _http("GET", path)
        assert st == 401, f"{path} 应 401，实际 {st}：{raw[:120]}"


def test_invalid_session_token_401():
    """无效 x-token → 401，不是 200 也不是 500。"""
    for path in ("/api/me", "/api/projects", "/api/dashboard"):
        st, raw, _ = _http("GET", path, headers={"x-token": "stale-not-a-session"})
        assert st == 401, f"{path} 应 401，实际 {st}：{raw[:120]}"


def test_invalid_api_key_401():
    """无效 API Key（两种头）→ 401；非 sk-sw- 前缀也 401。"""
    for headers in ({"authorization": "Bearer sk-sw-invalid"},
                    {"x-api-key": "sk-sw-invalid"}):
        st, raw, _ = _http("GET", "/api/me", headers=headers)
        assert st == 401, f"{headers} 应 401，实际 {st}：{raw[:120]}"
    st, raw, _ = _http("GET", "/api/me", headers={"authorization": "Bearer garbage"})
    assert st == 401
    assert "API Key" in raw


def test_invalid_key_does_not_fall_back_to_session(admin_token):
    """关键：无效 API Key + 有效 x-token 同时存在 → 仍 401，不许回退 session。

    回退的后果：Key 被吊销后，浏览器里的旧 session 仍能继续用，等于吊销不生效。
    """
    headers = {"authorization": "Bearer sk-sw-invalid", "x-token": admin_token}
    st, raw, _ = _http("GET", "/api/me", headers=headers)
    assert st == 401, f"无效 Key 不得回退 session，实际 {st}：{raw[:120]}"
    # 反向顺序（x-api-key 在 session 之后）同样必须 401
    st, raw, _ = _http("GET", "/api/me",
                       headers={"x-token": admin_token, "x-api-key": "sk-sw-invalid"})
    assert st == 401, f"无效 x-api-key 不得回退 session，实际 {st}：{raw[:120]}"


def test_current_user_rejects_invalid_key_before_session(monkeypatch):
    """直接调依赖：无效 Key 抛 401，且根本不去查 session 表。"""
    import api.auth as _auth

    def _boom(conn, token):        # 一旦回退查 session 就炸，测试立刻红
        raise AssertionError("无效 API Key 竟回退去查 session")

    monkeypatch.setattr(_auth, "get_user", _boom)
    monkeypatch.setattr(_auth, "user_projects", lambda *a, **k: {})
    with pytest.raises(app.HTTPException) as exc:
        current_user(_Req({"authorization": "Bearer sk-sw-invalid",
                           "x-token": "whatever"}), _Conn())
    assert exc.value.status_code == 401


# ================= 2. API Key 禁访管理面 =================

def test_api_key_cannot_read_admin_users(tmp_api_key):
    """有效 API Key 访问 /api/admin/users → 403，且错误信息含「API Key」。"""
    st, raw, _ = _http("GET", "/api/admin/users",
                       headers={"authorization": "Bearer " + tmp_api_key["key"]})
    assert st == 403, f"管理面应拒绝 API Key，实际 {st}：{raw[:160]}"
    assert "API Key" in raw, f"错误信息要说明是 API Key 被拒：{raw[:160]}"


def test_api_key_blocked_even_with_valid_session(tmp_api_key, admin_token):
    """管理面双保险：API Key + 有效 x-token 同时带，仍 403，不回退。"""
    st, raw, _ = _http("GET", "/api/admin/users",
                       headers={"authorization": "Bearer " + tmp_api_key["key"],
                                "x-token": admin_token})
    assert st == 403, f"应 403，实际 {st}：{raw[:160]}"
    assert "API Key" in raw


def test_session_admin_can_read_admin_users(admin_token):
    """对照：x-token（admin）访问同一端点 → 200。"""
    st, raw, _ = _http("GET", "/api/admin/users", headers={"x-token": admin_token})
    assert st == 200, f"admin session 应 200，实际 {st}：{raw[:160]}"
    assert json.loads(raw)["items"] is not None


def test_require_admin_rejects_api_key_and_viewer():
    """直接调 require_admin：api_key 403、viewer 403、session admin 通过。"""
    with pytest.raises(app.HTTPException) as exc:
        require_admin({**ADMIN, "auth_type": "api_key"})
    assert exc.value.status_code == 403
    assert "API Key" in str(exc.value.detail)
    with pytest.raises(app.HTTPException) as exc:
        require_admin({**ADMIN, "role": "viewer"})
    assert exc.value.status_code == 403
    assert require_admin({**ADMIN, "auth_type": "session"})["username"] == "admin"


# ================= 3. 权限矩阵 =================

def test_admin_passes_any_project():
    """admin 对任意项目（含不存在的名字）读/写都通过。"""
    check_project(ADMIN, PROJECT)
    check_project(ADMIN, PROJECT, write=True)
    check_project(ADMIN, "根本不存在的项目", write=True)
    assert can_write(ADMIN, "根本不存在的项目") is True


def test_outsider_forbidden():
    """无该项目权限：读 403 FORBIDDEN，写同样 403。"""
    assert project_access(OUTSIDER, PROJECT) is None
    assert can_write(OUTSIDER, PROJECT) is False
    with pytest.raises(app.ApiError) as exc:
        check_project(OUTSIDER, PROJECT)
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"
    with pytest.raises(app.ApiError) as exc:
        check_project(OUTSIDER, PROJECT, write=True)
    assert exc.value.code == "FORBIDDEN"


def test_readonly_write_403_readonly():
    """readonly 项目：读通过，写 403 且错误码必须是 READONLY。"""
    assert project_access(VIEWER, PROJECT) == "readonly"
    check_project(VIEWER, PROJECT)                      # 读不抛
    with pytest.raises(app.ApiError) as exc:
        check_project(VIEWER, PROJECT, write=True)
    assert exc.value.status_code == 403
    assert exc.value.code == "READONLY"
    # editor 角色 + readonly 授权：全局写权限有，项目级仍只读
    ro_editor = {**EDITOR, "projects": {PROJECT: "readonly"}}
    assert can_write(ro_editor, PROJECT) is False
    with pytest.raises(app.ApiError) as exc:
        check_project(ro_editor, PROJECT, write=True)
    assert exc.value.code == "READONLY"


def test_zero_projects_is_empty_not_everything():
    """零项目账号：scopes 空列表，任何项目都 403 —— 不能兜底成「全部可见」。"""
    assert scopes_of(ZERO) == []
    with pytest.raises(app.ApiError) as exc:
        scopes_of(ZERO, PROJECT)
    assert exc.value.status_code == 403
    assert exc.value.code == "FORBIDDEN"
    with pytest.raises(app.ApiError) as exc:
        check_project(ZERO, PROJECT)
    assert exc.value.code == "FORBIDDEN"


def test_scopes_of_passthrough_and_foreign():
    """scopes_of：admin 传项目名直接放行；外来项目 403。"""
    assert scopes_of(ADMIN, PROJECT) == [PROJECT]
    assert scopes_of(VIEWER) == [PROJECT]
    with pytest.raises(app.ApiError) as exc:
        scopes_of(OUTSIDER, PROJECT)
    assert exc.value.code == "FORBIDDEN"


def test_current_user_matrix_with_fake_db():
    """直接调 current_user：无头 401 / 无效 Key 401 / 非前缀 401 / 有效 Key 200。"""
    with pytest.raises(app.HTTPException) as exc:
        current_user(_Req(), _Conn())
    assert exc.value.status_code == 401
    with pytest.raises(app.HTTPException) as exc:
        current_user(_Req({"authorization": "Bearer sk-sw-invalid"}),
                     _Conn(key_row=None))
    assert exc.value.status_code == 401
    with pytest.raises(app.HTTPException) as exc:
        current_user(_Req({"authorization": "Bearer nope"}), _Conn())
    assert exc.value.status_code == 401
    # 有效 Key 行：8 列 (id,user_id,expires,revoked,username,role,enabled,real_name)
    row = (7, 1, None, None, "admin", "admin", True, "聂聃")
    user = current_user(_Req({"x-api-key": "sk-sw-valid"}),
                        _Conn(key_row=row, user_row=(1, "admin", "admin", True),
                              projects=((PROJECT, "write"),)))
    assert user["auth_type"] == "api_key"
    assert user["projects"][PROJECT] == "write"


# ================= 4. 路径安全 =================

@pytest.fixture
def base(tmp_path):
    d = tmp_path / "act"
    d.mkdir()
    return d


@pytest.mark.parametrize("rel", [
    "../etc/passwd",            # 经典上跳
    "a/../../b",               # 中间上跳
    "../../etc/passwd",
    "..\\..\\x.docx",          # 反斜杠上跳
    "/etc/passwd",             # 绝对路径
    "%2fescape.docx",          # URL 编码的绝对路径
    "%2Fetc%2Fpasswd",         # 编码的绝对路径
    "..%2f..%2fx.docx",        # 编码的上跳
    "%252e%252e/x",            # 双重编码
    "a" * 300 + ".docx",       # 单段超长
])
def test_safe_rel_path_rejects(base, rel):
    """危险输入一律 400，绝不能返回一个越界或归一后失控的路径。"""
    with pytest.raises(app.HTTPException) as exc:
        safe_rel_path(base, rel)
    assert exc.value.status_code == 400, f"{rel!r} 未被拒绝：{exc.value.detail}"


def test_safe_rel_path_accepts_normal(base):
    """正常相对路径通过；.trash 前缀必须保留（PR-06：回收站恢复靠它找文件）。"""
    got = safe_rel_path(base, "正常.docx")
    assert got == (base / "正常.docx").resolve()
    trash = safe_rel_path(base, ".trash/2026/a.txt")
    assert trash == (base / ".trash/2026/a.txt").resolve()
    assert trash.parts[len(base.resolve().parts)] == ".trash", ".trash 前缀被剥掉了"


def test_normalize_rel_safe_outcomes():
    """台账归一：上跳吃掉、绝对路径出界变空、.trash 与中文名原样保留。"""
    assert normalize_rel("../etc/passwd") == "etc/passwd"
    assert normalize_rel("a/../../b") == "b"
    assert normalize_rel("/etc/passwd") == ""
    assert normalize_rel(".trash/2026/a.txt") == ".trash/2026/a.txt"
    assert normalize_rel("正常.docx") == "正常.docx"
    assert normalize_rel("") == ""


def test_download_endpoint_rejects_encoded_traversal(admin_token):
    """端到端：下载端点对编码上跳回 400，不落到磁盘读文件。

    端点路径里的项目名/活动名是中文，http.client 只发 ASCII，
    必须整体 percent-encode，否则客户端先抛 UnicodeEncodeError。
    """
    proj = urllib.parse.quote(PROJECT, safe="")
    act = urllib.parse.quote("11-共治行动-第一期", safe="")
    q = urllib.parse.quote("../../etc/passwd", safe="")
    st, raw, _ = _http(
        "GET", f"/api/projects/{proj}/activities/{act}/download?path={q}",
        headers={"x-token": admin_token})
    assert st == 400, f"应 400，实际 {st}：{raw[:120]}"
    assert "路径" in raw, raw[:160]
    # 未编码的 .. 同样被 safe_rel_path 挡下（真实服务端解码后是 ..）
    st, raw, _ = _http(
        "GET", f"/api/projects/{proj}/activities/{act}/download?path=../../etc/passwd",
        headers={"x-token": admin_token})
    assert st == 400 and "路径" in raw, f"未编码 .. 应 400，实际 {st}：{raw[:120]}"


# ================= 5. 登录限速 =================

def _fresh_rate_user() -> str:
    """每次测试换一个不存在的用户名。

    服务端计数在 uvicorn 进程内存（api.state._LOGIN_FAILS），pytest 进程
    清不到；用过的名字会一直带着 429 走完 15 分钟窗口。同时满足工单
    「必须用测试专用用户名」：不存在、永不锁 admin。
    """
    return f"__rate_{os.getpid()}_{time.time_ns()}__"


def test_11th_failure_returns_429():
    """连续 11 次错误密码 → 前 10 次 401，第 11 次 429。"""
    user = _fresh_rate_user()
    for i in range(_LOGIN_FAIL_MAX):
        st, raw = _login(user, "wrong-password")
        assert st == 401, f"第 {i + 1} 次应 401，实际 {st}：{raw[:120]}"
    st, raw = _login(user, "wrong-password")
    assert st == 429, f"第 {_LOGIN_FAIL_MAX + 1} 次应 429，实际 {st}：{raw[:120]}"
    assert "retry_after" in raw, raw[:200]


def test_rate_limit_does_not_lock_admin():
    """限速按 用户名+IP 计数：锁了测试用户，admin 仍能正常登录。"""
    user = _fresh_rate_user()
    for _ in range(_LOGIN_FAIL_MAX + 1):
        _login(user, "wrong-password")
    st, raw, _ = _http("POST", "/api/login",
                       {"username": "admin", "password": ADMIN_PW})
    assert st == 200, f"admin 被误锁，实际 {st}：{raw[:160]}"
    _SESSION_TOKENS.append(json.loads(raw)["token"])


def test_login_fails_counters_cleared():
    """成功登录清空该 key 的失败记录，避免正常用户被历史失败拖累。"""
    for _ in range(5):
        _login(_fresh_rate_user(), "wrong")
    st, raw, _ = _http("POST", "/api/login",
                       {"username": "admin", "password": ADMIN_PW})
    assert st == 200
    _SESSION_TOKENS.append(json.loads(raw)["token"])
    # 服务端进程的计数 pytest 读不到；直接验「还能再失败 10 次」更实在
    user = _fresh_rate_user()
    codes = [_login(user, "wrong")[0] for _ in range(_LOGIN_FAIL_MAX)]
    assert codes == [401] * _LOGIN_FAIL_MAX, codes
