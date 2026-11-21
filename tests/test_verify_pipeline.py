"""工单 A：端到端业务链路验证（生成 → 核对 → 归档）。

全部用 requests 调用 http://127.0.0.1:8001 的真实 HTTP 服务，不做任何 mock。
认证复用 sessions 表里的会话 token（x-token 头），与 Web 前端一致。

覆盖三条链路：
  1. 模板生成：/api/sw/run（预演、落盘、模板不存在、参数校验）
  2. 材料核对：.../review（核对记录、签字、参数校验、撤销）
  3. 归档核对：.../check 与活动详情（15 项、状态取值、deliverable、code）

破坏性操作（生成文件、写 review）统一在 fixture 清理；写 review 前还会先确认
真实数据本来没有该条记录，避免误清掉别人的核对结果。
"""
from __future__ import annotations

import atexit
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import requests

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from verify_guard import pytestmark  # noqa: E402,F401  生产验证套件守卫

# ---------- 服务端真实数据 ----------
BASE = os.environ.get("SW_TEST_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
PROJECT = "惠民社区保障资金项目"       # token 对应的 admin 对该项目有 write 权限
ACTIVITY = "11-共治行动-第一期"         # 真实活动：有编号、15 项核对、材料较齐全
TIMEOUT = 30

# 服务端项目根目录；与 engine/projects.py 的默认值保持一致
PROJECTS_ROOT = Path(os.environ.get("SOCIAL_WORK_PROJECTS", "/srv/social-work/projects"))
ACT_DIR = PROJECTS_ROOT / PROJECT / "活动" / ACTIVITY
FILE_MARK = "__verify_pipeline_"       # 本文件生成文件的统一前缀，清理时只删这个前缀

# 直连库拿 token（工单给定的连接方式）
PG_ENV = {**os.environ, "PGPASSWORD": "tjfjyjr"}
PSQL = ["psql", "-h", "127.0.0.1", "-U", "admin", "-d", "socialwork"]


@pytest.fixture(scope="module")
def token() -> str:
    """取最新会话 token；查不到或已过期就补插一条两小时有效的。"""
    def _query(sql: str) -> str:
        out = subprocess.run(PSQL + ["-Atc", sql], env=PG_ENV,
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    tok = _query("select token from sessions order by expires_at desc limit 1")
    if tok:
        probe = requests.get(f"{BASE}/api/me", headers={"x-token": tok},
                             timeout=TIMEOUT)
        if probe.status_code == 200:
            return tok
    # 兜底：user_id=4 是当前 admin；token 仅由时间戳和随机串组成，无 SQL 注入风险
    tok = f"verify-tok-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        PSQL + ["-c", "INSERT INTO sessions(user_id,token,expires_at) "
                       f"VALUES(4,'{tok}',now()+interval '2 hours')"],
        env=PG_ENV, capture_output=True, text=True, check=True)
    # 自己建的 session 自己删：否则每跑一轮就往库里漏一条（实测漏了 13 条）
    atexit.register(_drop_session, tok)
    return tok


def _drop_session(tok: str) -> None:
    """删掉本文件插入的 session；token 为正则约束过的安全串。"""
    if not tok or not re.fullmatch(r"verify-tok-\d+-[0-9a-f]{8}", tok):
        return
    try:
        subprocess.run(PSQL + ["-c", f"DELETE FROM sessions WHERE token='{tok}'"],
                       env=PG_ENV, capture_output=True, text=True, timeout=15)
    except Exception:
        pass


@pytest.fixture
def api(token):
    """带 x-token 的真实 HTTP 会话；先自检身份，token 失效立刻报错。"""
    session = requests.Session()
    session.headers.update({"x-token": token})
    resp = session.get(f"{BASE}/api/me", timeout=TIMEOUT)
    assert resp.status_code == 200, f"token 不可用：{resp.status_code} {resp.text}"
    return session


def _activity_url(suffix: str = "") -> str:
    return f"{BASE}/api/projects/{PROJECT}/activities/{ACTIVITY}{suffix}"


@pytest.fixture
def clean_reviews(api):
    """登记写过的核对项，用例结束统一 clear=true 撤销。"""
    items: list[int] = []
    yield items.append
    for no in set(items):
        api.post(_activity_url("/review"), json={"item": no, "clear": True},
                 timeout=TIMEOUT)


@pytest.fixture
def clean_files():
    """登记落盘文件，用例结束直接删；只删带本文件前缀且位于目标活动的。"""
    paths: list[Path] = []
    yield paths.append
    for path in set(paths):
        if path.name.startswith(FILE_MARK) and path.parent == ACT_DIR:
            path.unlink(missing_ok=True)


def _check(api) -> dict:
    resp = api.get(_activity_url("/check"), timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _item(api, no: int) -> dict:
    return next(it for it in _check(api)["items"] if it["no"] == no)


def _assert_unreviewed(api, no: int) -> None:
    """写之前确认真实数据没有该条核对，避免撤销时误删已有记录。"""
    if _item(api, no).get("review") is not None:
        pytest.fail(f"第 {no} 项已有核对记录，停止用例以免污染真实数据")


# ================= 链路 1：模板生成 =================

def test_run_activity_catalog_dry_run_filled(api):
    """活动目录模板预演：200、ok、filled>0。"""
    resp = api.post(f"{BASE}/api/sw/run", json={
        "project": PROJECT, "activity": ACTIVITY,
        "template": "活动目录", "dry_run": True}, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ok"] is True
    assert out["filled"] > 0
    assert out["dry_run"] is True
    assert out["path"].endswith("活动目录.docx")


def test_run_juesuan_biao_dry_run_ok(api):
    """同一活动换决算表模板：同样返回 200。"""
    resp = api.post(f"{BASE}/api/sw/run", json={
        "project": PROJECT, "activity": ACTIVITY,
        "template": "决算表", "dry_run": True}, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["dry_run"] is True


def test_run_unknown_template_404(api):
    """不存在的模板：404 且错误码为 TEMPLATE_NOT_FOUND。"""
    resp = api.post(f"{BASE}/api/sw/run", json={
        "project": PROJECT, "activity": ACTIVITY,
        "template": "不存在的模板", "dry_run": True}, timeout=TIMEOUT)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "TEMPLATE_NOT_FOUND"


def test_run_dry_run_does_not_write_file(api, clean_files):
    """预演模式指定唯一输出名，确认服务端没有真的落盘。"""
    name = f"{FILE_MARK}dry-{uuid.uuid4().hex[:8]}.docx"
    dest = ACT_DIR / name
    clean_files(dest)  # 先登记，万一实现有 bug 误落盘也能清掉
    resp = api.post(f"{BASE}/api/sw/run", json={
        "project": PROJECT, "activity": ACTIVITY, "template": "活动目录",
        "dry_run": True, "output": name}, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_run"] is True
    assert not dest.exists(), f"dry_run 不应写文件：{dest}"


def test_run_real_file_lands_on_disk(api, clean_files):
    """dry_run=false 指定唯一输出名，文件确实落到活动目录且非空。"""
    name = f"{FILE_MARK}real-{uuid.uuid4().hex[:8]}.docx"
    dest = ACT_DIR / name
    clean_files(dest)  # 请求发出前先登记，异常也能清理
    resp = api.post(f"{BASE}/api/sw/run", json={
        "project": PROJECT, "activity": ACTIVITY, "template": "活动目录",
        "dry_run": False, "output": name}, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_run"] is False
    assert dest.is_file(), f"生成后未落盘：{dest}"
    assert dest.stat().st_size > 0


def test_run_missing_project_activity_400(api):
    """缺 project/activity 必填参数：400 VALIDATION。"""
    resp = api.post(f"{BASE}/api/sw/run", json={
        "template": "活动目录", "dry_run": True}, timeout=TIMEOUT)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION"


# ================= 链路 2：材料核对（PR-01） =================

def test_review_item1_records_reviewer(api, clean_reviews):
    """标记 item=1：200，record.reviewed_by 非空。"""
    _assert_unreviewed(api, 1)
    clean_reviews(1)  # 结束撤销
    resp = api.post(_activity_url("/review"), json={
        "item": 1, "note": "端到端核对"}, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    record = resp.json()["record"]
    assert record.get("reviewed_by")
    assert record["signed"] is False


def test_check_reviewed_count_after_review(api, clean_reviews):
    """写完核对后，GET /check 的 reviewed_count 至少加一。"""
    _assert_unreviewed(api, 1)
    clean_reviews(1)
    before = _check(api)["reviewed_count"]
    resp = api.post(_activity_url("/review"), json={"item": 1}, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    after = _check(api)
    assert after["reviewed_count"] >= before + 1
    assert after["reviewed_count"] >= 1


def test_review_signed_without_scan_ok(api, clean_reviews):
    """signed=true 不传 scan：人工确认签字即可，允许后补扫描件。"""
    _assert_unreviewed(api, 8)
    clean_reviews(8)
    resp = api.post(_activity_url("/review"), json={
        "item": 8, "signed": True}, timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    record = resp.json()["record"]
    assert record["signed"] is True
    assert not record.get("scan")


def test_review_missing_scan_400(api):
    """signed=true 但 scan 指向不存在的文件：400。不会写入，无需清理。"""
    resp = api.post(_activity_url("/review"), json={
        "item": 8, "signed": True, "scan": "不存在的扫描件.jpg"}, timeout=TIMEOUT)
    assert resp.status_code == 400, resp.text


def test_review_bad_item_400(api):
    """item=99 超出 1-15：400。不会写入，无需清理。"""
    resp = api.post(_activity_url("/review"), json={"item": 99}, timeout=TIMEOUT)
    assert resp.status_code == 400, resp.text


def test_review_clear_removes_record(api, clean_reviews):
    """clear=true 撤销：reviews 中不再有该条，reviewed_count 回到原值。"""
    _assert_unreviewed(api, 1)
    clean_reviews(1)  # 兜底撤销；本用例自己也会 clear 一次（重复 clear 是幂等的）
    before = _check(api)["reviewed_count"]
    api.post(_activity_url("/review"), json={"item": 1, "note": "待撤销"},
             timeout=TIMEOUT)
    assert _item(api, 1).get("review") is not None

    resp = api.post(_activity_url("/review"), json={"item": 1, "clear": True},
                    timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["record"] == {}
    assert _item(api, 1).get("review") is None
    assert _check(api)["reviewed_count"] == before


def test_review_requires_auth():
    """未带 x-token 直接写核对：401。"""
    resp = requests.post(_activity_url("/review"), json={"item": 1},
                         timeout=TIMEOUT)
    assert resp.status_code == 401, resp.text


# ================= 链路 3：归档核对 =================

def test_check_returns_15_items_with_valid_status(api):
    """归档核对清单恰好 15 项，序号 1-15，状态取值合法。"""
    items = _check(api)["items"]
    assert len(items) == 15
    assert {it["no"] for it in items} == set(range(1, 16))
    assert all(it["status"] in {"ok", "partial", "missing", "na"} for it in items)


def test_check_deliverable_is_bool(api):
    """check 的 deliverable 必须是真正的 bool（不是字符串或 None）。"""
    assert isinstance(_check(api)["deliverable"], bool)


def test_activity_detail_has_code(api):
    """活动详情 200 且带 code 字段（生成材料的前置条件）。"""
    resp = api.get(_activity_url(), timeout=TIMEOUT)
    assert resp.status_code == 200, resp.text
    assert resp.json().get("code")
