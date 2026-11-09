"""归档审计日志：每次 scan-inbox.py 处理一个文件就追加一条。

写到 state/audit/YYYY-MM-DD.jsonl（保留期由 config.yaml 的 audit_retention_days 控制）。
M3 的 audit.py 只读这个文件出摘要，写入口统一在这里，避免两处写逻辑漂移。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))


def audit_dir(root) -> Path:
    return Path(root) / "state" / "audit"


def append(root, entry: dict) -> Path:
    """追加一条审计记录，返回写入的文件路径。

    审计里含文件名（可能含居民姓名），文件权限收紧到 0600；
    目录 0700。飞书/界面展示用脱敏后的 `display`，不要直接外发 `file`。
    """
    d = audit_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    now = datetime.now(TZ)
    path = d / f"{now.strftime('%Y-%m-%d')}.jsonl"
    record = {"ts": now.isoformat(timespec="seconds"), **entry}
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


def prune(root, retention_days: int = 90) -> int:
    """删掉超过保留期的日志，返回删除的文件数。retention_days<=0 表示不清理。"""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(TZ).date() - timedelta(days=retention_days)
    removed = 0
    for f in audit_dir(root).glob("*.jsonl"):
        try:
            day = datetime.strptime(f.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            f.unlink()
            removed += 1
    return removed
