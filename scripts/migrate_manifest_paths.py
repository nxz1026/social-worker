#!/usr/bin/env python3
"""把历史台账里的 Windows 反斜杠路径归一成 POSIX。

用法：
  python3 scripts/migrate_manifest_paths.py --root /srv/social-work --dry-run
  python3 scripts/migrate_manifest_paths.py --root /srv/social-work

默认先备份到 `state/manifest.yaml.bak-<时间戳>`；`--dry-run` 只打印不写。
只改 original / md / media 三个路径字段，其他字段原样保留。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import manifest as _mf

PATH_KEYS = ("original", "md", "media")


def migrate(root: Path, dry_run: bool = False) -> int:
    path = _mf.manifest_path(root)
    data = _mf.load(path)
    changed = 0
    for token, entry in (data.get("files") or {}).items():
        if not isinstance(entry, dict):
            continue
        for key in PATH_KEYS:
            old = entry.get(key)
            if not isinstance(old, str) or "\\" not in old:
                continue
            new = _mf.normalize_rel(old)
            print(f"  {token} {key}: {old} → {new}")
            entry[key] = new
            changed += 1
    if not changed:
        print("没有需要迁移的路径")
        return 0
    if dry_run:
        print(f"dry-run：将修改 {changed} 个字段")
        return 0
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(path, backup)
    _mf.save(path, data)
    print(f"已迁移 {changed} 个字段，备份：{backup}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="台账路径反斜杠 → POSIX")
    ap.add_argument("--root", default="/srv/social-work")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    return migrate(Path(args.root), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
