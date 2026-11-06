#!/usr/bin/env python3
"""Cognee 客户端（包装官方 cognee-cli）。

用法：
  cognee.py search "<查询>" [--type CHUNKS] [--dataset NAME] [--top-k N]
  cognee.py add <宿主机路径> [--dataset NAME]
  cognee.py cognify [--dataset NAME]
  cognee.py datasets
  cognee.py status

鉴权：读配置里的 key_file（默认 <root>/.cognee-key）里的 API key（X-Api-Key）。
路径：add 时自动把服务端 root 映射为容器内 container_root
      （默认 /srv/social-work → /host/social-work）。
配置：config/config.yaml，可用 --config 指定；SOCIAL_WORK_ROOT 覆盖 root。
"""
import argparse
import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from config import load_config

CLI = "/app/.venv/bin/cognee-cli"
API_URL = "http://127.0.0.1:8000"

CFG = None


def get_config():
    """延迟加载，--help 时不必依赖配置文件存在。"""
    global CFG
    if CFG is None:
        CFG = load_config()
    return CFG


SEARCH_TYPES = [
    "CHUNKS", "CHUNKS_LEXICAL", "SUMMARIES", "GRAPH_COMPLETION",
    "HYBRID_COMPLETION", "RAG_COMPLETION", "CODE", "CYPHER",
    "GRAPH_REPORT", "BROAD", "SKILLS", "TEMPORAL",
]


def load_key():
    key_file = get_config().key_file
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        sys.exit(f"缺少 {key_file}，请先在服务端创建 API key")
    if not key:
        sys.exit(f"{key_file} 为空")
    return key


def run(args, key, timeout=900):
    cmd = ["docker", "exec", "-e", f"COGNEE_API_KEY={key}", get_config().container,
           CLI, "--api-url", API_URL] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        sys.stderr.write((proc.stderr or "") + (proc.stdout or ""))
        sys.exit(proc.returncode)
    return proc.stdout


def to_container(path):
    """相对路径按服务端 root 解析，再映射成容器内路径。"""
    cfg = get_config()
    host_root = cfg.root
    if not path.startswith("/"):
        path = str(host_root / path.lstrip("./"))
    try:
        rel = Path(path).relative_to(host_root)
    except ValueError:
        return path
    return str(Path(cfg.container_root) / rel)


def extract_results(data, out):
    """解析 cognee-cli search 返回的 list[dataset]。

    CHUNKS/SUMMARIES 的 search_result 是 dict（含 text/document_name/score）；
    GRAPH_COMPLETION 的 search_result 是纯字符串列表。
    """
    for ds in data if isinstance(data, list) else []:
        if not isinstance(ds, dict):
            continue
        for item in ds.get("search_result") or []:
            if isinstance(item, str):
                if item.strip():
                    out.append(("(直接回答)", None, item))
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    name = item.get("document_name") or item.get("document_id") or "?"
                    out.append((name, item.get("score"), text))


def cmd_search(args, key):
    cli_args = ["search", args.query, "--query-type", args.type,
                "--output-format", "json"]
    if args.dataset:
        cli_args += ["--datasets", args.dataset]
    if args.top_k:
        cli_args += ["--top-k", str(args.top_k)]
    stdout = run(cli_args, key)
    # cognee-cli 会在 JSON 前打印 "Searching for: ..." 之类的提示行
    starts = [i for i in (stdout.find("["), stdout.find("{")) if i >= 0]
    if not starts:
        print(stdout)
        return
    try:
        data = json.loads(stdout[min(starts):])
    except json.JSONDecodeError:
        print(stdout)
        return
    results = []
    extract_results(data, results)
    if not results:
        print("(无结果)")
        return
    for name, score, text in results:
        head = f"### {name}"
        if score is not None:
            head += f"  (score {score:.3f})"
        print(head)
        print(text.strip())
        print()


def cmd_add(args, key):
    path = to_container(args.path)
    cli_args = ["add", path]
    if args.dataset:
        cli_args += ["--dataset-name", args.dataset]
    print(run(cli_args, key).strip())


def cmd_cognify(args, key):
    cli_args = ["cognify"]
    if args.dataset:
        cli_args += ["--datasets", args.dataset]
    print(run(cli_args, key).strip())


def cmd_datasets(args, key):
    stdout = run(["datasets", "list"], key)
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("ID") or set(line) <= set("-"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            print(f"{parts[0]}  {parts[1]}")


def cmd_status(args, key):
    try:
        with urllib.request.urlopen(f"{API_URL}/health", timeout=10) as r:
            print(r.read().decode())
    except Exception as exc:
        sys.exit(f"服务端不可达：{exc}")


def _api(path, key, method="GET", body=None):
    """直接调 REST API（X-Api-Key 鉴权）。"""
    req = urllib.request.Request(API_URL + path, method=method)
    req.add_header("X-Api-Key", key)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw else {}


def _collect_mds(root):
    """收集 root 下要索引的 md，排除 Obsidian 配置、Syncthing 元数据与 media。"""
    skip_parts = {".obsidian", ".stfolder", ".stversions", "media", ".trash"}
    out = []
    for p in sorted(root.rglob("*.md")):
        if skip_parts & set(p.relative_to(root).parts):
            continue
        out.append(p)
    return out


def md5_of(path):
    """文件内容 md5（Cognee 的 contentHash 就是这个）。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_entries(ds_id, key):
    entries = _api(f"/api/v1/datasets/{ds_id}/data", key)
    if isinstance(entries, dict):
        entries = entries.get("data") or entries.get("items") or []
    return entries


def _is_text_entry(entry):
    """只有文本条目（md 转出来的）参与镜像；图片等非文本条目保留。"""
    return entry.get("extension") in (None, "txt")


def plan_sync(entries, jobs):
    """纯函数：算出这次 sync 该删什么、该加什么。

    entries = 数据集现有条目（dict 列表）
    jobs    = [(Path, contentHash)]，本地 md 及其内容 hash

    规则：
      - 文本条目里，name 在本地有、但 hash 对不上 → 删（内容变了的旧版本）
      - 文本条目里，name 本地没有 → 删（md 已删/已移走）
      - 非文本条目（图片等）→ 一律不动
      - 本地 md 的 (name, hash) 已存在 → 跳过，不重加
    """
    target = {}
    for m, h in jobs:
        target.setdefault(m.stem, set()).add(h)

    to_delete = []
    seen = set()
    for e in entries:
        if not _is_text_entry(e):
            continue
        name = e.get("name")
        h = e.get("contentHash")
        if name not in target or h not in target[name]:
            to_delete.append(e)          # 内容变了的旧版本，或 md 已删/已移走
            continue
        if (name, h) in seen:
            to_delete.append(e)          # 同名同内容的重复条目，只留一条
            continue
        seen.add((name, h))

    drop_ids = {e.get("id") for e in to_delete}
    remaining = {}
    for e in entries:
        if e.get("id") in drop_ids:
            continue
        remaining.setdefault(e.get("name"), set()).add(e.get("contentHash"))

    to_add = [(m, h) for m, h in jobs if h not in remaining.get(m.stem, set())]
    return to_delete, to_add


def cmd_sync(args, key):
    """把 <md-root> 下的 md 全量镜像进数据集（幂等，可反复跑）。

    Cognee 的 add 对同一文件的不同版本会建出新条目而不是更新，
    所以按 (name, contentHash) 比对：内容变了的旧条目删掉重加，
    没变的跳过。数据集里的非文本条目（图片等）不动。
    """
    root = Path(args.md_root)
    if not root.is_dir():
        sys.exit(f"目录不存在：{root}")
    mds = _collect_mds(root)
    if not mds:
        print(f"{root} 下没有 md")
        return

    dss = _api("/api/v1/datasets", key)
    ds = next((d for d in dss if d.get("name") == args.dataset), None)
    if not ds:
        sys.exit(f"数据集不存在：{args.dataset}")
    ds_id = ds["id"]

    jobs = [(m, md5_of(m)) for m in mds]

    to_delete, _ = plan_sync(_fetch_entries(ds_id, key), jobs)
    for e in to_delete:
        if args.dry_run:
            print(f"  [dry-run] 删除 {e.get('name')}（现有 hash {e.get('contentHash')}）")
        else:
            _api(f"/api/v1/datasets/{ds_id}/data/{e['id']}", key, method="DELETE")
    print(f"清理旧条目 {len(to_delete)} 个")

    # 删完重取，得到数据集的真实剩余
    _, to_add = plan_sync(_fetch_entries(ds_id, key), jobs)

    added = 0
    for m, _ in to_add:
        if args.dry_run:
            print(f"  [dry-run] 入库 {m}")
            added += 1
            continue
        r = subprocess.run(["docker", "exec", "-e", f"COGNEE_API_KEY={key}", get_config().container,
                            CLI, "--api-url", API_URL, "add", to_container(str(m)),
                            "--dataset-name", args.dataset],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ! add 失败 {m.name}: {(r.stderr or r.stdout).strip()[:160]}")
        else:
            added += 1
    print(f"入库 {added}/{len(mds)}（跳过未变 {len(mds) - len(to_add)}）")

    if args.dry_run:
        print("(dry-run，未执行 cognify)")
        return

    r = subprocess.run(["docker", "exec", "-e", f"COGNEE_API_KEY={key}", get_config().container,
                        CLI, "--api-url", API_URL, "cognify", "--datasets", args.dataset],
                       capture_output=True, text=True)
    print((r.stdout or r.stderr).strip()[:300])


def main():
    p = argparse.ArgumentParser(description="Cognee 客户端（包装 cognee-cli）")
    p.add_argument("--config", metavar="PATH", help="配置文件路径（默认 config/config.yaml）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="检索知识库")
    s.add_argument("query")
    s.add_argument("--type", default="CHUNKS", choices=SEARCH_TYPES)
    s.add_argument("--dataset")
    s.add_argument("--top-k", type=int)
    s.set_defaults(func=cmd_search)

    a = sub.add_parser("add", help="入库文件")
    a.add_argument("path")
    a.add_argument("--dataset")
    a.set_defaults(func=cmd_add)

    c = sub.add_parser("cognify", help="重建知识图谱")
    c.add_argument("--dataset")
    c.set_defaults(func=cmd_cognify)

    d = sub.add_parser("datasets", help="列出数据集")
    d.set_defaults(func=cmd_datasets)

    sy = sub.add_parser("sync", help="把 md 目录全量重建进数据集（幂等）")
    sy.add_argument("md_root")
    sy.add_argument("--dataset", required=True)
    sy.add_argument("--dry-run", action="store_true", help="只报计划，不删不加的")
    sy.set_defaults(func=cmd_sync)

    st = sub.add_parser("status", help="检查服务端健康")
    st.set_defaults(func=cmd_status)

    args = p.parse_args()
    global CFG
    try:
        CFG = load_config(args.config)
    except FileNotFoundError as exc:
        sys.exit(f"! {exc}")
    args.func(args, load_key())


if __name__ == "__main__":
    main()
