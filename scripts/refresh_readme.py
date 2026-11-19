#!/usr/bin/env python3
"""刷新项目 / 活动 README 索引。

用法：
    python3 scripts/refresh_readme.py            # 全量刷新
    python3 scripts/refresh_readme.py --dry-run  # 只报会改哪些
    python3 scripts/refresh_readme.py --project 惠民社区保障资金项目

项目根默认 /srv/social-work/projects；可用 SOCIAL_WORK_PROJECTS 覆盖，
或只给 SOCIAL_WORK_ROOT，由脚本推导出 <ROOT>/projects。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 必须在 import engine.projects 之前设置：它只在模块加载时读一次环境变量
if not os.environ.get("SOCIAL_WORK_PROJECTS"):
    _root = os.environ.get("SOCIAL_WORK_ROOT")
    if _root:
        os.environ["SOCIAL_WORK_PROJECTS"] = str(Path(_root) / "projects")

from engine import doc_mirror, readme  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="刷新项目 / 活动 README 索引")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报会改哪些，不写盘")
    parser.add_argument("--project", default="",
                        help="只刷新指定项目（默认全部）")
    args = parser.parse_args(argv)

    project = args.project.strip() or None
    result = readme.refresh_all(dry_run=args.dry_run, project=project)

    verb = "会写" if args.dry_run else "写入"
    print(f"{verb} {len(result['written'])} 个，跳过 {result['skipped']} 个")
    for rel in result["written"]:
        print(f"  {verb} {rel}")

    # 顺手补 docx/xlsx/pdf 的同名 md 骨架（Obsidian 图谱用），不另设定时器
    try:
        if project:
            mirror = doc_mirror.mirror_project(project, dry_run=args.dry_run)
        else:
            mirror = doc_mirror.refresh_all(dry_run=args.dry_run)
    except Exception as exc:                 # 骨架失败不能把 README 刷新整轮拖垮
        mirror = {"written": [], "skipped": 0, "errors": [str(exc)]}
    print(f"{verb} {len(mirror['written'])} 个文档骨架，"
          f"跳过 {mirror['skipped']} 个")
    for rel in mirror["written"]:
        print(f"  {verb} {rel}")

    errors = list(result["errors"]) + list(mirror["errors"])
    for err in errors:
        print(f"  错误 {err}", file=sys.stderr)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
