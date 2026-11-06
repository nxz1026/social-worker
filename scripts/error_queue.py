#!/usr/bin/env python3
"""错误队列：不静默失败。

两份记录（都在 `state/errors/` 下）：
  - `open.yaml`            未处理的错误（人工/兜底处理完才划掉）
  - `YYYY-MM-DD.jsonl`     全量流水（含已关闭的，永久留痕）

为什么需要它：改之前转换失败只在 stdout 打一行 `! 转换失败`，
定时任务跑完就没了 —— 用户完全不知道有文件没处理。
现在每个失败都会落盘，并由 `scan-inbox.py` 立刻发飞书告警。

幂等：同一个 `(file, error)` 只入队一次（`error_id` 由两者哈希而来），
重复运行不会把同一条错误刷成多条，也不会重复告警。

用法
    from error_queue import add, list_open, resolve
    entry, is_new = add(root, file="x.pdf", error="转换失败：...", log=log)
    if is_new:
        notify.send(f"⚠️ 归档失败：{entry['file']} — {entry['error']}", root)
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

TZ = timezone(timedelta(hours=8))


def errors_dir(root) -> Path:
    return Path(root) / "state" / "errors"


def open_path(root) -> Path:
    return errors_dir(root) / "open.yaml"


def trail_path(root, date=None) -> Path:
    day = date or datetime.now(TZ).strftime("%Y-%m-%d")
    return errors_dir(root) / f"{day}.jsonl"


def error_id(file, message) -> str:
    """同文件同错误 → 同一个 id（用于去重与关闭）。"""
    return hashlib.sha256(f"{file}\n{message}".encode("utf-8")).hexdigest()[:16]


def brief_error(exc, limit: int = 300) -> str:
    """异常 → 简短描述。

    docling/pandoc 的异常消息带着整段 traceback（几十行），直接进错误队列和
    飞书消息会把人淹没。取第一行（真正的原因通常在这），太长就截断。
    """
    text = str(exc).strip() if not isinstance(exc, str) else exc.strip()
    if not text:
        text = type(exc).__name__
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), text)
    return first[:limit]


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add(root, file, error, extra=None, log=None):
    """把一个错误写进队列。返回 `(entry, is_new)`。

    `is_new=False` 表示这条错误已经在队列里（重复运行）—— 调用方**不该**重复告警。
    """
    root = Path(root)
    eid = error_id(file, error)
    entry = {
        "id": eid,
        "file": str(file),
        "error": str(error)[:500],
        "ts": _now(),
        "status": "open",
    }
    if extra:
        entry.update({k: v for k, v in extra.items() if v is not None})

    opens = _read_open(root)
    is_new = not any(o.get("id") == eid for o in opens)
    if is_new:
        opens.append(entry)
        _atomic_write(open_path(root), yaml.safe_dump(opens, allow_unicode=True, sort_keys=False))

    # 全量流水：每次都记（便于事后按时间查）
    try:
        trail_path(root).parent.mkdir(parents=True, exist_ok=True)
        with open(trail_path(root), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        if log:
            log(f"  ! 错误流水写入失败：{exc}")

    return entry, is_new


def _read_open(root) -> list:
    p = open_path(root)
    if not p.is_file():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def list_open(root) -> list:
    """未处理的错误（按入队时间升序）。"""
    return sorted(_read_open(root), key=lambda e: e.get("ts", ""))


def count_open(root) -> int:
    return len(list_open(root))


def resolve(root, eid, log=None) -> bool:
    """把一条错误标记为已处理（从 open.yaml 移除，流水保留）。返回是否真的移除了。"""
    root = Path(root)
    opens = _read_open(root)
    kept = [o for o in opens if o.get("id") != eid]
    if len(kept) == len(opens):
        return False
    _atomic_write(open_path(root), yaml.safe_dump(kept, allow_unicode=True, sort_keys=False))
    if log:
        log(f"  ✓ 错误 {eid} 已关闭（剩余 {len(kept)}）")
    return True


def close_by_file(root, file, log=None) -> int:
    """把某个文件的全部未处理错误关掉（兜底处理完时用）。返回关闭条数。"""
    root = Path(root)
    opens = _read_open(root)
    kept = [o for o in opens if o.get("file") != file]
    n = len(opens) - len(kept)
    if n:
        _atomic_write(open_path(root), yaml.safe_dump(kept, allow_unicode=True, sort_keys=False))
        if log:
            log(f"  ✓ {file} 的 {n} 条错误已关闭")
    return n
