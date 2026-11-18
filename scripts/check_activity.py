#!/usr/bin/env python3
"""命令行：核对活动目录的归档材料（15 项清单）。

用法：
    python3 scripts/check_activity.py 花都社区保障资金项目/13-爱邻议事会-第一期
    python3 scripts/check_activity.py /srv/social-work/projects/某项目/活动/某活动
    python3 scripts/check_activity.py --all      # 扫所有项目的所有活动
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from engine import activities, checks, projects  # noqa: E402


def _resolve(raw: str) -> Path | None:
    """把命令行给的路径解析成活动目录。

    依次尝试：当前目录下的相对路径 / 项目根下的路径 /
    「项目名/活动名」和「项目名/活动/活动名」两种简写。
    """
    path = Path(raw)
    if path.is_dir():
        return path

    root = Path(projects.PROJECTS_ROOT)
    parts = list(path.parts)
    candidates = [root / path]
    if len(parts) == 2:
        candidates.append(root / parts[0] / "活动" / parts[1])
    elif len(parts) == 3 and parts[1] == "活动":
        candidates.append(root / parts[0] / "活动" / parts[2])
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _targets(all_projects: bool, raw: str) -> list[tuple[str, Path]]:
    """收集待核对的活动目录，返回 [(标签, 路径)]。"""
    if not all_projects:
        path = _resolve(raw)
        if path is None:
            print(f"活动目录不存在：{raw}", file=sys.stderr)
            return []
        return [("", path)]

    out: list[tuple[str, Path]] = []
    for project in projects.list_projects():
        project_dir = Path(projects.PROJECTS_ROOT) / project["name"]
        for dirname, act_dir in activities.activity_dirs(project_dir).items():
            out.append((f"{project['name']} / {dirname}", act_dir))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对活动目录的 15 项归档材料")
    parser.add_argument("path", nargs="?", help="活动目录（可用 项目名/活动名 简写）")
    parser.add_argument("--all", action="store_true", help="扫所有项目的所有活动")
    args = parser.parse_args(argv)

    if not args.all and not args.path:
        parser.error("要么给活动目录，要么用 --all")

    targets = _targets(args.all, args.path or "")
    if not targets:
        return 1

    done = 0
    for label, act_dir in targets:
        result = checks.check(act_dir)
        title = label or act_dir.name
        print(f"## {title}\n")
        print(checks.to_markdown(result))
        if result["complete"]:
            done += 1

    print(f"**合计**：{len(targets)} 个活动，完整 {done} 个，"
          f"未完整 {len(targets) - done} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
