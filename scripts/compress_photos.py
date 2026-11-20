#!/usr/bin/env python3
"""存量活动照片批量压缩（按 docs/照片上传协议.md 的 2560px / q95 规格）。

用法：
    python3 scripts/compress_photos.py --dry-run          # 只看能省多少
    python3 scripts/compress_photos.py --project 惠民社区保障资金项目
    python3 scripts/compress_photos.py --project 惠民社区保障资金项目 --activity 11-共治行动-第一期

只处理 10-物资照片资料 / 11-活动照片资料 / 12-发票 下的 .jpg/.jpeg；
原图就地覆盖（先写 .tmp 再 replace），不删除、不改名。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from engine import photos as _photos          # noqa: E402
from engine import projects as _projects      # noqa: E402


def _dir_bytes(photo_dir: Path) -> tuple[int, int]:
    """返回 (文件数, 总字节)；只算会被压缩的 jpg/jpeg。"""
    count = total = 0
    if photo_dir.is_dir():
        for item in photo_dir.iterdir():
            if (item.is_file() and item.suffix.lower() in (".jpg", ".jpeg")
                    and item.name != _photos.CONTACT_SHEET_NAME):
                count += 1
                total += item.stat().st_size
    return count, total


def _iter_photo_dirs(project_names: list[str], activity: str):
    root = Path(_projects.PROJECTS_ROOT)
    for name in project_names:
        act_root = root / name / "活动"
        if not act_root.is_dir():
            continue
        for act_dir in sorted(p for p in act_root.iterdir() if p.is_dir()):
            if activity and act_dir.name != activity:
                continue
            for sub in sorted(_photos.PHOTO_DIRS):
                photo_dir = act_dir / sub
                if photo_dir.is_dir():
                    yield name, act_dir, photo_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="活动照片存量压缩")
    parser.add_argument("--project", action="append", default=[],
                        help="只处理指定项目（可重复）；默认全部项目")
    parser.add_argument("--activity", default="", help="只处理指定活动目录名")
    parser.add_argument("--dry-run", action="store_true", help="只估算，不写文件")
    args = parser.parse_args()

    root = Path(_projects.PROJECTS_ROOT)
    if not root.is_dir():
        print(f"项目根目录不存在：{root}")
        return 1

    names = args.project or sorted(p.name for p in root.iterdir() if p.is_dir())
    rows = []
    total_files = total_before = total_saved = 0

    for project, act_dir, photo_dir in _iter_photo_dirs(names, args.activity):
        files, before = _dir_bytes(photo_dir)
        if not files:
            continue
        result = _photos.compress_dir(act_dir, photo_dir, dry_run=args.dry_run)
        after = before - result["saved_bytes"]
        rows.append((project, act_dir.name, photo_dir.name, files,
                     before, after, result))
        total_files += files
        total_before += before
        total_saved += result["saved_bytes"]

    if not rows:
        print("没有找到可压缩的活动照片")
        return 0

    print(f"{'项目':<16}{'活动':<28}{'目录':<14}{'张数':>5}"
          f"{'前':>10}{'后':>10}{'压缩率':>8}")
    for project, act, sub, files, before, after, result in rows:
        rate = (result["saved_bytes"] / before * 100) if before else 0
        print(f"{project:<16}{act:<28}{sub:<14}{files:>5}"
              f"{before / 1048576:>9.1f}M{after / 1048576:>9.1f}M{rate:>7.1f}%")

    rate = (total_saved / total_before * 100) if total_before else 0
    print("-" * 91)
    print(f"{'合计':<58}{total_files:>5}"
          f"{total_before / 1048576:>9.1f}M"
          f"{(total_before - total_saved) / 1048576:>9.1f}M{rate:>7.1f}%")
    if args.dry_run:
        print(f"预演模式：未写盘，预计可省 {total_saved / 1048576:.1f} MB")
    else:
        print(f"已写盘：省下 {total_saved / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
