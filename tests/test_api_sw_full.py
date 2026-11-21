"""工单 B：/api/sw/* 域 15 条端点全测。

全部走 `http://127.0.0.1:8001` 的真实 HTTP 服务（requests），不做 mock：
路径参数校验、multipart/form-data 边界、/api/sw/* 的错误体形状，只有真实
服务才能验；仓库既有的 test_api_*.py 是直调端点函数，那条路测不到这些。

认证：会话 token（x-token）+ 本文件自己签发的 API Key（Authorization: Bearer）。
两者都从真实库取 / 建，收尾统一清理。

破坏性操作一律限制在本文件的标记前缀 `__swfull_`，清理在 fixture 里做，
断言失败也不会把垃圾留在真实活动目录。
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from PIL import Image

# ---------- 服务端真实数据 ----------
BASE = os.environ.get("SW_TEST_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
PROJECT = "惠民社区保障资金项目"          # 有编号的活动在这里
# 写在测试专用活动里：真实业务活动不能被测试写脏（2026-11-21 实测污染过
# 141 张测试照片 + 39 条批次记录）。目录由 tests/conftest_make_test_activity.py 之外
# 的部署步骤创建，不存在时相关用例会 skip。
ACTIVITY = os.environ.get("SOCIAL_WORK_TEST_ACTIVITY", "99-测试专用-第一期")            # code 已配，可生成材料
TIMEOUT = 180                            # LLM 任务约 70s，留足余量

REPO = Path(__file__).resolve().parent.parent
PROJECTS_ROOT = Path(os.environ.get(
    "SOCIAL_WORK_PROJECTS", "/srv/social-work/projects"))
# 进程内直接调用端点函数时，api 模块会加载配置（需 DSN 与 ROOT）
os.environ.setdefault("SOCIAL_WORK_ROOT", str(PROJECTS_ROOT.parent))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN",
    "host=127.0.0.1 port=5432 dbname=socialwork user=admin password=tjfjyjr")
ACT_DIR = PROJECTS_ROOT / PROJECT / "活动" / ACTIVITY
PROJ_DIR = PROJECTS_ROOT / PROJECT

# ---------------- 安全闸：写操作只落在测试专用活动 ----------------
# 测试活动由部署步骤创建（project/活动/99-测试专用-第一期），名字里带「测试专用」。
# 写操作用例（传照片、传附件、生成文件）一律打到这里；万一 ACTIVITY 被改回真实
# 活动，宁可 skip 也不污染生产（2026-11-21 实测污染过 141 张照片 + 39 条批次）。
_IS_TEST_ACTIVITY = ("测试" in ACTIVITY) or bool(os.environ.get("SOCIAL_WORK_TEST_ACTIVITY"))
_skip_write = pytest.mark.skipif(
    not _IS_TEST_ACTIVITY,
    reason=("ACTIVITY 不是测试专用活动，跳过写操作以免污染生产数据；"
            "设 SOCIAL_WORK_TEST_ACTIVITY 指向测试活动目录再跑"))


# 本文件产生的所有磁盘痕迹统一前缀，清理只认这个前缀
MARK = "__swfull_"

# 每次进程启动一个唯一后缀：L3 去重键是「活动 + 日期 + scene」，
# scene 带这个后缀后，任何两次运行、同一文件内的不同用例都不会互相撞。
_RUN_ID = uuid.uuid4().hex[:8]
SCENE = f"{MARK}{_RUN_ID}"

# 每次运行用一个**不同的拍摄日期**：照片上传有 L3「24 小时内同活动 + 同日期 +
# 同场景 → DUPLICATE_BATCH」去重，用固定日期第二次跑就被 409 挡住。
# 取一个远期且随运行漂移的值（年内有效），既避开历史批次，也不会撞上真实业务日期。
# 日期必须 <= 今天（后端有校验），又要每次运行不同：取「今天往回推 N 天」，
# N 随运行耗时变化（0~27 天），跑到明年也不会重复。
_T = int(time.time())
_SHOT_SEQ = [0]


def _shot_date() -> str:
    """按**当前用例名**派生日期：同一条用例内恒定，不同用例之间不同。

    后端有 L3 去重：「同活动 + 同日期 + 同场景，24 小时内 → DUPLICATE_BATCH」。
    注意不能在一条用例里每次都变 —— 预检和实际上传必须用同一个日期才对得上。
    """
    name = os.environ.get("PYTEST_CURRENT_TEST", "") or ""
    seed = int(hashlib.sha256(("scene:" + name).encode()).hexdigest()[:8], 16)
    return (datetime.now() - timedelta(days=seed % 20 + 1)).strftime("%Y-%m-%d")


SHOT_DATE = _shot_date()
# 本次运行开始的时刻（批次号形如 20261121-153609）：清理时用它兜底，
# 凡是本次运行之后创建的批次一律删掉，不管 scene 是否带前缀。
_RUN_STARTED = datetime.now().strftime("%Y%m%d-%H%M%S")
FILE_MARK = f"{MARK}file.docx"
PHOTO_MARK = f"{MARK}photo.jpg"
PROJ_MARK = f"{MARK}proj.docx"
KEY_NAME = f"{MARK}{uuid.uuid4().hex[:8]}"
VIEWER_NAME = f"{MARK}viewer{uuid.uuid4().hex[:6]}"

PG_ENV = {**os.environ, "PGPASSWORD": "tjfjyjr"}
PSQL = ["psql", "-h", "127.0.0.1", "-U", "admin", "-d", "socialwork"]

# ---------------- 基础设施 ----------------
def _sql(sql: str) -> str:
    """执行 SQL，返回 stdout。"""
    return subprocess.run(PSQL + ["-Atc", sql], env=PG_ENV,
                          capture_output=True, text=True, check=True).stdout.strip()


def _purge_marked_files() -> None:
    """删掉本文件标记过的所有文件（含回收站 .trash）。"""
    if not PROJECTS_ROOT.is_dir():
        return
    for path in PROJECTS_ROOT.rglob(f"{MARK}*"):
        try:
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                import shutil
                shutil.rmtree(path)
        except OSError:
            pass


def _purge_state() -> None:
    """库残留：本文件签发的 key 行 / 建的 session / 测试账号。"""
    _sql(f"DELETE FROM api_keys WHERE name LIKE '{MARK}%'")
    _sql("DELETE FROM sessions WHERE ua = 'sw-full-test' OR token LIKE 'swfull-%'")
    _sql(f"DELETE FROM user_projects WHERE user_id IN "
         f"(SELECT id FROM users WHERE username LIKE '{MARK}%')")
    _sql(f"DELETE FROM users WHERE username LIKE '{MARK}%'")
    _purge_photo_meta()


def _purge_photo_meta() -> None:
    """清 _photos.json 里本文件造的批次与照片记录。

    只删磁盘文件是不够的：meta 里留下的 batch/photo/hash 记录会让下一次运行
    被 L3（同活动+同日期+同场景，24 小时）判成 DUPLICATE_BATCH，
    或者被 L2（sha256 命中）判成重复而 saved=0。2026-11-21 实测因此失败 7 例。
    """
    import json as _json
    meta_path = ACT_DIR / "_photos.json"
    if not meta_path.is_file():
        return
    try:
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return
    bad_batches = {b.get("batch_id") for b in meta.get("batches") or []
                   if str(b.get("scene") or "").startswith(MARK)
                   or str(b.get("batch_id") or "") >= _RUN_STARTED}
    bad_names = {p.get("name") for p in meta.get("photos") or []
                 if p.get("batch") in bad_batches
                 or str(p.get("original") or "").startswith(MARK)}
    bad_names.discard(None)
    if not bad_batches and not bad_names:
        return
    meta["batches"] = [b for b in meta.get("batches") or []
                       if b.get("batch_id") not in bad_batches]
    meta["photos"] = [p for p in meta.get("photos") or []
                      if p.get("name") not in bad_names]
    hashes = meta.get("hashes") or {}
    meta["hashes"] = {h: n for h, n in hashes.items() if n not in bad_names}
    try:
        meta_path.write_text(
            _json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


class Api:
    """薄封装：统一 BASE、超时、JSON 解析。"""

    def __init__(self, headers: dict | None = None):
        self.headers = dict(headers or {})

    def _url(self, path: str) -> str:
        return BASE + path

    def get(self, path: str, **kw) -> requests.Response:
        return requests.get(self._url(path), headers=self.headers,
                            timeout=kw.pop("timeout", TIMEOUT), **kw)

    def post(self, path: str, *, json_body=None, files=None, data=None,
             timeout=TIMEOUT) -> requests.Response:
        headers = dict(self.headers)
        if json_body is not None:
            headers.setdefault("content-type", "application/json")
        return requests.post(self._url(path), headers=headers,
                             json=json_body, files=files, data=data,
                             timeout=timeout)

    def delete(self, path: str) -> requests.Response:
        return requests.delete(self._url(path), headers=self.headers,
                               timeout=TIMEOUT)


@pytest.fixture(scope="session")
def token() -> str:
    """admin 的 Web 会话 token：先复用有效会话，没有就现插一条两小时有效的。

    会话只承担「取 admin 身份」，被测的是各端点自身行为。
    """
    tok = _sql("select token from sessions where expires_at > now() limit 1")
    if tok:
        probe = requests.get(f"{BASE}/api/me", headers={"x-token": tok},
                             timeout=TIMEOUT)
        if probe.status_code == 200:
            yield tok
            return
    fresh = (f"swfull-{int(datetime.now(timezone.utc).timestamp())}"
             f"-{uuid.uuid4().hex[:8]}")
    _sql("INSERT INTO sessions(user_id, token, expires_at, ua) VALUES("
         "(SELECT id FROM users WHERE username='admin' LIMIT 1), "
         f"'{fresh}', now() + interval '2 hours', 'sw-full-test')")
    yield fresh
    _sql(f"DELETE FROM sessions WHERE token = '{fresh}'")


@pytest.fixture
def api(token) -> Api:
    return Api({"x-token": token})


@pytest.fixture
def temp_key(api: Api):
    """临时 API Key（admin 身份）。yield (key_id, 明文)，用完吊销 + 删行。"""
    resp = api.post("/api/sw/keys", json_body={"name": KEY_NAME})
    assert resp.status_code == 200, resp.text
    made = resp.json()
    yield made["id"], made["key"]
    api.delete(f"/api/sw/keys/{made['id']}")
    _sql(f"DELETE FROM api_keys WHERE id = {made['id']}")


@pytest.fixture(autouse=True)
def _cleanup():
    """每条用例前后各清一次，避免上一轮残留影响下一轮的文件清单断言。"""
    _purge_marked_files()
    _purge_state()
    yield
    _purge_marked_files()
    _purge_state()


atexit.register(lambda: (_purge_marked_files(), _purge_state()))


# ---------------- 样本构造 ----------------
class _FakeReq:
    """最小 Request 替身：端点只用到 client.host。"""
    class client:
        host = "127.0.0.1"


class FakeConn:
    """最小 DB 连接替身：端点会写审计，这里全部吞掉不落库。"""
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): return None
        def fetchone(self): return None
        def fetchall(self): return []
    def cursor(self): return FakeConn._Cur()
    def commit(self): pass
    def rollback(self): pass


ADMIN_USER = {"id": 4, "username": "admin", "real_name": "测试",
              "role": "admin", "auth_type": "session",
              "projects": {"惠民社区保障资金项目": "write"}}


def _jpeg(size=(1200, 800), color=None) -> bytes:
    """生成 JPEG。color 默认随运行时间变化 —— 照片上传按 sha256 去重，
    内容每次都一样的话，重跑时会被判成「之前传过」而 saved=0。"""
    if color is None:
        t = int(time.time() * 1000)
        color = (t % 256, (t // 256) % 256, (t // 65536) % 256)
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _png_bomb() -> bytes:
    """9000×9000 纯色 PNG：压缩后只有几十 KB，字节预算拦不住，必须走像素门。"""
    buf = io.BytesIO()
    Image.new("L", (9000, 9000), 0).save(buf, "PNG")
    data = buf.getvalue()
    assert len(data) < 500 * 1024, "样本必须是「小体积巨图」，否则测不到像素门"
    return data


def _zip(entries: dict[str, bytes], name="现场照片.zip") -> tuple[dict, bytes]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in entries.items():
            zf.writestr(arc, data)
    return {"archive": (name, buf.getvalue())}, buf.getvalue()


def _act_snapshot() -> dict:
    """活动目录文件清单：文件名 -> sha256，用于验证上传确实落盘。"""
    out = {}
    if ACT_DIR.is_dir():
        for p in ACT_DIR.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                out[str(p.relative_to(ACT_DIR))] = hashlib.sha256(
                    p.read_bytes()).hexdigest()
    return out


def _proj_snapshot() -> set[str]:
    return {str(p.relative_to(PROJ_DIR)) for p in PROJ_DIR.rglob("*")
            if p.is_file()}


def _error_code(resp: requests.Response) -> str:
    body = resp.json()
    return str((body.get("error") or {}).get("code") or body.get("detail"))


# ================= 1. capabilities =================
def test_capabilities_shape_without_auth():
    """能力自述不需要认证，且结构稳定：版本 + 端点清单 + 错误码表。"""
    resp = requests.get(f"{BASE}/api/sw/capabilities", timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["version"], str) and body["version"]
    assert body["auth"]["header"].startswith("Authorization:")
    assert body["permissions"]["model"]

    paths = {(e["method"], e["path"]) for e in body["endpoints"]}
    assert ("GET", "/api/sw/projects") in paths
    assert ("POST", "/api/sw/run") in paths
    assert ("POST", "/api/sw/batch") in paths
    # capabilities 本身不应出现在自描述清单里（原来就是如此，回归防护）
    assert ("GET", "/api/sw/capabilities") not in paths

    for code in ("TEMPLATE_NOT_FOUND", "CODE_MISSING", "READONLY", "FORBIDDEN"):
        assert code in body["errors"]


# ================= 2. 身份 / 项目 / 活动查询 =================
def test_me_reports_identity_and_projects(api: Api, temp_key):
    """API Key 调 /me：auth_type=api_key，项目授权与 Web 会话一致。"""
    kid, plain = temp_key
    resp = Api({"Authorization": f"Bearer {plain}"}).get("/api/sw/me")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert body["auth_type"] == "api_key"
    assert PROJECT in body["projects"]

    web = api.get("/api/sw/me").json()
    assert web["auth_type"] == "session"
    assert web["projects"] == body["projects"]


def test_projects_lists_visible_with_access(api: Api):
    """列项目：含 access 字段，普通账号拿不到未授权项目。"""
    resp = api.get("/api/sw/projects")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = [it["name"] for it in body["items"]]
    assert PROJECT in names
    assert all(it["access"] in ("readonly", "write") for it in body["items"])
    assert any(it["name"] == PROJECT and it["access"] == "write"
               for it in body["items"])
    assert body["total"] == len(body["items"])


def test_activities_and_detail(api: Api):
    """活动列表 + 详情：编号 / 状态 / 文件数 / 15 项核对。"""
    resp = api.get(f"/api/sw/projects/{PROJECT}/activities")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 返回的是分组字典 {planned, existing, missing, extra}，不是扁平列表
    assert isinstance(body, dict), body
    assert {"planned", "existing", "missing", "extra"} <= set(body)
    assert ACTIVITY in (body.get("existing") or []), body.get("existing")

    resp = api.get(f"/api/sw/projects/{PROJECT}/activities/{ACTIVITY}")
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["dirname"] == ACTIVITY
    assert detail["code"], "真实活动应有编号"
    assert detail["status"] in ("generated", "imported", "checked", "reviewed",
                                "deliverable", "")
    assert isinstance(detail["files"], list) and isinstance(detail["dirs"], list)
    assert len(detail["check"]["items"]) == 15


def test_unknown_project_is_404_machine_readable(api: Api):
    """不存在的项目 → 404 + PROJECT_NOT_FOUND（不是 HTML，也不是 500）。"""
    resp = api.get("/api/sw/projects/不存在的词目/activities")
    assert resp.status_code == 404, resp.text
    assert _error_code(resp) == "PROJECT_NOT_FOUND"

    resp = api.get("/api/sw/projects/不存在的项目/activities/不存在的第一期")
    assert resp.status_code == 404, resp.text


def test_search_returns_results(api: Api):
    """检索真实关键词，文档台账 / Cognee 至少一路给结果。"""
    resp = api.get("/api/sw/search", params={"q": "共治", "limit": 10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "共治"
    assert isinstance(body["results"], list) and body["results"]
    assert all(r.get("source") for r in body["results"])

    empty = api.get("/api/sw/search", params={"q": "", "limit": 5})
    assert empty.status_code == 400, empty.text


# ================= 3. tasks/{id} 必须 501 =================
@pytest.mark.parametrize("task_id", ["1", "abc", "00000000-0000-0000-0000-000000000000"])
def test_tasks_endpoint_always_501(api: Api, task_id: str):
    """异步任务未接入：任意 id 都是 501 TASKS_NOT_IMPLEMENTED，绝不是 200/404。"""
    resp = api.get(f"/api/sw/tasks/{task_id}")
    assert resp.status_code == 501, resp.text
    assert _error_code(resp) == "TASKS_NOT_IMPLEMENTED"


# ================= 4. keys CRUD =================
def test_keys_create_once_list_and_revoke(api: Api, temp_key):
    """创建 → 明文只随创建响应给出一次 → 列表不含哈希 → 吊销后 401。

    与工单字面「列表不含明文」的差异：本仓库 `GET /api/sw/keys` 对 **admin**
    会返回 `secret`（解密后的完整明文，供用户管理页查看；docs/API-使用说明.md
    与 openapi 描述均如此声明）。因此这里按真实契约断言：
      1) 创建响应给出完整明文，长度/前缀固定；
      2) 列表项不含 `key_hash`，任何字段都不含 sha256(明文)；
      3) 非 admin（viewer）只能看到自己的 key，看不到别人的明文。
    """
    kid, plain = temp_key
    assert plain.startswith("sk-sw-") and len(plain) == len("sk-sw-") + 43

    listed = api.get("/api/sw/keys")
    assert listed.status_code == 200, listed.text
    item = next(x for x in listed.json()["items"] if x["id"] == kid)
    assert item["name"] == KEY_NAME
    assert item["prefix"] == plain[:12]
    assert item["revoked"] is False and item["active"] is True
    assert "key_hash" not in item, "列表项绝不能带哈希字段"
    blob = json.dumps(listed.json(), ensure_ascii=False)
    assert hashlib.sha256(plain.encode()).hexdigest() not in blob

    revoked = api.delete(f"/api/sw/keys/{kid}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] is True

    after = api.get("/api/sw/keys").json()
    item = next(x for x in after["items"] if x["id"] == kid)
    assert item["revoked"] is True and item["active"] is False

    dead = Api({"Authorization": f"Bearer {plain}"}).get("/api/sw/me")
    assert dead.status_code == 401, dead.text

    # 重复吊销不报错，但 revoked=false（幂等：已吊销就不再「新吊销」）
    again = api.delete(f"/api/sw/keys/{kid}")
    assert again.status_code == 200 and again.json()["revoked"] is False


def test_keys_delete_missing_is_404(api: Api):
    resp = api.delete("/api/sw/keys/9999999")
    assert resp.status_code == 404, resp.text
    assert _error_code(resp) == "KEY_NOT_FOUND"


# ================= 5. run / batch =================
@_skip_write
def test_run_template_dry_run_and_real(api: Api):
    """正常生成：dry_run 预演 filled>0 且不落盘；实跑落盘并真的写入。"""
    dry = api.post("/api/sw/run", json_body={
        "project": PROJECT, "activity": ACTIVITY,
        "template": "活动目录", "output": FILE_MARK, "dry_run": True})
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["ok"] is True and body["dry_run"] is True
    assert body["filled"] > 0, body
    assert not (ACT_DIR / FILE_MARK).exists(), "dry_run 不该落盘"

    before = _act_snapshot()
    real = api.post("/api/sw/run", json_body={
        "project": PROJECT, "activity": ACTIVITY,
        "template": "活动目录", "output": FILE_MARK, "dry_run": False})
    assert real.status_code == 200, real.text
    out = real.json()
    assert out["ok"] is True and out["dry_run"] is False
    assert out["filled"] > 0
    dest = ACT_DIR / FILE_MARK
    assert dest.is_file() and dest.stat().st_size > 0
    after = _act_snapshot()
    assert FILE_MARK not in before and FILE_MARK in after
    # 输出名带 .. 必须被安全边界挡掉，不许写到活动目录之外
    traversal = api.post("/api/sw/run", json_body={
        "project": PROJECT, "activity": ACTIVITY,
        "template": "活动目录", "output": "../escape.docx", "dry_run": True})
    assert traversal.status_code == 400, traversal.text


@_skip_write
def test_run_task_llm_fills_template():
    """task 走 LLM：生成内容真的被填进模板并落盘。

    直接调用端点函数（仓库既有习惯）：LLM 服务是进程内模块，
    monkeypatch 才生效。走 HTTP 的话服务是独立 uvicorn 进程，
    桩打不到它内部，会真的调用上游 LLM —— 实测单次 32~128 秒且不稳定，
    跑全量时必然超时。
    """
    sys.path.insert(0, str(REPO / "api"))
    from api.routes import agent as _agent
    from engine import llm as _llm_mod

    fake = {k: "单元测试生成的文本" for k in ("简报导语", "诉求描述", "达到目标")}
    fake["_check"] = {"ok": True, "score": 100}
    fake["_rewrites"] = 0

    out_name = f"{MARK}brief.docx"
    target = ACT_DIR / out_name
    orig_write = _llm_mod.write
    # 显式钉住模板目录：其它测试可能 monkeypatch 过 api.routes.misc.TEMPLATES_DIR，
    # 而 agent 模块持有自己的副本，全量跑时容易读到临时目录（实测 404 简报）。
    # 模板真实来源是 shared.domain.template_library() → _root()/templates/library。
    # 前面某些用例会 monkeypatch state.ROOT 指向开发仓，_root() 于是取到不存在的
    # 目录（实测全量跑时 404「模板不存在：简报」）。这里临时把 ROOT 钉回生产根。
    _real_root = Path("/srv/social-work")
    if not (_real_root / "templates" / "library").is_dir():
        pytest.skip(f"真实模板目录不存在：{_real_root}/templates/library")
    import api.state as _state
    _orig_tpl = getattr(_state, "ROOT", None)
    _state.ROOT = _real_root

    _llm_mod.write = lambda *a, **k: dict(fake)
    try:
        res = _agent.api_sw_run(
            {"project": PROJECT, "activity": ACTIVITY,
             "task": "brief", "output": out_name},
            _FakeReq(), FakeConn(), ADMIN_USER)
    finally:
        _llm_mod.write = orig_write
        if _orig_tpl is not None:
            _state.ROOT = _orig_tpl

    assert res.get("ok") is True, res
    assert res["filled"] > 0, res
    assert res["delivery"]["complete"] is True, res["delivery"]
    assert target.is_file(), f"未落盘：{target}"
    target.unlink(missing_ok=True)

@_skip_write
def test_run_unknown_template_404(api: Api):
    resp = api.post("/api/sw/run", json_body={
        "project": PROJECT, "activity": ACTIVITY,
        "template": "不存在的模板", "dry_run": True})
    assert resp.status_code == 404, resp.text
    assert _error_code(resp) == "TEMPLATE_NOT_FOUND"


@_skip_write
def test_run_missing_activity_and_template_400(api: Api):
    """不存在的活动 → 404 ACTIVITY_NOT_FOUND；缺 template/task → 400 VALIDATION。"""
    bad_act = api.post("/api/sw/run", json_body={
        "project": PROJECT, "activity": "99-不存在-第一期",
        "template": "活动目录", "dry_run": True})
    assert bad_act.status_code == 404 and _error_code(bad_act) == "ACTIVITY_NOT_FOUND"

    missing = api.post("/api/sw/run", json_body={
        "project": PROJECT, "activity": ACTIVITY, "dry_run": True})
    assert missing.status_code == 400 and _error_code(missing) == "VALIDATION"


@_skip_write
def test_run_missing_code_is_code_missing(api: Api):
    """活动缺编号：写权限通过后仍必须 400 CODE_MISSING，而不是生成半截文件。"""
    resp = api.post("/api/sw/run", json_body={
        "project": PROJECT, "activity": "01-走访调研-第一期",
        "template": "活动目录", "dry_run": True})
    assert resp.status_code == 400, resp.text
    assert _error_code(resp) == "CODE_MISSING"


def test_batch_check_all_activities(api: Api):
    """批量核对：逐项返回状态，缺编号的活动标 generatable=false。"""
    resp = api.post("/api/sw/batch", json_body={
        "action": "check", "project": PROJECT})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "check"
    assert body["failed"] == 0, body["results"]
    by_name = {r["activity"]: r for r in body["results"]}
    assert by_name[ACTIVITY]["status"] == "ok"
    assert by_name[ACTIVITY]["detail"]["generatable"] is True
    # 真实数据里多数活动没配 code，批量结果必须如实标注
    assert any(not r["detail"]["generatable"] for r in body["results"])

    bad = api.post("/api/sw/batch", json_body={"action": "nope"})
    assert bad.status_code == 400 and _error_code(bad) == "VALIDATION"

    unknown = api.post("/api/sw/batch", json_body={
        "action": "check", "project": "不存在的项目"})
    assert unknown.status_code == 404 and _error_code(unknown) == "PROJECT_NOT_FOUND"


@_skip_write
def test_batch_generate_one_activity(api: Api):
    """批量生成：指定 activities 时只处理该活动，输出 filled>0 且真的落盘。"""
    resp = api.post("/api/sw/batch", json_body={
        "action": "generate", "project": PROJECT,
        "activities": [ACTIVITY],
        "params": {"template": "活动目录", "output": f"{MARK}batch.docx"}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1 and body["ok"] == 1, body
    row = body["results"][0]
    assert row["status"] == "ok" and row["detail"]["filled"] > 0
    assert (ACT_DIR / f"{MARK}batch.docx").is_file()


@_skip_write
def test_batch_upload_writes_file(api: Api):
    """批量上传：base64 落盘到活动内已存在的子目录；重名第二次 FILE_EXISTS。"""
    payload = base64.b64encode(b"hello sw batch").decode()
    body_req = {"action": "upload", "project": PROJECT,
                "activities": [ACTIVITY],
                "params": {"sub": "13-其他佐证", "files": [
                    {"name": f"{MARK}batch.txt", "content_base64": payload}]}}
    first = api.post("/api/sw/batch", json_body=body_req)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] == 1, body
    assert body["results"][0]["detail"]["uploaded"] == [f"{MARK}batch.txt"]
    dest = ACT_DIR / "13-其他佐证" / f"{MARK}batch.txt"
    assert dest.read_text() == "hello sw batch"

    second = api.post("/api/sw/batch", json_body=body_req)
    row = second.json()["results"][0]
    assert row["status"] == "failed" and row["detail"]["code"] == "FILE_EXISTS"


@_skip_write
def test_batch_upload_rejects_unknown_subdir(api: Api):
    """sub 必须已存在且无 ..：非法路径 → 该项 failed VALIDATION，不落盘。"""
    payload = base64.b64encode(b"escape").decode()
    req = {"action": "upload", "project": PROJECT,
           "activities": [ACTIVITY],
           "params": {"sub": "../逃逸", "files": [
               {"name": f"{MARK}esc.txt", "content_base64": payload}]}}
    resp = api.post("/api/sw/batch", json_body=req)
    assert resp.status_code == 200, resp.text
    row = resp.json()["results"][0]
    assert row["status"] == "failed" and row["detail"]["code"] == "VALIDATION"
    assert not (ACT_DIR.parent / f"{MARK}esc.txt").exists()

    missing = api.post("/api/sw/batch", json_body={
        "action": "upload", "project": PROJECT, "activities": [ACTIVITY],
        "params": {"sub": "不存在的子目录", "files": [
            {"name": f"{MARK}x.txt", "content_base64": payload}]}})
    assert missing.json()["results"][0]["detail"]["code"] == "VALIDATION"


# ================= 6. 活动照片上传 =================
def test_photos_precheck_new_and_duplicated(api: Api):
    """预检只比对不落盘：全 new → 重复哈希进 duplicated，批次重复给 duplicate_batch。

    precheck 的哈希按**原始字节**比对，所以样本统一用 compress=false 上传，
    否则服务端落盘的是重编码字节，预检认不出来。
    """
    jpeg = _jpeg()
    digest = hashlib.sha256(jpeg).hexdigest()
    unknown = hashlib.sha256(b"never-uploaded").hexdigest()

    pre = api.post("/api/sw/photos/precheck", json_body={
        "project": PROJECT, "activity": ACTIVITY, "hashes": [unknown]})
    assert pre.status_code == 200, pre.text
    assert pre.json()["new"] == 1 and pre.json()["duplicated"] == []

    up = api.post("/api/sw/photos/upload", data={
        "project": PROJECT, "activity": ACTIVITY,
        "shot_date": _shot_date(), "scene": SCENE, "compress": "false"},
        files={"files": (PHOTO_MARK, jpeg, "image/jpeg")})
    assert up.status_code == 200, up.text
    assert up.json()["saved"] == 1

    again = api.post("/api/sw/photos/precheck", json_body={
        "project": PROJECT, "activity": ACTIVITY,
        "hashes": [digest, unknown], "shot_date": _shot_date(), "scene": SCENE})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["new"] == 1 and digest in body["duplicated"]
    assert body["duplicate_batch"], "同日期同场景已有批次，应给出上次批次号"

    bad = api.post("/api/sw/photos/precheck", json_body={
        "project": PROJECT, "activity": ACTIVITY, "hashes": "not-a-list"})
    assert bad.status_code == 400 and _error_code(bad) == "VALIDATION"


@_skip_write
def test_photos_upload_pixel_limit(api: Api):
    """像素超限：小体积巨图必须 IMAGE_TOO_LARGE（字节上限拦不住它）。"""
    resp = api.post("/api/sw/photos/upload", data={
        "project": PROJECT, "activity": ACTIVITY,
        "shot_date": "2026-11-10", "scene": SCENE},
        files={"files": (f"{MARK}bomb.png", _png_bomb(), "image/png")})
    assert resp.status_code == 400, resp.text
    assert _error_code(resp) == "IMAGE_TOO_LARGE"


@_skip_write
def test_photos_upload_rejects_non_image(api: Api):
    resp = api.post("/api/sw/photos/upload", data={
        "project": PROJECT, "activity": ACTIVITY,
        "shot_date": "2026-11-10", "scene": SCENE},
        files={"files": (f"{MARK}fake.jpg", b"not an image at all",
                         "image/jpeg")})
    assert resp.status_code == 400, resp.text
    assert _error_code(resp) == "NOT_AN_IMAGE"

    wrong_ext = api.post("/api/sw/photos/upload", data={
        "project": PROJECT, "activity": ACTIVITY,
        "shot_date": "2026-11-10", "scene": SCENE},
        files={"files": ("说明.txt", b"hello", "text/plain")})
    assert wrong_ext.status_code == 400, wrong_ext.text
    assert _error_code(wrong_ext) == "NOT_AN_IMAGE"

@_skip_write
def test_photos_upload_dedups_identical_bytes(api: Api):
    """compress=false 时哈希就是原始字节：同字节重复上传 → saved=0 / skipped=1。

    这正是「同一张图不许落两份」的内容门（L2）；压缩路径下哈希是重编码字节，
    内容门按字节比对，所以这里必须用 compress=false 才能稳定复现。
    """
    # 内容必须每次不同：否则第一次就撞上历史 sha256，saved=0 直接断言失败
    jpeg = _jpeg(color=(int(time.time() * 977) % 256, 3, 7))
    common = {"project": PROJECT, "activity": ACTIVITY,
              "shot_date": _shot_date(), "scene": SCENE, "compress": "false"}
    first = api.post("/api/sw/photos/upload", data=common,
                     files={"files": (PHOTO_MARK, jpeg, "image/jpeg")})
    assert first.status_code == 200, first.text
    fb = first.json()
    assert fb["saved"] == 1, fb   # 上一次运行残留会被 _purge_photo_meta 清掉

    # 第二次同日期同场景 → L3 预检直接 409（这是协议设计：写之前就挡）。
    again = api.post("/api/sw/photos/upload", data=common,
                     files={"files": (PHOTO_MARK, jpeg, "image/jpeg")})
    assert again.status_code == 409, again.text
    assert _error_code(again) == "DUPLICATE_BATCH"

    # 换一个日期绕过 L3，才走得到 L2 内容去重：同字节 → saved=0 / duplicate。
    other_day = common | {"shot_date": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")}
    third = api.post("/api/sw/photos/upload", data=other_day,
                     files={"files": (PHOTO_MARK, jpeg, "image/jpeg")})
    assert third.status_code == 200, third.text
    body = third.json()
    assert body["saved"] == 0, body
    assert len(body["skipped"]) == 1 and body["skipped"][0]["reason"] == "duplicate"

    # 显式允许重复批次：服务端会再存一份（当前契约如此），断言磁盘上确实多一个文件。
    # 这是协议有意保留的「强制重传」语义，不是漏洞；记录在此便于回归。
    forced = api.post("/api/sw/photos/upload", data={**common,
                                                    "allow_duplicate": "true"},
                      files={"files": (PHOTO_MARK, jpeg, "image/jpeg")})
    assert forced.status_code == 200, forced.text
    assert forced.json()["saved"] == 1


@_skip_write
def test_photos_upload_too_many_files(api: Api):
    """超 200 张：TOO_MANY_FILES。用小尺寸 JPEG，201 张即可触发。"""
    small = _jpeg(size=(8, 8))
    files = [("files", (f"{i}.jpg", small, "image/jpeg")) for i in range(201)]
    resp = api.post("/api/sw/photos/upload", data={
        "project": PROJECT, "activity": ACTIVITY, "shot_date": "2026-11-10"},
        files=files)
    assert resp.status_code == 400, resp.text
    assert _error_code(resp) == "TOO_MANY_FILES"


@_skip_write
def test_photos_upload_happy_path_and_repeat_guard(api: Api):
    """真实 JPEG 上传成功落盘并写 _photos.json；同日同场景再传走 DUPLICATE_BATCH。"""
    jpeg = _jpeg()
    common = {"project": PROJECT, "activity": ACTIVITY,
              "shot_date": _shot_date(), "scene": SCENE}
    first = api.post("/api/sw/photos/upload", data=common,
                     files={"files": (PHOTO_MARK, jpeg, "image/jpeg")})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] is True and body["saved"] == 1
    assert body["paths"] and body["paths"][0].startswith("11-活动照片资料/")
    assert (ACT_DIR / body["paths"][0]).is_file()

    meta = json.loads((ACT_DIR / "_photos.json").read_text(encoding="utf-8"))
    assert meta["photos"] and meta["hashes"]

    second = api.post("/api/sw/photos/upload", data=common,
                      files={"files": (PHOTO_MARK, jpeg, "image/jpeg")})
    assert second.status_code == 409, second.text
    assert _error_code(second) == "DUPLICATE_BATCH"


@_skip_write
def test_photos_upload_bad_shot_date(api: Api):
    """拍摄日期必填 / 格式错 / 晚于今天，三种都 400 VALIDATION。"""
    jpeg = _jpeg()
    for bad in ("", "2026/11/10", "2099-01-01"):
        resp = api.post("/api/sw/photos/upload", data={
            "project": PROJECT, "activity": ACTIVITY, "shot_date": bad},
            files={"files": (PHOTO_MARK, jpeg, "image/jpeg")})
        assert resp.status_code == 400, (bad, resp.text)
        assert _error_code(resp) == "VALIDATION"


# ================= 7. zip 归档上传 =================
@_skip_write
def test_photos_archive_happy_path(api: Api):
    """真实 zip：一级目录 = 活动目录名（前缀匹配），落盘张数与返回一致。

    前缀匹配单独验：只写序号前缀（如 '99'）也能落到同名活动目录。
    """
    entries = {
        f"{ACTIVITY}/{MARK}a.jpg": _jpeg(color=(1, 1, 1)),
        f"{ACTIVITY}/{MARK}b.jpg": _jpeg(color=(2, 2, 2)),
        f"{ACTIVITY}/{MARK}c.jpg": _jpeg(color=(3, 3, 3)),
    }
    files, _raw = _zip(entries)
    before = _act_snapshot()
    # 用本条用例专属的日期（同一用例内后续预检要能对上同一个日期）
    day = _shot_date()
    resp = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": day, "scene": SCENE},
        files=files)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["matched"] == {ACTIVITY: 3}
    assert body["unmatched"] == {}
    assert body["total_saved"] == 3
    after = _act_snapshot()
    assert sum(1 for k in after if k not in before) == 3

    # 前缀目录名：只给序号前缀，服务端按前缀匹配到完整活动目录名
    _seq = ACTIVITY.split("-")[0]
    prefix_files, _ = _zip({f"{_seq}/__swfullp.jpg": _jpeg(color=(4, 4, 4))})
    # 换一个日期：同一用例内第二次上传，同日期会被 L3 判成重复批次
    prefix_day = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    pre = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": prefix_day, "scene": SCENE}, files=prefix_files)
    assert pre.status_code == 200, pre.text
    assert pre.json()["matched"] == {ACTIVITY: 1}

    # 非 zip 后缀 / 后缀是 zip 但内容不是 → 都 ARCHIVE_UNSUPPORTED
    non_zip = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE},
        files={"archive": ("现场照片.rar", b"PK\x03\x04junk",
                           "application/x-rar")})
    assert non_zip.status_code == 400, non_zip.text
    assert _error_code(non_zip) == "ARCHIVE_UNSUPPORTED"

    misnamed = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE},
        files={"archive": ("叫 zip 的假包.zip", b"PK\x03\x04junk",
                           "application/zip")})
    assert misnamed.status_code == 400, misnamed.text
    assert _error_code(misnamed) == "ARCHIVE_UNSUPPORTED"


@_skip_write
def test_photos_archive_mixed_matched_and_unmatched(api: Api):
    """一部分目录认得出、一部分认不出：认出的落盘，认不出的进 unmatched，仍 200。"""
    entries = {f"{ACTIVITY}/{MARK}m.jpg": _jpeg(color=(9, 9, 9)),
               f"别的活动/{MARK}n.jpg": _jpeg(color=(10, 10, 10))}
    files, _raw = _zip(entries)
    # 用本条用例专属的日期（同一用例内后续预检要能对上同一个日期）
    day = _shot_date()
    resp = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": day, "scene": SCENE},
        files=files)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matched"] == {ACTIVITY: 1}
    assert body["unmatched"] == {"别的活动": 1}
    assert body["total_saved"] == 1


@_skip_write
def test_photos_archive_all_unmatched_is_400(api: Api):
    """全都认不出活动 → 400 NO_MATCHED_ACTIVITY，一张都不落盘。"""
    files, _raw = _zip({f"别的活动/{MARK}x.jpg": _jpeg(color=(9, 9, 9))})
    before = _act_snapshot()
    resp = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE},
        files=files)
    assert resp.status_code == 400, resp.text
    assert _error_code(resp) == "NO_MATCHED_ACTIVITY"
    assert _act_snapshot() == before

    # 图片直接放在压缩包根目录（没有一级目录）同样算认不出
    root_files, _ = _zip({f"{MARK}root.jpg": _jpeg(color=(11, 11, 11))})
    root = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE}, files=root_files)
    assert root.status_code == 400 and _error_code(root) == "NO_MATCHED_ACTIVITY"
    assert _act_snapshot() == before


@_skip_write
def test_photos_archive_empty_is_400(api: Api):
    """zip 里没有图片：ARCHIVE_EMPTY，不是 200 静默成功。"""
    files, _raw = _zip({f"{ACTIVITY}/说明.txt": b"hello"})
    resp = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE},
        files=files)
    assert resp.status_code == 400, resp.text
    assert _error_code(resp) == "ARCHIVE_EMPTY"

    # 隐藏文件与非图扩展名都不算图片
    hidden, _ = _zip({f"{ACTIVITY}/.hidden.jpg": _jpeg(color=(12, 12, 12)),
                      f"{ACTIVITY}/说明.txt": b"hi"})
    h = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE}, files=hidden)
    assert h.status_code == 400 and _error_code(h) == "ARCHIVE_EMPTY"

    # __MACOSX 目录整体跳过，不算 unmatched
    mac, _ = _zip({"__MACOSX/._x.jpg": _jpeg(color=(13, 13, 13))})
    m = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE}, files=mac)
    assert m.status_code == 400 and _error_code(m) == "ARCHIVE_EMPTY"


@_skip_write
def test_photos_archive_duplicate_batch_is_409(api: Api):
    """同日已传过 → 409 DUPLICATE_BATCH（L3 预检，写之前就挡）。"""
    files, _raw = _zip({f"{ACTIVITY}/{MARK}d1.jpg": _jpeg(color=(14, 14, 14))})
    first = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE}, files=files)
    assert first.status_code == 200, first.text

    again = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE}, files=files)
    assert again.status_code == 409, again.text
    assert _error_code(again) == "DUPLICATE_BATCH"


@_skip_write
def test_photos_archive_small_bomb_png(api: Api):
    """zip 里的小体积巨图：像素门必须挡住（与直接上传同一道门）。"""
    files, _raw = _zip({f"{ACTIVITY}/{MARK}bomb.png": _png_bomb()})
    resp = api.post("/api/sw/photos/upload-archive", data={
        "project": PROJECT, "shot_date": _shot_date(), "scene": SCENE},
        files=files)
    assert resp.status_code == 400, resp.text
    assert _error_code(resp) == "IMAGE_TOO_LARGE"


@_skip_write
def test_photos_archive_missing_project_is_422(api: Api):
    """缺 project 表单字段 → FastAPI 422（不是 500，也不落盘）。"""
    files, _raw = _zip({f"{ACTIVITY}/{MARK}z.jpg": _jpeg(color=(15, 15, 15))})
    resp = api.post("/api/sw/photos/upload-archive", data={
        "shot_date": "2026-09-17"}, files=files)
    assert resp.status_code == 422, resp.text
    assert "Traceback" not in resp.text


# ================= 8. 项目级文件上传 =================
@_skip_write
def test_project_files_upload_matches_checklist(api: Api):
    """命中清单项（文件名带「申报书」→ 项目方案书）→ 落 报告/，并回报 matched.item。"""
    before = _proj_snapshot()
    resp = api.post("/api/sw/project-files/upload", data={
        "project": PROJECT},
        files={"file": (f"{MARK}申报书.docx", b"fake docx bytes",
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stored"] == f"报告/{MARK}申报书.docx", body
    assert body["matched"] == {"item": 2, "name": "项目方案书"}, body
    assert body["size"] == len(b"fake docx bytes")
    assert body["stored"] in (_proj_snapshot() - before)

    unlisted = api.post("/api/sw/project-files/upload", data={
        "project": PROJECT},
        files={"file": (f"{MARK}杂项记录.docx", b"x", "application/octet-stream")})
    assert unlisted.status_code == 200, unlisted.text
    body = unlisted.json()
    assert body["stored"] == f"其他/{MARK}杂项记录.docx", body
    assert body["matched"] is None
    # suggestion 是「推荐归到哪一项」的结构，可能为 None（服务端未给出建议）
    if body.get("suggestion") is not None:
        assert isinstance(body["suggestion"], dict)

    dup = api.post("/api/sw/project-files/upload", data={"project": PROJECT},
                   files={"file": (f"{MARK}申报书.docx", b"y",
                                   "application/octet-stream")})
    assert dup.status_code == 409, dup.text
    assert _error_code(dup) == "DUPLICATE_NAME"


@_skip_write
def test_project_files_upload_rejects_hidden_and_traversal(api: Api):
    """隐藏文件（以 . 开头）与 .. 路径 → 400 VALIDATION，不落盘。"""
    before = _proj_snapshot()
    resp = api.post("/api/sw/project-files/upload", data={"project": PROJECT},
                     files={"file": (".hidden.docx", b"x",
                                     "application/octet-stream")})
    assert resp.status_code == 400, resp.text
    assert _error_code(resp) == "VALIDATION"

    traversal = api.post("/api/sw/project-files/upload", data={"project": PROJECT},
                         files={"file": ("../escape.docx", b"x",
                                         "application/octet-stream")})
    assert traversal.status_code == 400, traversal.text
    assert _proj_snapshot() == before

    unknown = api.post("/api/sw/project-files/upload",
                       data={"project": "不存在的项目"},
                       files={"file": (f"{MARK}u.docx", b"x",
                                       "application/octet-stream")})
    assert unknown.status_code == 404, unknown.text
    assert _error_code(unknown) == "PROJECT_NOT_FOUND"


# ================= 9. 权限 =================
def test_viewer_api_key_cannot_write(api: Api):
    """viewer 账号 + 项目只读授权：写端点 403（角色门先于项目门）。

    viewer 是本文件真实新建的账号（随机用户名，用例结束物理删除）。
    授权只给 readonly，用来证明「能读」与「不能写」的分界。
    """
    username = VIEWER_NAME
    _sql("INSERT INTO users(username, password_hash, role, enabled, real_name) "
         f"VALUES ('{username}', 'x', 'viewer', true, '测试-Viewer')")
    uid = _sql(f"SELECT id FROM users WHERE username = '{username}'")
    _sql(f"INSERT INTO user_projects(user_id, project, access) "
         f"VALUES ({uid}, '{PROJECT}', 'readonly')")

    # 给 viewer 签一把自己的 Key：POST /api/sw/keys 收 user 参数（admin 可代签）
    made = api.post("/api/sw/keys",
                    json_body={"user": username, "name": f"{MARK}viewer"})
    assert made.status_code == 200, made.text
    kid, plain = made.json()["id"], made.json()["key"]
    key_api = Api({"Authorization": f"Bearer {plain}"})
    try:
        me = key_api.get("/api/sw/me")
        assert me.status_code == 200, me.text
        assert me.json()["role"] == "viewer"
        assert me.json()["projects"] == {PROJECT: "readonly"}

        # 读得到（有授权）
        assert key_api.get("/api/sw/projects").status_code == 200
        assert key_api.get(f"/api/sw/projects/{PROJECT}/activities").status_code == 200

        # 写不了：角色门（viewer 非 editor/admin）
        denied = key_api.post("/api/sw/run", json_body={
            "project": PROJECT, "activity": ACTIVITY,
            "template": "活动目录", "output": f"{MARK}denied.docx",
            "dry_run": True})
        assert denied.status_code == 403, denied.text
        assert _error_code(denied) == "FORBIDDEN"

        denied_up = key_api.post("/api/sw/photos/upload", data={
            "project": PROJECT, "activity": ACTIVITY, "shot_date": "2026-11-10"},
            files={"files": (PHOTO_MARK, _jpeg(), "image/jpeg")})
        assert denied_up.status_code == 403, denied_up.text

        denied_arch = key_api.post("/api/sw/photos/upload-archive", data={
            "project": PROJECT, "shot_date": "2026-11-10"},
            files={"archive": ("现场照片.zip", b"PK\x03\x04", "application/zip")})
        assert denied_arch.status_code == 403, denied_arch.text

        denied_proj = key_api.post("/api/sw/project-files/upload", data={
            "project": PROJECT},
            files={"file": (PROJ_MARK, b"x", "application/octet-stream")})
        assert denied_proj.status_code == 403, denied_proj.text

        denied_keys = key_api.get("/api/sw/keys")
        assert denied_keys.status_code == 200, denied_keys.text
        assert all(k["user"] == username for k in denied_keys.json()["items"])
    finally:
        api.delete(f"/api/sw/keys/{kid}")
        _sql(f"DELETE FROM api_keys WHERE id = {kid}")


def test_editor_with_readonly_project_cannot_write(api: Api):
    """editor 角色但项目只有 readonly：写端点 403 READONLY（项目级门）。"""
    username = f"{MARK}editor{uuid.uuid4().hex[:6]}"
    _sql("INSERT INTO users(username, password_hash, role, enabled, real_name) "
         f"VALUES ('{username}', 'x', 'editor', true, '测试-Editor')")
    uid = _sql(f"SELECT id FROM users WHERE username = '{username}'")
    _sql(f"INSERT INTO user_projects(user_id, project, access) "
         f"VALUES ({uid}, '{PROJECT}', 'readonly')")
    made = api.post("/api/sw/keys",
                    json_body={"user": username, "name": f"{MARK}editor"})
    assert made.status_code == 200, made.text
    kid, plain = made.json()["id"], made.json()["key"]
    key_api = Api({"Authorization": f"Bearer {plain}"})
    try:
        me = key_api.get("/api/sw/me").json()
        assert me["role"] == "editor" and me["projects"] == {PROJECT: "readonly"}
        denied = key_api.post("/api/sw/run", json_body={
            "project": PROJECT, "activity": ACTIVITY,
            "template": "活动目录", "output": f"{MARK}denied.docx",
            "dry_run": True})
        assert denied.status_code == 403, denied.text
        assert _error_code(denied) == "READONLY"
    finally:
        api.delete(f"/api/sw/keys/{kid}")
        _sql(f"DELETE FROM api_keys WHERE id = {kid}")


def test_api_key_cannot_manage_users(api: Api, temp_key):
    """管理面（用户管理 / 审计）不允许 API Key，即便它是 admin 身份。"""
    _, plain = temp_key
    key_api = Api({"Authorization": f"Bearer {plain}"})
    assert key_api.get("/api/admin/users").status_code == 403
    assert key_api.get("/api/admin/audit").status_code == 403
    assert api.get("/api/admin/users").status_code == 200


def test_no_token_is_401(api: Api, temp_key):
    """无 token / 无效 Key / 无效 Key 混在有效 x-token 里，全部 401。"""
    assert Api().get("/api/sw/projects").status_code == 401

    bad = Api({"Authorization": "Bearer sk-sw-this-is-not-a-real-key"}).get(
        "/api/sw/projects")
    assert bad.status_code == 401, bad.text
    assert _error_code(bad) == "UNAUTHORIZED"

    # 带了 sk-sw- 前缀的错误 Key 不会退回同请求里的有效 x-token
    mixed = Api({"Authorization": "Bearer sk-sw-this-is-not-a-real-key",
                 "x-token": api.headers["x-token"]}).get("/api/sw/projects")
    assert mixed.status_code == 401, mixed.text


def test_sw_endpoints_do_not_leak_traceback(api: Api):
    """意外参数也不许吐 500 HTML；/api/sw/* 统一机器可读错误体。"""
    resp = api.get("/api/sw/projects/../../etc/passwd/activities")
    assert resp.status_code in (400, 404), resp.text
    assert "Traceback" not in resp.text
    assert "error" in resp.json() or "detail" in resp.json()
