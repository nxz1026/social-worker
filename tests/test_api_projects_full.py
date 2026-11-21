"""工单 A：projects + activities 域端点全测（真实 HTTP，不打 mock）。

覆盖工单列出的 26 条路径 / 32 个方法，另外补测 OpenAPI 中同域的 4 条端点
（captions/fill、PATCH check、photos、readme）。认证复用 sessions 表的 token
（x-token 头），与 Web 前端一致。

破坏性操作的清理策略（硬约束）：
  - 生成/上传的文件统一带 FILE_MARK 前缀，fixture 只删活动目录树内该前缀的文件；
  - 满意度汇总表、资料索引等固定名产物由 clean_paths 单独登记后删除；
  - review 写入前先确认原本没有记录，收尾用 clear=true 撤销；
  - activity.yaml / 项目 yaml / scores.json / 资料索引在改动前备份，收尾按字节还原；
  - 临时 viewer / outsider 账号通过管理接口硬删，级联清掉其 session 与授权。

LLM 上游在测试环境不可达（超时 240s × 3 次重试会长时间挂起），因此 /llm、
/llm-fill、/captions 只测参数与权限分支；其正常生成路径列在未测清单里。
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import uuid
from pathlib import Path

import pytest
import requests
import yaml

# ---------- 服务端真实数据 ----------
BASE = os.environ.get("SW_TEST_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
PROJECT = "惠民社区保障资金项目"
ACTIVITY = "11-共治行动-第一期"
PROJ_SEG = urllib.parse.quote(PROJECT, safe="")
ACT_SEG = urllib.parse.quote(ACTIVITY, safe="")
PROJ_URL = f"/api/projects/{PROJ_SEG}"
ACT_URL = f"{PROJ_URL}/activities/{ACT_SEG}"
TIMEOUT = 30

PROJECTS_ROOT = Path(os.environ.get("SOCIAL_WORK_PROJECTS", "/srv/social-work/projects"))
ACT_DIR = PROJECTS_ROOT / PROJECT / "活动" / ACTIVITY
PROJECT_YAML = PROJECTS_ROOT / PROJECT / f"{PROJECT}.yaml"
SCORES_JSON = ACT_DIR / "04-满意度调查表资料" / "scores.json"
README_MD = ACT_DIR / f"_{ACTIVITY}-资料索引.md"
SUMMARY_DOCX = ACT_DIR / "05-满意度测评汇总表_已填.docx"
FILE_MARK = "__projfull_a_"

PG_ENV = {**os.environ, "PGPASSWORD": "tjfjyjr"}
PSQL = ["psql", "-h", "127.0.0.1", "-U", "admin", "-d", "socialwork"]


def _req(session, method: str, path: str, **kw):
    """统一发请求：相对路径 + 超时，方便断言里直接看到状态与响应体。"""
    kw.setdefault("timeout", TIMEOUT)
    return session.request(method, BASE + path, **kw)


def _mark(name: str) -> str:
    """本文件生成的唯一文件名。"""
    return f"{FILE_MARK}{uuid.uuid4().hex[:8]}_{name}"


# ---------- 认证 / 临时账号 ----------

@pytest.fixture(scope="module")
def token() -> str:
    """按工单给定 SQL 取有效会话 token；取不到或失效直接失败，不自己造会话。"""
    out = subprocess.run(
        PSQL + ["-Atc", "select token from sessions where expires_at>now() limit 1"],
        env=PG_ENV, capture_output=True, text=True, check=True)
    tok = out.stdout.strip()
    assert tok, "sessions 表里没有有效 token"
    resp = requests.get(BASE + "/api/me", headers={"x-token": tok}, timeout=TIMEOUT)
    assert resp.status_code == 200, f"token 不可用：{resp.status_code} {resp.text}"
    return tok


@pytest.fixture
def api(token):
    """带 admin token 的会话，先自检身份。"""
    session = requests.Session()
    session.headers.update({"x-token": token})
    assert _req(session, "GET", "/api/me").status_code == 200
    return session


@pytest.fixture(scope="module")
def temp_users(token):
    """建临时 viewer（本项目只读）与 outsider（零项目）账号，模块结束硬删。"""
    admin = requests.Session()
    admin.headers.update({"x-token": token})
    ids: list[int] = []
    users: dict[str, requests.Session] = {}
    try:
        for key, role, projects in (
            ("viewer", "viewer", {PROJECT: "readonly"}),
            ("outsider", "editor", {}),
        ):
            username = f"__projfull_{key}_{uuid.uuid4().hex[:8]}__"
            made = _req(admin, "POST", "/api/admin/users", json={
                "username": username, "password": "pw-temp-123",
                "role": role, "real_name": "临时账号", "projects": projects,
            })
            assert made.status_code == 200, made.text
            uid = made.json()["id"]
            ids.append(uid)
            logged = requests.post(BASE + "/api/login", json={
                "username": username, "password": "pw-temp-123"}, timeout=TIMEOUT)
            assert logged.status_code == 200, logged.text
            session = requests.Session()
            session.headers.update({"x-token": logged.json()["token"]})
            users[key] = session
        yield users
    finally:
        # 硬删用户：外键级联清掉 sessions / user_projects，不留临时身份
        for uid in ids:
            try:
                _req(admin, "DELETE", f"/api/admin/users/{uid}")
            except Exception:
                pass


# ---------- 破坏性操作的清理 ----------

@pytest.fixture
def clean_paths():
    """登记本用例产生的文件/目录；只删活动目录树内的路径，越界一律跳过。"""
    paths: list[Path] = []

    def register(*raw):
        paths.extend(Path(p) for p in raw)

    yield register
    for path in set(paths):
        try:
            path.resolve().relative_to(ACT_DIR.resolve())
        except ValueError:
            continue                      # 活动目录之外的东西绝不删
        if path.is_file():
            path.unlink(missing_ok=True)
    # 空目录（回收站时间戳目录）最后再删，避免里面还有文件
    for path in sorted((p for p in set(paths) if p.is_dir()),
                       key=lambda p: -len(p.parts)):
        try:
            path.rmdir()
        except OSError:
            pass


@pytest.fixture
def restore_files():
    """改动前按字节备份，用例结束原样还原（不改内容只改 mtime 也可接受）。"""
    saved: list[tuple[Path, bytes | None]] = []

    def register(*raw):
        for item in raw:
            path = Path(item)
            saved.append((path, path.read_bytes() if path.is_file() else None))

    yield register
    for path, raw in saved:
        if raw is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(raw)


@pytest.fixture
def clean_reviews(api):
    """登记写过的核对项，结束统一 clear；写之前由 _assert_unreviewed 兜底。"""
    items: list[int] = []

    def register(no: int):
        items.append(no)

    yield register
    for no in set(items):
        _req(api, "POST", f"{ACT_URL}/review", json={"item": no, "clear": True})


def _assert_unreviewed(api, no: int) -> None:
    """写核对前确认真实数据没有该条记录，避免撤销时误删别人的结果。"""
    resp = _req(api, "GET", f"{ACT_URL}/check")
    assert resp.status_code == 200, resp.text
    item = next(it for it in resp.json()["items"] if it["no"] == no)
    assert item.get("review") is None, f"第 {no} 项已有核对记录，停止用例以免污染真实数据"


def _check(api) -> dict:
    resp = _req(api, "GET", f"{ACT_URL}/check")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ================= 1. GET /api/projects =================

def test_list_projects_ok(api):
    """正常：200，项目列表包含真实项目。"""
    resp = _req(api, "GET", "/api/projects")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == len(body["items"])
    assert PROJECT in {item["name"] for item in body["items"]}


def test_list_projects_outsider_sees_nothing(temp_users):
    """无项目权限：零项目账号列表为空，不能兜底成「全部可见」。"""
    resp = _req(temp_users["outsider"], "GET", "/api/projects")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"total": 0, "items": []}


# ================= 2. POST /api/projects =================

def test_create_project_bad_name_400(api):
    """参数错误：项目名带 ../ 越界 → 400，且不建目录（工单要求别造新项目）。"""
    resp = _req(api, "POST", "/api/projects", json={"name": "../evil"})
    assert resp.status_code == 400, resp.text
    assert not (PROJECTS_ROOT / "evil").exists()


def test_create_project_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", "/api/projects", json={"name": "x"})
    assert resp.status_code == 403, resp.text


# ================= 3. GET /api/projects/{name} =================

def test_get_project_ok(api):
    """正常：200，返回项目主数据。"""
    resp = _req(api, "GET", PROJ_URL)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == PROJECT
    assert resp.json()["has_config"] is True


def test_get_project_not_found_404(api):
    """参数错误：不存在的项目 → 404。"""
    resp = _req(api, "GET", f"/api/projects/{urllib.parse.quote('不存在的项目', safe='')}")
    assert resp.status_code == 404, resp.text


def test_get_project_outsider_403(temp_users):
    """无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", PROJ_URL)
    assert resp.status_code == 403, resp.text


# ================= 4. PUT /api/projects/{name} =================

def test_put_project_ok(api, restore_files):
    """正常：用原 yaml 整体覆盖写回，200；收尾按字节还原项目 yaml。"""
    restore_files(PROJECT_YAML)
    original = yaml.safe_load(PROJECT_YAML.read_text(encoding="utf-8"))
    resp = _req(api, "PUT", PROJ_URL, json=original)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert resp.json()["name"] == PROJECT


def test_put_project_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "PUT", PROJ_URL, json={"project": {}})
    assert resp.status_code == 403, resp.text


def test_put_project_not_found_404(api):
    """参数错误：没有 yaml 的项目 → 404。"""
    resp = _req(api, "PUT", f"/api/projects/{urllib.parse.quote('不存在的项目', safe='')}",
                json={"project": {}})
    assert resp.status_code == 404, resp.text


# ================= 5. GET /api/projects/{name}/activities =================

def test_list_activities_ok(api):
    """正常：200，计划/已存在/缺失/多余四个字段齐全，真实活动在磁盘清单里。"""
    resp = _req(api, "GET", f"{PROJ_URL}/activities")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(("planned", "existing", "missing", "extra")) <= set(body)
    assert ACTIVITY in body["existing"]


def test_list_activities_outsider_403(temp_users):
    """无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", f"{PROJ_URL}/activities")
    assert resp.status_code == 403, resp.text


# ================= 6. POST /api/projects/{name}/activities/split =================

def test_split_dry_run_ok(api):
    """正常：预演默认不落盘，返回 would_create / would_skip。"""
    resp = _req(api, "POST", f"{PROJ_URL}/activities/split", json={"dry_run": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert ACTIVITY in body["would_skip"]


def test_split_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{PROJ_URL}/activities/split",
                json={"dry_run": True})
    assert resp.status_code == 403, resp.text


# ================= 7. GET /api/projects/{name}/activities/{dirname} =================

def test_activity_detail_ok(api):
    """正常：200，带编号、状态和 15 项核对。"""
    resp = _req(api, "GET", ACT_URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dirname"] == ACTIVITY
    assert body["code"]
    assert len(body["check"]["items"]) == 15


def test_activity_detail_not_found_404(api):
    """参数错误：活动目录不存在 → 404。"""
    resp = _req(api, "GET", f"{PROJ_URL}/activities/不存在-999")
    assert resp.status_code == 404, resp.text


def test_activity_detail_outsider_403(temp_users):
    """无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", ACT_URL)
    assert resp.status_code == 403, resp.text


# ================= 8. PATCH /api/projects/{name}/activities/{dirname} =================

def test_patch_activity_rename_roundtrip(api):
    """正常：改名到临时名再改回来；任一步失败 fixture 都会强制还原。"""
    base = PROJECTS_ROOT / PROJECT / "活动"
    tmp_name = _mark("rename")
    holder = {"moved": False}
    try:
        resp = _req(api, "PATCH", ACT_URL, json={"new_name": tmp_name})
        assert resp.status_code == 200, resp.text
        holder["moved"] = True
        assert (base / tmp_name).is_dir()
        assert not (base / ACTIVITY).exists()

        back = _req(api, "PATCH", f"{PROJ_URL}/activities/{tmp_name}",
                    json={"new_name": ACTIVITY})
        assert back.status_code == 200, back.text
        holder["moved"] = False
        assert (base / ACTIVITY).is_dir()
    finally:
        if holder["moved"] and (base / tmp_name).is_dir() and not (base / ACTIVITY).exists():
            (base / tmp_name).rename(base / ACTIVITY)


def test_patch_activity_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "PATCH", ACT_URL, json={"new_name": "x"})
    assert resp.status_code == 403, resp.text


def test_patch_activity_empty_name_400(api):
    """参数错误：new_name 为空 → 400。"""
    resp = _req(api, "PATCH", ACT_URL, json={"new_name": "  "})
    assert resp.status_code == 400, resp.text


# ================= 9/10/15/16. files DELETE / GET、trash GET / restore POST =================

def test_files_list_ok(api):
    """GET /files 正常：200，返回一级条目列表。"""
    resp = _req(api, "GET", f"{ACT_URL}/files")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert items and any(item["name"] == "activity.yaml" for item in items)


def test_files_bad_sub_404(api):
    """GET /files 参数错误：不存在的子目录 → 404。"""
    resp = _req(api, "GET", f"{ACT_URL}/files", params={"sub": "根本不存在"})
    assert resp.status_code == 404, resp.text


def test_files_outsider_403(temp_users):
    """GET /files 无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", f"{ACT_URL}/files")
    assert resp.status_code == 403, resp.text


def test_trash_roundtrip(api, clean_paths):
    """重点：上传 → 删除进回收站 → 列表 → 恢复 → 原文件回来。"""
    filename = _mark("trash.txt")
    src = ACT_DIR / filename
    clean_paths(src)                      # 先登记，任何一步失败都能清掉
    uploaded = _req(api, "POST", f"{ACT_URL}/upload",
                    files={"file": (filename, b"trash-roundtrip", "text/plain")})
    assert uploaded.status_code == 200, uploaded.text
    assert src.is_file()

    deleted = _req(api, "DELETE", f"{ACT_URL}/files", json={"path": filename})
    assert deleted.status_code == 200, deleted.text
    trash_value = deleted.json()["trash"]
    assert trash_value.startswith(".trash/")
    trash_file = ACT_DIR / trash_value
    clean_paths(trash_file, trash_file.parent)   # 恢复后父目录为空，一并登记
    assert not src.exists()
    assert trash_file.is_file()

    listed = _req(api, "GET", f"{ACT_URL}/trash")
    assert listed.status_code == 200, listed.text
    hit = next(i for i in listed.json()["items"] if i["original"] == filename)
    assert hit["trash"] == trash_value

    restored = _req(api, "POST", f"{ACT_URL}/trash/restore", json={"trash": trash_value})
    assert restored.status_code == 200, restored.text
    assert restored.json()["path"] == filename
    assert src.read_bytes() == b"trash-roundtrip", "恢复后原文件内容应完好"


def test_trash_list_ok(api):
    """GET /trash 正常：200，items 是数组（当前回收站可为空）。"""
    resp = _req(api, "GET", f"{ACT_URL}/trash")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["items"], list)


def test_trash_list_outsider_403(temp_users):
    """GET /trash 无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", f"{ACT_URL}/trash")
    assert resp.status_code == 403, resp.text


def test_delete_file_viewer_403(temp_users):
    """DELETE /files 权限：viewer → 403。"""
    resp = _req(temp_users["viewer"], "DELETE", f"{ACT_URL}/files",
                json={"path": "activity.yaml"})
    assert resp.status_code == 403, resp.text


def test_delete_file_not_found_404(api):
    """DELETE /files 参数错误：文件不存在 → 404。"""
    resp = _req(api, "DELETE", f"{ACT_URL}/files", json={"path": "根本不存在.docx"})
    assert resp.status_code == 404, resp.text


def test_restore_viewer_403(temp_users):
    """POST /trash/restore 权限：viewer → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/trash/restore",
                json={"trash": ".trash/20260101-000000/a.txt"})
    assert resp.status_code == 403, resp.text


def test_restore_bad_path_400(api):
    """POST /trash/restore 参数错误：不在 .trash 里的路径 → 400。"""
    resp = _req(api, "POST", f"{ACT_URL}/trash/restore",
                json={"trash": "activity.yaml"})
    assert resp.status_code == 400, resp.text


def test_restore_missing_404(api):
    """POST /trash/restore 参数错误：回收站里没有该文件 → 404。"""
    resp = _req(api, "POST", f"{ACT_URL}/trash/restore",
                json={"trash": ".trash/20260101-000000/不存在.docx"})
    assert resp.status_code == 404, resp.text


# ================= 11. GET .../download =================

def test_download_ok(api):
    """正常：下载活动内真实文件，200 且内容非空。"""
    path = urllib.parse.quote("00-目录.md", safe="")
    resp = _req(api, "GET", f"{ACT_URL}/download", params={"path": path})
    assert resp.status_code == 200, resp.text
    assert len(resp.content) > 0


def test_download_traversal_400(api):
    """参数错误：编码上跳路径 → 400，不读磁盘。"""
    path = urllib.parse.quote("../../etc/passwd", safe="")
    resp = _req(api, "GET", f"{ACT_URL}/download", params={"path": path})
    assert resp.status_code == 400, resp.text


def test_download_missing_404(api):
    """参数错误：文件不存在 → 404。"""
    resp = _req(api, "GET", f"{ACT_URL}/download",
                params={"path": "根本不存在.docx"})
    assert resp.status_code == 404, resp.text


def test_download_outsider_403(temp_users):
    """无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", f"{ACT_URL}/download",
                params={"path": "00-目录.md"})
    assert resp.status_code == 403, resp.text


# ================= 12. POST .../upload =================

def test_upload_ok(api, clean_paths):
    """正常：上传 txt 到活动根，200 且返回 size/sha256，文件确实落盘。"""
    filename = _mark("upload.txt")
    src = ACT_DIR / filename
    clean_paths(src)                      # 请求前先登记，异常也能清掉
    resp = _req(api, "POST", f"{ACT_URL}/upload",
                files={"file": (filename, b"hello-upload", "text/plain")},
                data={"sub": ""})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == filename
    assert body["size"] == len(b"hello-upload")
    assert len(body["sha256"]) == 64
    assert src.read_bytes() == b"hello-upload"


def test_upload_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/upload",
                files={"file": (_mark("x.txt"), b"x", "text/plain")})
    assert resp.status_code == 403, resp.text


def test_upload_bad_ext_400(api):
    """参数错误：不允许的扩展名 → 400，且不落盘。"""
    filename = _mark("bad.exe")
    resp = _req(api, "POST", f"{ACT_URL}/upload",
                files={"file": (filename, b"x", "application/octet-stream")})
    assert resp.status_code == 400, resp.text
    assert not (ACT_DIR / filename).exists()


# ================= 13. GET .../check =================

def test_check_activity_ok(api):
    """正常：15 项核对，序号 1-15，状态值合法。"""
    items = _check(api)["items"]
    assert len(items) == 15
    assert {it["no"] for it in items} == set(range(1, 16))
    assert all(it["status"] in {"ok", "partial", "missing", "na"} for it in items)


def test_check_activity_outsider_403(temp_users):
    """无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", f"{ACT_URL}/check")
    assert resp.status_code == 403, resp.text


# ================= 14. POST .../review（PR-01） =================

def test_review_item_ok(api, clean_reviews):
    """正常：标记 item=1，返回核对人；收尾 clear 撤销。"""
    _assert_unreviewed(api, 1)
    clean_reviews(1)
    resp = _req(api, "POST", f"{ACT_URL}/review", json={"item": 1, "note": "全测"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["record"]["reviewed_by"]
    assert resp.json()["record"]["signed"] is False


def test_review_signed_without_scan_ok(api, clean_reviews):
    """重点：signed 不传 scan → 成功，人工确认签字允许后补扫描件。"""
    _assert_unreviewed(api, 8)
    clean_reviews(8)
    resp = _req(api, "POST", f"{ACT_URL}/review", json={"item": 8, "signed": True})
    assert resp.status_code == 200, resp.text
    record = resp.json()["record"]
    assert record["signed"] is True
    assert not record.get("scan")


def test_review_invalid_scan_400(api):
    """重点：scan 指向不存在的文件 → 400，不写入。"""
    resp = _req(api, "POST", f"{ACT_URL}/review", json={
        "item": 8, "signed": True, "scan": "不存在的扫描件.jpg"})
    assert resp.status_code == 400, resp.text


def test_review_bad_item_400(api):
    """重点：item 越界 → 400。"""
    resp = _req(api, "POST", f"{ACT_URL}/review", json={"item": 99})
    assert resp.status_code == 400, resp.text


def test_review_clear_removes_record(api, clean_reviews):
    """重点：clear 撤销，reviewed_count 回到原值。"""
    _assert_unreviewed(api, 1)
    clean_reviews(1)
    before = _check(api)["reviewed_count"]
    assert _req(api, "POST", f"{ACT_URL}/review", json={"item": 1}).status_code == 200
    assert next(it for it in _check(api)["items"] if it["no"] == 1)["review"]

    cleared = _req(api, "POST", f"{ACT_URL}/review", json={"item": 1, "clear": True})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["record"] == {}
    assert next(it for it in _check(api)["items"] if it["no"] == 1)["review"] is None
    assert _check(api)["reviewed_count"] == before


def test_review_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/review", json={"item": 1})
    assert resp.status_code == 403, resp.text


# ================= 17. POST .../fill =================

def test_fill_ok(api, clean_paths):
    """正常：指定唯一输出名渲染模板，200 且文件落盘；收尾删除。"""
    name = _mark("fill.docx")
    clean_paths(ACT_DIR / name)
    resp = _req(api, "POST", f"{ACT_URL}/fill",
                json={"template": "活动目录", "output": name})
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == name
    assert resp.json()["filled"] > 0
    assert (ACT_DIR / name).is_file()


def test_fill_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/fill",
                json={"template": "活动目录"})
    assert resp.status_code == 403, resp.text


def test_fill_bad_template_404(api):
    """参数错误：模板不存在 → 404。"""
    resp = _req(api, "POST", f"{ACT_URL}/fill",
                json={"template": "根本不存在的模板", "output": _mark("n.docx")})
    assert resp.status_code == 404, resp.text


# ================= 18. POST .../llm =================
# 说明：上游 LLM 不可达（240s × 3 重试），正常生成路径未执行，见文末未测清单。

def test_llm_missing_task_400(api):
    """参数错误：缺必填 task → 400。"""
    resp = _req(api, "POST", f"{ACT_URL}/llm", json={})
    assert resp.status_code == 400, resp.text


def test_llm_unknown_task_400(api):
    """参数错误：task 不在注册列表 → 400。"""
    resp = _req(api, "POST", f"{ACT_URL}/llm", json={"task": "不存在的任务"})
    assert resp.status_code == 400, resp.text


def test_llm_viewer_403(temp_users):
    """权限：viewer 写操作 → 403（在调用 LLM 之前就被拦下）。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/llm", json={"task": "summary"})
    assert resp.status_code == 403, resp.text


# ================= 19. POST .../llm-fill =================

def test_llm_fill_missing_task_400(api):
    """参数错误：缺必填 task → 400（不会触发上游 LLM）。"""
    resp = _req(api, "POST", f"{ACT_URL}/llm-fill", json={})
    assert resp.status_code == 400, resp.text


def test_llm_fill_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/llm-fill", json={"task": "summary"})
    assert resp.status_code == 403, resp.text


# ================= 20. POST .../captions（工单写 caption，实际路径 captions） =================

def test_captions_bad_picked_400(api):
    """参数错误：picked 不是数组 → 400（不会触发上游 LLM）。"""
    resp = _req(api, "POST", f"{ACT_URL}/captions", json={"picked": "不是数组"})
    assert resp.status_code == 400, resp.text


def test_captions_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/captions", json={})
    assert resp.status_code == 403, resp.text


# ================= 21. POST .../invoice/scan =================

def test_invoice_scan_ok(api):
    """正常：返回决算 / 报销 / 发票三项交叉校验。"""
    resp = _req(api, "POST", f"{ACT_URL}/invoice/scan")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(("决算", "报销", "发票")) <= set(body)
    assert body["发票"]["count"] == 3


def test_invoice_scan_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/invoice/scan")
    assert resp.status_code == 403, resp.text


# ================= 22. POST .../invoice/report =================

def test_invoice_report_ok(api, clean_paths):
    """正常：生成发票明细表，200 且 count/total 正确；收尾删除产物。"""
    name = f"12-发票/{_mark('report.xlsx')}"
    clean_paths(ACT_DIR / name)
    resp = _req(api, "POST", f"{ACT_URL}/invoice/report", json={"output": name})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 3
    assert body["total"] == 1662.0
    assert (ACT_DIR / name).is_file()


def test_invoice_report_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/invoice/report", json={})
    assert resp.status_code == 403, resp.text


# ================= 23. GET .../satisfaction =================

def test_satisfaction_get_ok(api):
    """正常：200，返回问卷分数条目数组。"""
    resp = _req(api, "GET", f"{ACT_URL}/satisfaction")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_satisfaction_get_outsider_403(temp_users):
    """无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", f"{ACT_URL}/satisfaction")
    assert resp.status_code == 403, resp.text


# ================= 24. PUT .../satisfaction =================

def test_satisfaction_put_ok(api, restore_files):
    """正常：写回 scores.json，200；收尾按字节还原原文件。"""
    restore_files(SCORES_JSON)
    # 用真实照片名写一条合法分数，避免凭空造条目
    entries = json.loads(SCORES_JSON.read_text(encoding="utf-8"))
    first = entries[0]["file"]
    resp = _req(api, "PUT", f"{ACT_URL}/satisfaction", json={
        "scores": [{"file": first, "score": 4.5, "satisfied": True}]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 1


def test_satisfaction_put_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "PUT", f"{ACT_URL}/satisfaction",
                json={"scores": []})
    assert resp.status_code == 403, resp.text


def test_satisfaction_put_bad_scores_400(api):
    """参数错误：scores 不是数组 → 400。"""
    resp = _req(api, "PUT", f"{ACT_URL}/satisfaction", json={"scores": "不是数组"})
    assert resp.status_code == 400, resp.text


# ================= 25. POST .../satisfaction/list =================

def test_satisfaction_list_ok(api, restore_files):
    """正常：扫描问卷生成/更新 scores.json，200；收尾还原文件。"""
    restore_files(SCORES_JSON)
    resp = _req(api, "POST", f"{ACT_URL}/satisfaction/list")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


def test_satisfaction_list_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/satisfaction/list")
    assert resp.status_code == 403, resp.text


# ================= 26. POST .../satisfaction/fill =================

def test_satisfaction_fill_ok(api, restore_files, clean_paths):
    """正常：先录一条分数再统计填表，200；收尾还原 scores.json 并删生成表。"""
    restore_files(SCORES_JSON)
    clean_paths(SUMMARY_DOCX)
    entries = json.loads(SCORES_JSON.read_text(encoding="utf-8"))
    recorded = _req(api, "PUT", f"{ACT_URL}/satisfaction", json={
        "scores": [{"file": entries[0]["file"], "score": 5, "satisfied": True}]})
    assert recorded.status_code == 200, recorded.text

    resp = _req(api, "POST", f"{ACT_URL}/satisfaction/fill")
    assert resp.status_code == 200, resp.text
    assert resp.json()["份数"] == 1
    assert SUMMARY_DOCX.is_file()


def test_satisfaction_fill_no_score_400(api, restore_files):
    """参数错误：一份分数都没录 → 400。"""
    restore_files(SCORES_JSON)
    entries = json.loads(SCORES_JSON.read_text(encoding="utf-8"))
    _req(api, "PUT", f"{ACT_URL}/satisfaction", json={
        "scores": [{"file": entries[0]["file"], "score": None}]})
    resp = _req(api, "POST", f"{ACT_URL}/satisfaction/fill")
    assert resp.status_code == 400, resp.text


def test_satisfaction_fill_viewer_403(temp_users):
    """权限：viewer 写操作 → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/satisfaction/fill")
    assert resp.status_code == 403, resp.text


# ================= 27. GET /api/projects/{name}/check =================

def test_project_check_ok(api):
    """正常：项目级核对返回 items 数组。"""
    resp = _req(api, "GET", f"{PROJ_URL}/check")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["items"], list)


def test_project_check_outsider_403(temp_users):
    """无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", f"{PROJ_URL}/check")
    assert resp.status_code == 403, resp.text


# ================= 28. GET /api/projects/{name}/code =================

def test_code_ok(api):
    """正常：返回 code + parsed，字段齐全。"""
    resp = _req(api, "GET", f"{PROJ_URL}/code",
                params={"act_type": 1, "act_month": 7})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"]
    assert body["parsed"]["act_type"] == 1
    assert body["parsed"]["act_month"] == 7


def test_code_not_persisted(api):
    """重点：编号是临时生成的，换 serial 结果不同，且不写回任何文件/目录。"""
    base = PROJECTS_ROOT / PROJECT
    before_names = {p.name for p in base.iterdir()}
    before_yaml = (ACT_DIR / "activity.yaml").read_bytes()
    a = _req(api, "GET", f"{PROJ_URL}/code",
             params={"act_type": 1, "act_month": 7, "serial": 101})
    b = _req(api, "GET", f"{PROJ_URL}/code",
             params={"act_type": 1, "act_month": 7, "serial": 202})
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    assert a.json()["code"] != b.json()["code"]
    assert a.json()["parsed"]["serial"] == 101
    assert b.json()["parsed"]["serial"] == 202
    assert {p.name for p in base.iterdir()} == before_names, "编号端点不应建目录"
    assert (ACT_DIR / "activity.yaml").read_bytes() == before_yaml, "编号端点不应改活动 yaml"


def test_code_missing_params_422(api):
    """参数错误：缺 act_type / act_month → 422。"""
    resp = _req(api, "GET", f"{PROJ_URL}/code")
    assert resp.status_code == 422, resp.text


def test_code_bad_act_type_400(api):
    """参数错误：act_type 不在项目配置 → 400。"""
    resp = _req(api, "GET", f"{PROJ_URL}/code",
                params={"act_type": 99, "act_month": 7})
    assert resp.status_code == 400, resp.text


def test_code_outsider_403(temp_users):
    """无项目权限：零项目账号读编号 → 403（viewer 只读，读编号是允许的）。"""
    resp = _req(temp_users["outsider"], "GET", f"{PROJ_URL}/code",
                params={"act_type": 1, "act_month": 7})
    assert resp.status_code == 403, resp.text


# ================= 补充：OpenAPI 中同域但工单清单未列的 4 条端点 =================

def test_captions_fill_ok(api, clean_paths):
    """POST captions/fill：人工给出图注即可渲染，不触发 LLM；收尾删产物。"""
    name = _mark("capfill.docx")
    clean_paths(ACT_DIR / name)
    resp = _req(api, "POST", f"{ACT_URL}/captions/fill", json={
        "captions": [{"file": "a.jpg", "caption": "居民共治现场"}],
        "template": "活动目录", "output": name})
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == name
    assert (ACT_DIR / name).is_file()


def test_captions_fill_bad_400(api):
    """POST captions/fill 参数错误：captions 为空 → 400。"""
    resp = _req(api, "POST", f"{ACT_URL}/captions/fill", json={"captions": []})
    assert resp.status_code == 400, resp.text


def test_check_set_na_ok(api, restore_files):
    """PATCH /check：标记不适用项，200；收尾还原 activity.yaml。"""
    restore_files(ACT_DIR / "activity.yaml")
    resp = _req(api, "PATCH", f"{ACT_URL}/check", json={"not_applicable": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["not_applicable"] == []


def test_check_set_na_bad_400(api):
    """PATCH /check 参数错误：not_applicable 不是数组 → 400。"""
    resp = _req(api, "PATCH", f"{ACT_URL}/check", json={"not_applicable": "不是数组"})
    assert resp.status_code == 400, resp.text


def test_check_set_na_viewer_403(temp_users):
    """PATCH /check 权限：viewer → 403。"""
    resp = _req(temp_users["viewer"], "PATCH", f"{ACT_URL}/check",
                json={"not_applicable": []})
    assert resp.status_code == 403, resp.text


def test_photos_ok(api):
    """GET /photos：200，返回照片条目（name/size/thumb）。"""
    resp = _req(api, "GET", f"{ACT_URL}/photos")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert items
    assert {"name", "size", "thumb"} <= set(items[0])


def test_photos_outsider_403(temp_users):
    """GET /photos 无项目权限：403。"""
    resp = _req(temp_users["outsider"], "GET", f"{ACT_URL}/photos")
    assert resp.status_code == 403, resp.text


def test_readme_ok(api, restore_files):
    """POST /readme：200，索引重生成；收尾按字节还原索引文件。"""
    restore_files(README_MD)
    resp = _req(api, "POST", f"{ACT_URL}/readme")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["written"], bool)


def test_readme_viewer_403(temp_users):
    """POST /readme 权限：viewer → 403。"""
    resp = _req(temp_users["viewer"], "POST", f"{ACT_URL}/readme")
    assert resp.status_code == 403, resp.text
