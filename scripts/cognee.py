#!/usr/bin/env python3
"""Cognee 客户端（包装官方 cognee-cli）。

用法：
  cognee.py search "<查询>" [--type auto] [--dataset NAME] [--auto-dataset] [--top-k N]
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
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

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
# M7：检索参数下沉。--type 默认 auto，由 pick_type 按查询形态选；
# --auto-dataset 按查询里的项目关键词选数据集。
AUTO = "auto"
_DATE_RE = re.compile(r"(19|20)\d{2}|\d{4}[-/年]\d{1,2}|\d{1,2}月|\d{1,2}日")


def pick_type(query: str) -> str:
    """按查询形态选检索类型（M7）。"""
    q = (query or "").strip()
    if _DATE_RE.search(q):
        return "TEMPORAL"
    if any(ch in q for ch in ('"', "“", "”", "「", "」", "'", "‘", "’")):
        return "CHUNKS_LEXICAL"
    return "CHUNKS"


def pick_dataset(query: str, cfg, config_dir: Path = None):
    """按查询里的项目关键词选数据集（M7）。判不了返回 None = 搜全部。

    唯一最高分才返回；并列或没命中都返回 None。
    """
    cfg_dir = Path(config_dir) if config_dir else (
        cfg.path.parent if getattr(cfg, "path", None) else Path("/srv/social-work/config"))
    alias_file = cfg_dir / "project_aliases.yaml"
    if not alias_file.is_file():
        return None
    try:
        data = yaml.safe_load(alias_file.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    datasets = getattr(cfg, "datasets", {}) or {}
    q = query or ""
    scores = {}
    for key, words in data.items():
        if not isinstance(words, list):
            continue
        hits = sum(q.count(str(w)) for w in words if w)
        if hits:
            scores[key] = hits
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None                      # 并列 → 交给全库检索
    key = ranked[0][0]
    # 项目 key 与数据集名按约定同名；配置里显式给了映射就用映射
    return datasets.get(key) or key


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
    qtype = pick_type(args.query) if args.type == AUTO else args.type
    dataset = args.dataset
    if not dataset and args.auto_dataset:
        dataset = pick_dataset(args.query, get_config(), None)
        if dataset:
            print(f"· 自动选数据集：{dataset}", file=sys.stderr)
    cli_args = ["search", args.query, "--query-type", qtype,
                "--output-format", "json"]
    if dataset:
        cli_args += ["--datasets", dataset]
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
    print(f"· 检索类型：{qtype}", file=sys.stderr)
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
    """纯函数：算出这次 sync 该加什么、该改什么、该删什么。

    entries = 数据集现有条目（dict 列表）
    jobs    = [(Path, contentHash)]，本地 md 及其内容 hash

    返回 (to_add, to_update, to_delete)：
      - to_add    = [(Path, hash)]，本地新出现、库里没有
      - to_update = [(entry, Path, hash)]，同名但内容变了（entry 是库里的旧条目）
      - to_delete = [entry]，本地 md 已删/已移走，或同名同内容的重复条目

    规则：
      - 非文本条目（图片等）→ 一律不动
      - 同名同内容的重复条目只留一条
      - 同名同内容的多份本地 md 只入一条（避免重复入库）
    """
    target = {}
    for m, h in jobs:
        target.setdefault(m.stem, set()).add(h)

    to_add, to_update, to_delete = [], [], []
    seen = set()          # 已确认保留的 (name, hash)
    claimed = set()       # jobs 下标，已被「保留条目」或「更新」认领
    for e in entries:
        if not _is_text_entry(e):
            continue
        name, h = e.get("name"), e.get("contentHash")
        if name in target and h in target[name] and (name, h) not in seen:
            seen.add((name, h))
            for i, (m, jh) in enumerate(jobs):
                if i not in claimed and m.stem == name and jh == h:
                    claimed.add(i)
                    break
            continue
        # 该删或该改：同名的新 job 还没被认领 → 算「改」
        if name in target:
            for i, (m, jh) in enumerate(jobs):
                if i not in claimed and m.stem == name and jh != h:
                    claimed.add(i)
                    to_update.append((e, m, jh))
                    break
            else:
                to_delete.append(e)
        else:
            to_delete.append(e)

    # 库里已保留同名同内容、或本地多份完全相同的 md，都不必再入
    added = set()
    for i, (m, h) in enumerate(jobs):
        if i in claimed or (m.stem, h) in seen or (m.stem, h) in added:
            continue
        added.add((m.stem, h))
        to_add.append((m, h))
    return to_add, to_update, to_delete


def sync_dataset(md_root, dataset, key, dry_run=False, log=None):
    """把 <md-root> 下的 md 全量镜像进数据集（幂等，可反复跑）。

    Cognee 的 add 对同一文件的不同版本会建出新条目而不是更新，
    所以按 (name, contentHash) 比对：内容变了的旧条目删掉重加，
    没变的跳过。数据集里的非文本条目（图片等）不动。

    返回 {"added": int, "updated": int, "deleted": int,
          "cognified": bool, "error": str|None}。
    """
    log = log or print
    root = Path(md_root)
    st = {"added": 0, "updated": 0, "deleted": 0, "cognified": False, "error": None}
    if not root.is_dir():
        st["error"] = f"目录不存在：{root}"
        return st
    mds = _collect_mds(root)
    if not mds:
        log(f"{root} 下没有 md")
        return st

    dss = _api("/api/v1/datasets", key)
    ds = next((d for d in dss if d.get("name") == dataset), None)
    if not ds:
        st["error"] = f"数据集不存在：{dataset}"
        return st
    ds_id = ds["id"]

    jobs = [(m, md5_of(m)) for m in mds]
    to_add, to_update, to_delete = plan_sync(_fetch_entries(ds_id, key), jobs)

    if dry_run:
        for e in to_delete:
            log(f"  [dry-run] 删除 {e.get('name')}（现有 hash {e.get('contentHash')}）")
        for e, m, h in to_update:
            log(f"  [dry-run] 更新 {e.get('name')} → {m}")
        for m, h in to_add:
            log(f"  [dry-run] 入库 {m}")
        st["deleted"] = len(to_delete)
        st["updated"] = len(to_update)
        st["added"] = len(to_add)
        log("(dry-run，未执行 cognify)")
        return st

    # 先删旧条目：内容变了的旧版本 + 本地已无对应 md 的孤儿
    stale = list(to_delete) + [e for e, _, _ in to_update]
    delete_failed = 0
    for e in stale:
        try:
            _api(f"/api/v1/datasets/{ds_id}/data/{e['id']}", key, method="DELETE")
        except Exception as exc:  # noqa: BLE001
            delete_failed += 1
            log(f"  ! 删除旧条目失败 {e.get('name')}: {exc}")
    log(f"清理旧条目 {len(stale)} 个")

    # 删完重取，得到数据集的真实剩余
    to_add, _, _ = plan_sync(_fetch_entries(ds_id, key), jobs)

    added = 0
    failed: list[str] = []
    for m, _ in to_add:
        r = subprocess.run(["docker", "exec", "-e", f"COGNEE_API_KEY={key}", get_config().container,
                            CLI, "--api-url", API_URL, "add", to_container(str(m)),
                            "--dataset-name", dataset],
                           capture_output=True, text=True)
        if r.returncode != 0:
            failed.append(m.name)
            log(f"  ! add 失败 {m.name}: {(r.stderr or r.stdout).strip()[:160]}")
        else:
            added += 1
    log(f"入库 {added}/{len(mds)}（跳过未变 {len(mds) - len(to_add)}）")

    st["deleted"] = len(to_delete)
    st["updated"] = len(to_update)
    st["added"] = added
    if delete_failed or failed:
        st["error"] = (f"{delete_failed} 个旧条目删除失败、" if delete_failed else "") \
            + (f"{len(failed)} 个条目入库失败" if failed else "")
    if stale or to_add:
        st["cognified"] = cognify(dataset, key, log=log)
        if not st["cognified"] and not st.get("error"):
            st["error"] = "cognify 失败"
    return st


def cognify(dataset, key, log=None):
    """重建数据集的知识图谱，返回是否成功。"""
    log = log or print
    r = subprocess.run(["docker", "exec", "-e", f"COGNEE_API_KEY={key}", get_config().container,
                        CLI, "--api-url", API_URL, "cognify", "--datasets", dataset],
                       capture_output=True, text=True)
    log((r.stdout or r.stderr).strip()[:300])
    return r.returncode == 0


def delete_by_name(dataset, name, key, dry_run=False, log=None):
    """删掉数据集里指定 name 的文本条目，返回删了几个。"""
    log = log or print
    dss = _api("/api/v1/datasets", key)
    ds = next((d for d in dss if d.get("name") == dataset), None)
    if not ds:
        raise RuntimeError(f"数据集不存在：{dataset}")
    ds_id = ds["id"]
    n = 0
    for e in _fetch_entries(ds_id, key):
        if not _is_text_entry(e) or e.get("name") != name:
            continue
        if dry_run:
            log(f"  [dry-run] 删除 {name}")
        else:
            _api(f"/api/v1/datasets/{ds_id}/data/{e['id']}", key, method="DELETE")
        n += 1
    return n


def cmd_sync(args, key):
    """把 <md-root> 下的 md 全量镜像进数据集（幂等，可反复跑）。"""
    st = sync_dataset(args.md_root, args.dataset, key, dry_run=args.dry_run, log=print)
    if st.get("error"):
        sys.exit(f"! {st['error']}")


def main():
    p = argparse.ArgumentParser(description="Cognee 客户端（包装 cognee-cli）")
    p.add_argument("--config", metavar="PATH", help="配置文件路径（默认 config/config.yaml）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="检索知识库")
    s.add_argument("query")
    s.add_argument("--type", default=AUTO, choices=[AUTO] + SEARCH_TYPES,
                   help="检索类型；默认 auto 按查询形态自动选（日期→TEMPORAL，带引号→CHUNKS_LEXICAL）")
    s.add_argument("--dataset")
    s.add_argument("--auto-dataset", action="store_true",
                   help="按查询里的项目关键词自动选数据集")
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
