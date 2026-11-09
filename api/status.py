#!/usr/bin/env python3
"""文档状态计算（纯函数，API 与测试共用）。

只回答「这篇文档现在怎么样」，不写任何东西：
  - source : 原件在不在
  - md     : 转换后的 md 在不在
  - feishu : 飞书归档是 moved / uploaded / missing / deleted / none
  - index  : Cognee 里是 indexed / stale / missing / unknown
  - relations : md 里的 [[实体]]（只读）

⚠️ Cognee 的 contentHash 是 **md5**，台账的 md_sha256 是 **sha256**
   ⇒ 判「索引过期」必须算 md5 去比，不能拿 sha256 比。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

_ENTITY_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

_MD5_CACHE: dict[tuple[str, int, int], str] = {}
_ROSTER_CACHE: dict[tuple[str, int], dict[str, str]] = {}
_TEMPLATE_CACHE: dict[tuple[str, int], str] = {}


def _hash_file(path: Path, algo: str, use_cache: bool = False) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_mtime_ns, st.st_size)
    if use_cache and algo == "md5" and key in _MD5_CACHE:
        return _MD5_CACHE[key]
    h = hashlib.new(algo)
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    out = h.hexdigest()
    if use_cache and algo == "md5":
        if len(_MD5_CACHE) > 5000:
            _MD5_CACHE.clear()
        _MD5_CACHE[key] = out
    return out


def md5_of(path: Path, use_cache: bool = True) -> str:
    return _hash_file(Path(path), "md5", use_cache=use_cache)


def sha256_of(path: Path) -> str:
    return _hash_file(Path(path), "sha256")


def load_roster(path: Path) -> dict[str, str]:
    """实体名册：名字 → 类型（projects/orgs/persons/places）。读不到返回空。"""
    try:
        st = Path(path).stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns)
    if key in _ROSTER_CACHE:
        return _ROSTER_CACHE[key]
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for kind, names in data.items():
            if isinstance(names, list):
                for name in names:
                    if isinstance(name, str) and name.strip():
                        out[name.strip()] = str(kind)
    _ROSTER_CACHE[key] = out
    return out


def load_feishu_url_template(path: Path) -> str:
    """feishu-flow.yaml 的 feishu.url_template；读不到返回空串。"""
    try:
        st = Path(path).stat()
    except OSError:
        return ""
    key = (str(path), st.st_mtime_ns)
    if key in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[key]
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    tpl = ""
    if isinstance(data, dict):
        tpl = str((data.get("feishu") or {}).get("url_template") or "")
    _TEMPLATE_CACHE[key] = tpl
    return tpl


def extract_entities(text: str) -> list[str]:
    """md 正文里的 [[实体]]，去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _ENTITY_RE.finditer(text or ""):
        name = m.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def relations_status(text: str, roster: dict[str, str] | None = None) -> dict:
    roster = roster or {}
    names = extract_entities(text)
    by_type: dict[str, list[str]] = {}
    for n in names:
        by_type.setdefault(roster.get(n, "其他"), []).append(n)
    return {"count": len(names), "entities": names, "by_type": by_type}


_ENT_CACHE: dict[tuple[str, int, int], list[str]] = {}


def doc_entities(path: Path) -> list[str]:
    """读一篇 md 的实体清单（按 mtime+size 缓存：反链要扫全库，不能每次重读）。"""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return []
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _ENT_CACHE:
        return _ENT_CACHE[key]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    out = extract_entities(text)
    if len(_ENT_CACHE) > 5000:
        _ENT_CACHE.clear()
    _ENT_CACHE[key] = out
    return out


def backlinks(root: Path, self_token: str, self_entry: dict,
              docs: list[tuple[str, str, str]],
              limit: int = 50) -> dict:
    """反链：**谁提到了我 / 谁和我共享实体**（只读，扫全库 md）。

    docs = [(token, name, md_rel)]，由调用方按权限过滤好。
    - ``backlinks``   : 正文里 ``[[本文档名]]`` / ``[[md 文件名]]`` 的其它文档
    - ``shared``      : 本文档的每个实体 → 也提到它的其它文档（最多 8 篇/实体）
    """
    root = Path(root)
    self_name = str(self_entry.get("name") or "")
    self_md = str(self_entry.get("md") or "")
    self_stem = Path(self_md).stem if self_md else ""
    targets = {t for t in (self_name, self_stem, Path(self_name).stem) if t}

    self_path = root / self_md if self_md else None
    my_entities = doc_entities(self_path) if (self_path and self_path.is_file()) else []
    mine = set(my_entities)

    links: list[dict] = []
    shared: dict[str, list[dict]] = {}
    for token, name, md_rel in docs:
        if token == self_token or not md_rel:
            continue
        path = root / md_rel
        if not path.is_file():
            continue
        ents = doc_entities(path)
        if not ents:
            continue
        ref = {"token": token, "name": name, "md": md_rel}
        if targets & set(ents):
            links.append(ref)
        for e in ents:
            if e in mine:
                bucket = shared.setdefault(e, [])
                if len(bucket) < 8:
                    bucket.append(ref)
    links.sort(key=lambda x: str(x.get("name") or ""))
    return {
        "count": len(links),
        "backlinks": links[:limit],
        "shared": [{"entity": e, "docs": shared[e]} for e in my_entities if e in shared],
    }


def feishu_status(entry: dict, token: str, url_template: str = "") -> dict:
    """飞书归档状态。

    - deleted : 台账已删除
    - uploaded: 本地文件补传到了「已分类」（有 feishu_token）
    - moved   : 飞书来源的文件被 move 到「已分类」（token 不是 local-*）
    - missing : 本地来源、但没补传成功
    - none    : 还没走到飞书归档
    """
    state = str(entry.get("status") or "")
    ft = str(entry.get("feishu_token") or "")
    local = str(token).startswith("local-")
    if state == "deleted":
        fs = "deleted"
    elif ft or entry.get("feishu_uploaded_at"):
        fs = "uploaded"
    elif state == "done" and token and not local:
        fs = "moved"
    elif state == "done":
        fs = "missing"
    else:
        fs = "none"
    link_token = ft or ("" if local else str(token or ""))
    url = ""
    if link_token and url_template:
        try:
            url = url_template.format(token=link_token)
        except Exception:
            url = ""
    return {
        "state": fs,
        "token": link_token or None,
        "url": url or None,
        "uploaded_at": entry.get("feishu_uploaded_at"),
    }


def index_status(md_path: Path | None, md_name: str,
                 entries: list[dict] | None, md5: str = "") -> dict:
    """Cognee 索引状态。

    entries 为 None ⇒ 取不到（unknown，不是「没索引」）。
    同名条目里 contentHash == md5 ⇒ indexed；同名但 hash 不同 ⇒ stale；没有同名 ⇒ missing。
    """
    if not md_path or not Path(md_path).is_file():
        return {"state": "missing", "name": md_name, "content_hash": None,
                "entries": 0, "error": "md 不存在"}
    if entries is None:
        return {"state": "unknown", "name": md_name, "content_hash": None,
                "entries": 0, "error": "Cognee 不可达"}
    if not md5:
        md5 = md5_of(Path(md_path))
    matches = [e for e in entries
               if isinstance(e, dict)
               and str(e.get("name") or "") == md_name
               and e.get("extension") in (None, "txt", "md")]
    if not matches:
        return {"state": "missing", "name": md_name, "content_hash": md5,
                "entries": 0, "error": None}
    hashes = {str(e.get("contentHash") or "") for e in matches}
    state = "indexed" if md5 in hashes else "stale"
    return {"state": state, "name": md_name, "content_hash": md5,
            "entries": len(matches), "error": None}


def doc_status(root: Path, token: str, entry: dict,
               dataset_entries: list[dict] | None,
               url_template: str = "",
               roster: dict[str, str] | None = None,
               include_relations: bool = True) -> dict:
    """一篇文档的完整状态。dataset_entries 由调用方按数据集取一次。"""
    root = Path(root)
    source_rel = str(entry.get("original") or "")
    md_rel = str(entry.get("md") or "")
    source_path = root / source_rel if source_rel else None
    md_path = root / md_rel if md_rel else None

    source_ok = bool(source_path and source_path.is_file())
    md_ok = bool(md_path and md_path.is_file())
    md5 = md5_of(md_path) if md_ok else ""
    md_name = Path(md_rel).stem if md_rel else ""

    text = ""
    if md_ok and include_relations:
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            text = ""

    out = {
        "token": token,
        "source": {
            "state": "ok" if source_ok else "missing",
            "path": source_rel,
            "size": (source_path.stat().st_size if source_ok else entry.get("size")),
            "sha256": entry.get("sha256") or "",
        },
        "md": {
            "state": "ok" if md_ok else "missing",
            "path": md_rel,
            "md5": md5 or None,
            "sha256": entry.get("md_sha256") or (sha256_of(md_path) if md_ok else ""),
        },
        "feishu": feishu_status(entry, token, url_template),
        "index": index_status(md_path if md_ok else None, md_name,
                              dataset_entries, md5=md5),
    }
    if include_relations:
        out["relations"] = relations_status(text, roster)
    return out
