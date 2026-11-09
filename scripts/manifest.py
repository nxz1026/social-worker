#!/usr/bin/env python3
"""台账（state/manifest.yaml）统一读写。

三件事集中在这里，避免每个脚本各写一份：

1. **锁**：同一份台账可能被 scan-inbox / reconcile / API 同时改。以前是
   「读-改-写」裸奔，后写覆盖前写，`deleted_at` / `restored_at` 会丢。
   现在所有写都走 `lock()` 或 `update_entry()`。
2. **原子写**：临时文件 + fsync + `os.replace`，崩溃不会留下截断的 YAML。
3. **路径**：台账里的路径统一存 POSIX 形式（`a/b.md`），Windows 上
   `str(Path)` 会产生 `a\\b.md`，跨平台读不回来。

锁文件是 `<manifest>.lock`，用 `O_CREAT|O_EXCL` 创建，跨平台可用；
超过 `stale_after` 秒的锁视为进程崩溃遗留，自动回收。
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path, PurePosixPath

import yaml

MANIFEST_REL = Path("state") / "manifest.yaml"


class ManifestCorrupt(RuntimeError):
    """台账存在但解析不了 —— 调用方应停下并告警，不能当空台账继续。"""


def manifest_path(root) -> Path:
    return Path(root) / MANIFEST_REL


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextlib.contextmanager
def lock(path, timeout: float = 60.0, stale_after: float = 600.0):
    """独占锁。超时抛 TimeoutError，不静默继续。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lp = _lock_path(p)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
            break
        except FileExistsError:
            try:
                if time.time() - lp.stat().st_mtime > stale_after:
                    lp.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"台账被占用超过 {timeout:.0f}s：{lp}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lp.unlink(missing_ok=True)
        except OSError:
            pass


def load(path, strict: bool = True) -> dict:
    """读台账。文件不存在返回空台账；解析失败按 strict 决定抛错还是返回空。"""
    p = Path(path)
    if not p.is_file():
        return {"files": {}}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - YAML 的异常类型很杂
        if strict:
            raise ManifestCorrupt(f"台账解析失败 {p}：{exc}") from exc
        return {"files": {}}
    if data is None:
        return {"files": {}}
    if not isinstance(data, dict):
        if strict:
            raise ManifestCorrupt(f"台账根节点不是映射：{p}")
        return {"files": {}}
    files = data.setdefault("files", {})
    if not isinstance(files, dict):
        if strict:
            raise ManifestCorrupt(f"台账 files 不是映射：{p}")
        return {"files": {}}
    return data


def save(path, data: dict) -> None:
    """原子写：同目录临时文件 → fsync → replace。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def update(path, mutator, timeout: float = 60.0):
    """锁内读-改-写；mutator 抛异常则不落盘。返回 mutator 的返回值。"""
    p = Path(path)
    with lock(p, timeout=timeout):
        data = load(p)
        out = mutator(data)
        save(p, data)
        return out


def update_entry(path, token: str, fields: dict, remove=(), timeout: float = 60.0) -> dict:
    """只更新一个条目的指定字段（并发安全），返回更新后的条目。"""
    def _mut(data: dict) -> dict:
        entry = data["files"].setdefault(token, {})
        entry.update(fields)
        for key in remove:
            entry.pop(key, None)
        return entry

    return update(path, _mut, timeout=timeout)


def rel_posix(path, root) -> str:
    """相对 root 的 POSIX 路径（Windows 上也返回 a/b.md）。"""
    return Path(path).relative_to(Path(root)).as_posix()


def normalize_rel(rel: str) -> str:
    """把历史台账里可能出现的 `a\\b.md` 归一成 `a/b.md`。"""
    return str(rel or "").replace("\\", "/").lstrip("./")


def entry_path(root, entry: dict, key: str = "md") -> Path | None:
    """台账路径 → 绝对路径；空值返回 None。"""
    rel = normalize_rel(str(entry.get(key) or ""))
    return (Path(root) / PurePosixPath(rel)) if rel else None
