#!/usr/bin/env python3
"""活动级 YAML 互斥读写。

activity.yaml 由三个地方写：核对记录（checks.save_review）、不适用项
（materials）、活动状态/编号（ingest.write_activity_yaml）。它们改的是
**同一个文件的不同字段**，各自「读全量 → 改字段 → 覆盖写」的模式在并发下
必然互相覆盖：

- 固定 `.tmp` 名 → 两个写者撞同一临时文件，先完成的 replace 走后另一个
  报 FileNotFoundError（2026-11-22 实测：8 线程 116 次异常）；
- 后写者基于旧快照整体覆盖 → 先写者的记录静默消失（同一次实测：8 个
  序号只剩 3 个）。

这里提供唯一的写入口：持活动级 fcntl 排他锁 + 唯一临时名。
多线程、多进程（多个 uvicorn worker）都有效。
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import secrets
from pathlib import Path
from typing import Callable

import yaml

# 锁文件按活动目录放，名字固定（不能每个调用方自己起名，否则跨模块不互斥）
LOCK_NAME = ".activity.lock"


@contextlib.contextmanager
def activity_lock(act_dir: Path):
    """活动级跨进程互斥锁。同一个活动目录下所有 yaml 写入都应持有它。"""
    base = Path(act_dir)
    base.mkdir(parents=True, exist_ok=True)
    fd = os.open(base / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write_yaml(path: Path, data: dict) -> None:
    """原子写 YAML：唯一临时名 + replace。

    临时名带 pid 与随机后缀：即使调用方忘了持锁，也不会两个写者撞同一 .tmp。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        tmp.replace(p)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def update_yaml(path: Path, mutator: Callable[[dict], dict | None],
                *, lock_dir: Path | None = None) -> dict:
    """在活动锁内完成「读 → 改 → 写」。返回写入后的数据。

    mutator 接收当前数据，返回要写的新数据（返回 None 表示不写）。
    读操作也在锁内，避免读到「另一个写者改了一半」的中间态。
    """
    p = Path(path)
    lock_target = Path(lock_dir) if lock_dir else p.parent
    with activity_lock(lock_target):
        data = {}
        if p.is_file():
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            data = loaded if isinstance(loaded, dict) else {}
        new = mutator(data)
        if new is None:
            return data
        atomic_write_yaml(p, new)
        return new


def atomic_write_text(path: Path, text: str) -> None:
    """原子写任意文本（项目 yaml、索引 md 等非 dict 内容）。

    固定 .tmp 名在多用户并发时会撞同一临时文件：先完成的 replace 走后，
    另一个报 FileNotFoundError。带 pid + 随机后缀可避免。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
