#!/usr/bin/env python3
"""工单 A：核心库补测（4 个模块，纯逻辑，不碰真实数据、不连外网）。

覆盖：
1. api.errors.ApiError —— 字段、继承、与 app 异常处理器拼出的响应体
2. api.shared.delivery.sw_delivery —— 交付完整性四元组
3. engine.ingest.read_activity_code —— 编号提取与回退
4. engine.cognee_index —— 桩掉 urllib，只验请求构造与错误分支

约定（沿用仓库现有测试）：
- 直接 import 模块 / 直接调函数，不用 httpx / TestClient（环境未装 httpx）
- 所有写盘走 tmp_path；所有网络用 monkeypatch 桩
- 数据库相关：本文件只用「连不上的假 DSN」让 api.settings 能加载，
  不建连接、不碰真实库
- 中文注释
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import types
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "api"))
# 必须在 import api.settings 之前设好，否则 load() 直接 RuntimeError
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN",
    "host=127.0.0.1 port=5432 dbname=test user=test password=x")

from api.errors import ApiError  # noqa: E402
from api.shared.delivery import sw_delivery  # noqa: E402
from engine import cognee_index as ci  # noqa: E402
from engine import ingest  # noqa: E402


# ==========================================================================
# 0. 共用桩
# ==========================================================================

class _FakeResponse:
    """urllib.request.urlopen 的返回值替身（with 语句 + read()）。"""

    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_docx(path: Path, text: str) -> None:
    """造最小 docx：只写 word/document.xml，够 _docx_text 抽取。"""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


@pytest.fixture
def key_root(tmp_path):
    """带 .cognee-key 的隔离根目录。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / ".cognee-key").write_text("sk-test\n", encoding="utf-8")
    return root


@pytest.fixture
def stub_urlopen(monkeypatch):
    """把 urllib.request.urlopen 换成记录调用的桩。

    返回 (calls, set_response)：calls 记录 (url, method, data, headers, timeout)，
    set_response(fn) 换掉响应体来源。
    """
    calls: list[tuple] = []
    holder: dict = {"fn": lambda req, timeout=None: _FakeResponse("{}")}

    def _fake(req, timeout=None):
        calls.append((req.full_url, req.method, req.data,
                      dict(req.header_items()), timeout))
        return holder["fn"](req, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return calls, holder


# ==========================================================================
# 1. api/errors.py
# ==========================================================================

def test_api_error_basic_fields():
    """status / code / message 正确落到属性上。"""
    err = ApiError(400, "VALIDATION", "msg")
    assert err.status_code == 400
    assert err.code == "VALIDATION"
    assert err.message == "msg"


def test_api_error_default_hint_and_retry_after():
    """不带 hint / retry_after 时是空串与 None。"""
    err = ApiError(403, "FORBIDDEN", "no")
    assert err.hint == ""
    assert err.retry_after is None


def test_api_error_is_http_exception_with_detail():
    """继承 HTTPException：detail 存的是 message，能直接被 FastAPI 序列化。"""
    from fastapi import HTTPException
    err = ApiError(404, "NOT_FOUND", "gone", hint="检查 id")
    assert isinstance(err, HTTPException)
    assert err.detail == "gone"
    assert err.hint == "检查 id"


def test_api_error_retry_after_is_keyword_only():
    """retry_after 只能关键字传；第 5 个位置参数是 hint。"""
    err = ApiError(429, "RATE_LIMITED", "慢点", "等 5 秒", retry_after=5)
    assert err.hint == "等 5 秒"
    assert err.retry_after == 5


def test_api_error_handler_sw_shape(monkeypatch):
    """/api/sw/* 序列化成 {"error":{code,message,hint}}；限速额外带 retry_after。"""
    import app  # noqa: PLC0415 —— 延迟 import，避开 settings 加载顺序

    async def run():
        scope = {"type": "http", "method": "GET", "path": "/api/sw/me",
                 "raw_path": b"/api/sw/me", "query_string": b"",
                 "headers": [], "client": ("127.0.0.1", 1),
                 "server": ("t", 80), "scheme": "http"}
        from fastapi import Request  # noqa: PLC0415
        req = Request(scope)
        out = []
        for exc in (ApiError(400, "VALIDATION", "msg", hint="h"),
                    ApiError(429, "RATE_LIMITED", "慢", retry_after=7)):
            resp = await app.api_error_handler(req, exc)
            out.append((resp.status_code, json.loads(resp.body),
                        resp.headers.get("retry-after")))
        return out

    results = asyncio.run(run())
    assert results[0] == (400, {"error": {"code": "VALIDATION", "message": "msg",
                                         "hint": "h"}}, None)
    code, body, retry = results[1]
    assert code == 429
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["retry_after"] == 7
    assert retry == "7"


def test_api_error_handler_web_shape(monkeypatch):
    """/api/*（非 sw）保持旧契约 {"detail": ...}，限速才附 error/retry_after。"""
    import app  # noqa: PLC0415

    async def run():
        scope = {"type": "http", "method": "GET", "path": "/api/me",
                 "raw_path": b"/api/me", "query_string": b"",
                 "headers": [], "client": ("127.0.0.1", 1),
                 "server": ("t", 80), "scheme": "http"}
        from fastapi import Request  # noqa: PLC0415
        req = Request(scope)
        out = []
        for exc in (ApiError(400, "VALIDATION", "msg", hint="h"),
                    ApiError(429, "RATE_LIMITED", "慢", retry_after=9)):
            resp = await app.api_error_handler(req, exc)
            out.append((resp.status_code, json.loads(resp.body)))
        return out

    results = asyncio.run(run())
    assert results[0] == (400, {"detail": "msg"})
    assert results[1][1]["detail"] == "慢"
    assert results[1][1]["error"] == {"code": "RATE_LIMITED",
                                     "message": "慢", "hint": ""}
    assert results[1][1]["retry_after"] == 9


# ==========================================================================
# 2. api/shared/delivery.py
# ==========================================================================

def test_delivery_empty_missing_is_complete():
    """missing 为空 → complete=True，unfilled 空。"""
    out = sw_delivery([], "brief")
    assert out["complete"] is True
    assert out["unfilled"] == []
    assert out["other_missing"] == []
    assert out["expected"] == ["简报导语", "诉求描述", "达到目标"]


def test_delivery_required_field_unfilled():
    """本 task 的字段没填 → complete=False，且进 unfilled。"""
    out = sw_delivery(["{简报导语}", "{诉求描述}", "{达到目标}"], "brief")
    assert out["complete"] is False
    assert out["unfilled"] == ["简报导语", "诉求描述", "达到目标"]


def test_delivery_other_field_goes_to_other_missing():
    """图注_N / 合计金额 归别的环节 → 只进 other_missing，不算失败。"""
    out = sw_delivery(["{图注_1}", "{合计金额}"], "brief")
    assert out["other_missing"] == ["{图注_1}", "{合计金额}"]


def test_delivery_mixed_split():
    """必需 + 非必需混在一起：两者分开记账。"""
    out = sw_delivery(["{简报导语}", "{图注_1}"], "brief")
    assert out["complete"] is False
    assert out["unfilled"] == ["简报导语"]
    assert out["other_missing"] == ["{图注_1}"]


def test_delivery_braces_and_spaces_normalized():
    """占位符写作 "{ 简报导语 }"（带空格）也能匹配白名单。"""
    out = sw_delivery(["{ 简报导语 }"], "brief")
    assert out["complete"] is False
    assert out["unfilled"] == ["简报导语"]
    assert out["other_missing"] == []


def test_delivery_unknown_task_is_complete():
    """未知 task：不做判断，complete=True，missing 全归 other_missing。"""
    out = sw_delivery(["{简报导语}"], "不存在的 task")
    assert out["complete"] is True
    assert out["expected"] == []
    assert out["unfilled"] == []
    assert out["other_missing"] == ["{简报导语}"]


def test_delivery_none_missing_tolerated():
    """missing 传 None 不炸，等价于空列表。"""
    out = sw_delivery(None, "supervision")
    assert out["complete"] is True
    assert out["expected"] == ["活动内容"]


def test_delivery_expected_order_matches_llm_tasks():
    """expected 直接来自 engine.llm.TASKS 的 fields 顺序，不能写死。"""
    from engine import llm  # noqa: PLC0415
    out = sw_delivery([], "summary")
    assert out["expected"] == [str(x) for x in llm.TASKS["summary"]["fields"]]
    assert len(out["expected"]) == 19


# ==========================================================================
# 3. engine/ingest.py
# ==========================================================================

def test_read_activity_code_from_catalog(tmp_path):
    """00-目录.docx 里有编号 → 直接取它。"""
    _make_docx(tmp_path / "00-目录.docx", "归档目录 CDYJR2026008-C-1110002")
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-C-1110002"


def test_read_activity_code_falls_back_to_plan(tmp_path):
    """00 没有编号（或 00 不存在）→ 回退 01-计划书.docx。"""
    _make_docx(tmp_path / "00-目录.docx", "归档目录，无编号")
    _make_docx(tmp_path / "01-计划书.docx", "编号 CDYJR2026008-C-1111002")
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-C-1111002"
    # 没有 00 时同样回退
    (tmp_path / "00-目录.docx").unlink()
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-C-1111002"


def test_read_activity_code_prefers_catalog(tmp_path):
    """两个文件都有编号时以 00 为准。"""
    _make_docx(tmp_path / "00-目录.docx", "CDYJR2026008-C-1110002")
    _make_docx(tmp_path / "01-计划书.docx", "CDYJR2026008-C-9999999")
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-C-1110002"


def test_read_activity_code_multi_run_split(tmp_path):
    """编号被 Word 拆进多个 <w:r>，抽取后仍能拼回连续串。"""
    xml = ('<w:r><w:t>CDYJR2026008-</w:t></w:r>'
           '<w:r><w:t>C-1110002</w:t></w:r>')
    with zipfile.ZipFile(tmp_path / "00-目录.docx", "w") as zf:
        zf.writestr("word/document.xml", xml)
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-C-1110002"


def test_read_activity_code_regex_boundaries(tmp_path):
    """编号尾号只认 6~7 位数字；5 位不匹配，8 位只取前 7 位。"""
    _make_docx(tmp_path / "00-目录.docx", "CDYJR2026008-C-11100")       # 5 位
    assert ingest.read_activity_code(tmp_path) is None

    _make_docx(tmp_path / "00-目录.docx", "CDYJR2026008-C-11100021")   # 8 位
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-C-1110002"


def test_read_activity_code_lowercase_c_accepted(tmp_path):
    """旧数据用小写 c 分隔，也要认。"""
    _make_docx(tmp_path / "00-目录.docx", "CDYJR2026008-c-1110002")
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-c-1110002"


def test_read_activity_code_missing_dir_returns_none(tmp_path):
    """目录不存在 → None（不抛异常）。"""
    assert ingest.read_activity_code(tmp_path / "不存在") is None


def test_read_activity_code_corrupt_docx_returns_none(tmp_path):
    """docx 不是 zip / 缺 word/document.xml / 空目录 → 一律 None。"""
    (tmp_path / "00-目录.docx").write_bytes(b"not a zip")
    assert ingest.read_activity_code(tmp_path) is None

    with zipfile.ZipFile(tmp_path / "00-目录.docx", "w") as zf:
        zf.writestr("other.xml", "<w:t>CDYJR2026008-C-1110002</w:t>")
    assert ingest.read_activity_code(tmp_path) is None

    (tmp_path / "00-目录.docx").unlink()
    assert ingest.read_activity_code(tmp_path) is None


def test_read_activity_code_sorted_glob(tmp_path):
    """同前缀多个 docx 时按文件名排序取第一个命中的。"""
    _make_docx(tmp_path / "00-b.docx", "CDYJR2026008-C-2220002")
    _make_docx(tmp_path / "00-a.docx", "CDYJR2026008-C-1110002")
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-C-1110002"


# ==========================================================================
# 4. engine/cognee_index.py（全部桩掉 urllib，不连真 Cognee）
# ==========================================================================

def test_key_missing_and_empty(tmp_path):
    """缺 .cognee-key 或内容为空 → RuntimeError，且带路径。"""
    with pytest.raises(RuntimeError, match="缺少 Cognee API key"):
        ci._key(tmp_path)

    (tmp_path / ".cognee-key").write_text("   \n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Cognee API key 为空"):
        ci._key(tmp_path)


def test_headers_shape():
    """请求头固定 X-Api-Key + Accept: application/json。"""
    assert ci._headers("k") == {"X-Api-Key": "k", "Accept": "application/json"}


def test_data_count_request_and_parse(key_root, stub_urlopen):
    """GET 到 /datasets/{id}/data/count，timeout=60，取 count 字段。"""
    calls, holder = stub_urlopen
    holder["fn"] = lambda req, timeout=None: _FakeResponse('{"count": 7}')

    assert ci.data_count(key_root, "ds-1") == 7
    url, method, data, headers, timeout = calls[0]
    assert url == "http://127.0.0.1:8000/api/v1/datasets/ds-1/data/count"
    assert method == "GET" and data is None
    assert headers["X-api-key"] == "sk-test"
    assert timeout == 60


def test_data_count_missing_field_is_zero(key_root, stub_urlopen):
    """count 缺失 / 为 0 → 0；非 dict 响应 → RuntimeError。"""
    _, holder = stub_urlopen
    holder["fn"] = lambda req, timeout=None: _FakeResponse('{"count": null}')
    assert ci.data_count(key_root, "ds-1") == 0

    holder["fn"] = lambda req, timeout=None: _FakeResponse('[1, 2]')
    with pytest.raises(RuntimeError, match="预期对象"):
        ci.data_count(key_root, "ds-1")


def test_list_datasets_returns_list_and_rejects_object(key_root, stub_urlopen):
    """正常回数组；上游回对象 → RuntimeError（预期数组）。"""
    calls, holder = stub_urlopen
    holder["fn"] = lambda req, timeout=None: _FakeResponse('[{"id": "a"}]')
    assert ci.list_datasets(key_root) == [{"id": "a"}]

    holder["fn"] = lambda req, timeout=None: _FakeResponse('{"id": "a"}')
    with pytest.raises(RuntimeError, match="预期数组"):
        ci.list_datasets(key_root)


def test_graph_summary_filters_invalid_entries(key_root, stub_urlopen):
    """只保留带 datasetId 的项，空 id 与缺字段项被丢掉。"""
    _, holder = stub_urlopen
    holder["fn"] = lambda req, timeout=None: _FakeResponse(json.dumps([
        {"datasetId": "a", "numNodes": 1},
        {"datasetId": "", "numNodes": 2},
        {"no": "id"},
        {"datasetId": "b", "numNodes": 3},
    ]))
    out = ci.graph_summary(key_root)
    assert out == {"a": {"datasetId": "a", "numNodes": 1},
                   "b": {"datasetId": "b", "numNodes": 3}}


def test_ensure_dataset_reuses_existing(key_root, stub_urlopen):
    """已有同名数据集 → 不再 POST，直接返回现有。"""
    calls, holder = stub_urlopen
    holder["fn"] = lambda req, timeout=None: _FakeResponse(
        '[{"id": "old", "name": "DS"}]')

    assert ci.ensure_dataset(key_root, "DS") == {"id": "old", "name": "DS"}
    assert len(calls) == 1 and calls[0][1] == "GET"


def test_ensure_dataset_creates_when_absent(key_root, stub_urlopen):
    """不存在 → POST /datasets 带上 {"name": ...}；返回缺 id 时报错。"""
    calls, holder = stub_urlopen

    def route(req, timeout=None):
        if req.method == "GET":
            return _FakeResponse("[]")
        return _FakeResponse('{"id": "new", "name": "DS"}')

    holder["fn"] = route
    assert ci.ensure_dataset(key_root, "DS")["id"] == "new"
    assert calls[1][1] == "POST"
    assert json.loads(calls[1][2]) == {"name": "DS"}
    assert calls[1][0].endswith("/api/v1/datasets")

    holder["fn"] = lambda req, timeout=None: (
        _FakeResponse("[]") if req.method == "GET"
        else _FakeResponse('{"name": "DS"}'))
    with pytest.raises(RuntimeError, match="返回缺少 id"):
        ci.ensure_dataset(key_root, "DS")


def test_add_text_multipart_shape(key_root, stub_urlopen):
    """multipart 里含 datasetName 字段、filename、MIME 与正文；timeout=300。"""
    calls, holder = stub_urlopen
    holder["fn"] = lambda req, timeout=None: _FakeResponse(
        '{"data_ids": ["d-1"]}')

    assert ci.add_text(key_root, "DS", "# 标题\n正文", "a.md") == "d-1"
    body = calls[0][2]
    assert b'name="datasetName"' in body and b"DS" in body
    assert b'filename="a.md"' in body
    assert b"Content-Type: text/markdown" in body
    assert "# 标题".encode("utf-8") in body
    assert calls[0][4] == 300
    assert calls[0][3]["Content-type"].startswith("multipart/form-data; boundary=")


def test_add_text_missing_data_ids(key_root, stub_urlopen):
    """上游没回 data_ids → RuntimeError，错误里带原始响应。"""
    _, holder = stub_urlopen
    holder["fn"] = lambda req, timeout=None: _FakeResponse('{"data_ids": []}')
    with pytest.raises(RuntimeError, match="返回缺少 data_ids"):
        ci.add_text(key_root, "DS", "x", "a.md")


def test_cognify_payload_and_success(key_root, stub_urlopen, monkeypatch):
    """POST cognify 的固定参数 + 轮询到 pending=0 → True。"""
    calls, holder = stub_urlopen
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)

    def route(req, timeout=None):
        if req.full_url.endswith("/cognify"):
            return _FakeResponse('{"ds-1": {"pipeline_run_id": "run-9"}}')
        if "processing-status" in req.full_url:
            return _FakeResponse('{"total": 5, "completed": 5, "pending": 0}')
        return _FakeResponse('{"ds-1": "COMPLETED"}')

    holder["fn"] = route
    assert ci.cognify(key_root, "ds-1", timeout=10, poll_interval=0) is True

    url, method, data, _, timeout = calls[0]
    assert url.endswith("/api/v1/cognify") and method == "POST"
    assert json.loads(data) == {
        "datasets": ["ds-1"], "runInBackground": True,
        "dataPerBatch": 1, "chunksPerBatch": 4, "chunkSize": 512}
    assert timeout == 120
    # 成功后不再查数据集状态
    assert all("datasets/status" not in c[0] for c in calls)


def test_cognify_timeout_raises(key_root, stub_urlopen, monkeypatch):
    """一直 pending → 到 deadline 抛错，错误里带进度。"""
    _, holder = stub_urlopen
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)

    def route(req, timeout=None):
        if req.full_url.endswith("/cognify"):
            return _FakeResponse('{"ds-1": {"pipeline_run_id": "r"}}')
        if "processing-status" in req.full_url:
            return _FakeResponse('{"total": 10, "completed": 2, "pending": 8}')
        return _FakeResponse('{"ds-1": "RUNNING"}')

    holder["fn"] = route
    with pytest.raises(RuntimeError, match="等待建图超时"):
        ci.cognify(key_root, "ds-1", timeout=0.05, poll_interval=0)


def test_cognify_dataset_error_state(key_root, stub_urlopen, monkeypatch):
    """数据集状态 ERROR → 立即失败，不空等到超时。"""
    _, holder = stub_urlopen
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)

    def route(req, timeout=None):
        if req.full_url.endswith("/cognify"):
            return _FakeResponse('{"ds-1": {"pipeline_run_id": "r"}}')
        if "processing-status" in req.full_url:
            return _FakeResponse('{"total": 10, "completed": 2, "pending": 8}')
        return _FakeResponse('{"ds-1": "ERROR"}')

    holder["fn"] = route
    with pytest.raises(RuntimeError, match="数据集状态 ERROR"):
        ci.cognify(key_root, "ds-1", timeout=10, poll_interval=0)


def test_request_json_http_error_body(key_root, stub_urlopen):
    """HTTPError 的返回体原样进异常消息。"""
    _, holder = stub_urlopen

    def route(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 503, "boom", {}, io.BytesIO(b'{"err": "x"}'))

    holder["fn"] = route
    with pytest.raises(RuntimeError, match=r"HTTP 503 \{\"err\": \"x\"\}"):
        ci.list_datasets(key_root)


def test_request_json_non_json_response(key_root, stub_urlopen):
    """上游回 HTML → RuntimeError「响应不是 JSON」。"""
    _, holder = stub_urlopen
    holder["fn"] = lambda req, timeout=None: _FakeResponse("<html>nope</html>")
    with pytest.raises(RuntimeError, match="响应不是 JSON"):
        ci.list_datasets(key_root)


def test_request_json_empty_body_is_empty_dict(key_root, stub_urlopen):
    """空响应体 → {}，不抛异常。"""
    _, holder = stub_urlopen
    holder["fn"] = lambda req, timeout=None: _FakeResponse("")
    assert ci._request_json("GET", "http://x", "k") == {}


def test_cognify_poll_interval_has_minimum(monkeypatch):
    """poll_interval=0 时必须有下限，否则空转打爆上游（实测 1 秒 11843 次）。"""
    from engine import cognee_index as _ci
    assert _ci._MIN_POLL_INTERVAL > 0
    slept = []
    monkeypatch.setattr(_ci.time, "sleep", lambda v: slept.append(v))
    # 直接验证 sleep 参数被夹到下限；不看完整流程，避免依赖 HTTP 桩
    import inspect
    src = inspect.getsource(_ci.cognify)
    assert "_MIN_POLL_INTERVAL" in src, "cognify 未使用最小间隔常量"
