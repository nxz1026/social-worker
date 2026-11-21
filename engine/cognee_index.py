#!/usr/bin/env python3
"""Cognee HTTP 客户端：数据集管理 + 文本入库 + 建图。

路径以容器实际 openapi.json 为准（2026-11 探测结果）：
  GET  /api/v1/datasets
  POST /api/v1/datasets
  POST /api/v1/add        （multipart：data 文件 + datasetName）
  POST /api/v1/cognify    （JSON：datasets 列表）

所有失败都把上游返回体打到 stderr 并抛 RuntimeError，不吞异常。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

COGNEE_BASE = "http://127.0.0.1:8000"


def _key(root: Path) -> str:
    """读 <root>/.cognee-key。"""
    key_file = Path(root) / ".cognee-key"
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"缺少 Cognee API key：{key_file}") from exc
    if not key:
        raise RuntimeError(f"Cognee API key 为空：{key_file}")
    return key


def _headers(key: str) -> dict[str, str]:
    return {"X-Api-Key": key, "Accept": "application/json"}


def _fail(what: str, body: str) -> RuntimeError:
    """统一处理上游错误：先原样打出返回体，再返回异常。"""
    sys.stderr.write(f"[cognee] {what} 失败，上游返回：\n{body}\n")
    sys.stderr.flush()
    return RuntimeError(f"{what} 失败：{body}")


def _request_json(method: str, url: str, key: str, *,
                  payload: dict | None = None, timeout: int = 60) -> dict | list:
    """发 JSON 请求并解析响应。"""
    data = None
    headers = _headers(key)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise _fail(f"{method} {url}", f"HTTP {exc.code} {body}") from exc
    except Exception as exc:
        raise _fail(f"{method} {url}", repr(exc)) from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _fail(f"{method} {url}", f"响应不是 JSON：{raw[:500]}") from exc


def _post_multipart(url: str, key: str, fields: dict[str, str],
                    files: list[tuple[str, str, bytes, str]],
                    timeout: int = 120) -> dict:
    """手工拼 multipart/form-data：fields 是普通字段，files 是 (字段名, 文件名, 内容, MIME)。"""
    boundary = "----cognee" + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                 f"{value}\r\n").encode("utf-8")
    for name, filename, content, mime in files:
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{name}"; '
                 f'filename="{filename}"\r\n'
                 f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        body += content
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    headers = _headers(key)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(url, data=bytes(body), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        upstream = exc.read().decode("utf-8", "replace")
        raise _fail(f"POST {url}", f"HTTP {exc.code} {upstream}") from exc
    except Exception as exc:
        raise _fail(f"POST {url}", repr(exc)) from exc
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise _fail(f"POST {url}", f"响应不是 JSON：{raw[:500]}") from exc


def list_datasets(root: Path) -> list[dict]:
    """GET /api/v1/datasets"""
    key = _key(root)
    data = _request_json("GET", f"{COGNEE_BASE}/api/v1/datasets", key)
    if not isinstance(data, list):
        raise _fail("GET /api/v1/datasets", f"预期数组，实际：{data}")
    return data


def data_count(root: Path, dataset_id: str) -> int:
    """GET /api/v1/datasets/{id}/data/count → 数据集里的文档条数。"""
    key = _key(root)
    data = _request_json(
        "GET", f"{COGNEE_BASE}/api/v1/datasets/{dataset_id}/data/count", key)
    if not isinstance(data, dict):
        raise _fail("GET /api/v1/datasets/{id}/data/count", f"预期对象，实际：{data}")
    return int(data.get("count") or 0)


def graph_summary(root: Path) -> dict[str, dict]:
    """GET /api/v1/datasets/graph-summary → {datasetId: {numNodes, numEdges, ...}}。

    一次取全部数据集的图规模，避免逐个数据集再发一遍请求。
    """
    key = _key(root)
    data = _request_json("GET", f"{COGNEE_BASE}/api/v1/datasets/graph-summary", key)
    if not isinstance(data, list):
        raise _fail("GET /api/v1/datasets/graph-summary", f"预期数组，实际：{data}")
    return {
        str(item.get("datasetId")): item
        for item in data
        if isinstance(item, dict) and item.get("datasetId")
    }


def ensure_dataset(root: Path, name: str) -> dict:
    """不存在则 POST 创建，存在返回现有。返回数据集 dict（含 id）。"""
    for dataset in list_datasets(root):
        if dataset.get("name") == name:
            return dataset
    key = _key(root)
    data = _request_json("POST", f"{COGNEE_BASE}/api/v1/datasets", key,
                         payload={"name": name})
    if not isinstance(data, dict) or not data.get("id"):
        raise _fail("POST /api/v1/datasets", f"返回缺少 id：{data}")
    return data


def add_text(root: Path, dataset: str, text: str, filename: str) -> str:
    """POST /api/v1/add（multipart 上传 .md 文本），返回 data_id。

    调用前已按容器 openapi.json 确认：该版本只有 /api/v1/add，没有 /api/v1/add_text。
    """
    key = _key(root)
    data = _post_multipart(
        f"{COGNEE_BASE}/api/v1/add", key,
        fields={"datasetName": dataset},
        files=[("data", filename, text.encode("utf-8"), "text/markdown")],
        timeout=300,
    )
    data_ids = data.get("data_ids") or []
    if not data_ids:
        raise _fail("POST /api/v1/add", f"返回缺少 data_ids：{json.dumps(data, ensure_ascii=False)}")
    return str(data_ids[0])


# 轮询最小间隔（秒）。poll_interval=0 时 time.sleep(0) 空转，实测 1 秒内
# 发了 11843 次 processing-status 请求，会把 Cognee 打挂。
_MIN_POLL_INTERVAL = 0.5


def cognify(root: Path, dataset: str, *, timeout: int = 1800,
            poll_interval: float = 10) -> bool:
    """POST /api/v1/cognify，带 datasets 参数。成功后返回 True。

    这个接口很慢（几分钟），超时设 1800 秒。服务端建图是阻塞式的，
    但 HTTP 连接容易被网关/超时掐断，所以这里用 runInBackground 提交，
    再轮询 processing-status 直到全部条目处理完。
    """
    key = _key(root)
    data = _request_json(
        "POST", f"{COGNEE_BASE}/api/v1/cognify", key,
        payload={
            "datasets": [dataset],
            "runInBackground": True,
            "dataPerBatch": 1,
            "chunksPerBatch": 4,
            "chunkSize": 512,
        },
        timeout=120,
    )
    if not isinstance(data, dict) or not data:
        raise _fail("POST /api/v1/cognify", f"返回异常：{data}")

    dataset_id = next(iter(data))
    run = data[dataset_id]
    run_id = run.get("pipeline_run_id") if isinstance(run, dict) else None
    sys.stderr.write(f"[cognify] {dataset} 已提交，run={run_id}\n")
    sys.stderr.flush()

    deadline = time.monotonic() + timeout
    last_report = 0.0
    while True:
        status = _request_json(
            "GET", f"{COGNEE_BASE}/api/v1/datasets/{dataset_id}/processing-status", key)
        total = int(status.get("total") or 0)
        completed = int(status.get("completed") or 0)
        pending = int(status.get("pending") or 0)

        now = time.monotonic()
        if now - last_report >= 30:
            sys.stderr.write(f"[cognify] {dataset}：{completed}/{total}，待处理 {pending}\n")
            sys.stderr.flush()
            last_report = now

        if pending == 0 and completed >= total:
            return True
        if now >= deadline:
            raise _fail("POST /api/v1/cognify",
                        f"等待建图超时（{timeout}s）：{completed}/{total}，"
                        f"待处理 {pending}")

        # 数据集级状态若已报错，不必再等
        overview = _request_json("GET", f"{COGNEE_BASE}/api/v1/datasets/status", key)
        state = overview.get(dataset_id, "") if isinstance(overview, dict) else ""
        if isinstance(state, str) and ("ERROR" in state or "FAILED" in state):
            raise _fail("POST /api/v1/cognify", f"数据集状态 {state}")

        time.sleep(max(float(poll_interval), _MIN_POLL_INTERVAL))
