#!/usr/bin/env python3
"""把已解压的活动包归档进项目活动目录，并写入 activity.yaml。

用法：
    python3 scripts/ingest_activities.py            # 全部导入
    python3 scripts/ingest_activities.py --dry-run  # 只看计划

流程：找活动目录（没有就按项目指标新建）→ 复制文件 → 读包内编号 → 写 yaml。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import activities, ingest, projects  # noqa: E402

# 来源：2026-11-18 活动包解压目录 /tmp/act4（P2.5 工单给定的四包）
# 顺序：源目录、归属项目、活动关键词、期数
IMPORTS = [
    ("/tmp/act4/爱邻议事会第一期20261105", "花都社区保障资金项目", "爱邻议事会", "第一期"),
    ("/tmp/act4/共治行动第一期",         "惠民社区保障资金项目", "共治行动", "第一期"),
    ("/tmp/act4/志愿服务第一期",         "惠民社区保障资金项目", "志愿服务", "第一期"),
    ("/tmp/act4/爱邻议事会第三期",       "惠民社区保障资金项目", "爱邻议事会", "第三期"),
]

CN_TZ = timezone(timedelta(hours=8))


def _project_dir(project_name: str) -> Path:
    """项目目录；项目名非法或不存在交给 engine 抛错。"""
    path = Path(projects.PROJECTS_ROOT) / project_name
    if not path.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{project_name}")
    return path


def _planned_target(project_name: str, keyword: str,
                    ordinal: str) -> tuple[Path | None, dict | None]:
    """目录不存在时，按项目指标算出应建的活动目录。

    返回 (目标路径, 计划项)；算不出唯一结果返回 (None, None)。
    """
    data = projects.load_project(project_name)
    matches = [
        item for item in activities.plan_activities(data)
        if keyword in item["dirname"] and ordinal in item["dirname"]
    ]
    if len(matches) != 1:
        return None, None
    # 走 projects.activity_dir，顺带做路径安全校验
    return projects.activity_dir(project_name, matches[0]["dirname"]), matches[0]


def _counts(src: Path, dst: Path) -> tuple[int, int]:
    """预览用：统计将复制 / 将跳过的文件数。"""
    will_copy = will_skip = 0
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        if (dst / item.relative_to(src)).exists():
            will_skip += 1
        else:
            will_copy += 1
    return will_copy, will_skip


def run(dry_run: bool = False) -> int:
    """执行导入，返回进程退出码（0 全成功，1 有错误）。"""
    failures = 0
    for src_text, project_name, keyword, ordinal in IMPORTS:
        src = Path(src_text)
        print(f"\n== {project_name} / {keyword}-{ordinal}")
        print(f"   源：{src}")

        if not src.is_dir():
            print("   [错误] 源目录不存在")
            failures += 1
            continue

        target = activities.find_activity_dir(_project_dir(project_name), keyword, ordinal)
        created = False
        if target is None:
            target, planned = _planned_target(project_name, keyword, ordinal)
            if target is None or planned is None:
                print("   [错误] 找不到唯一的匹配目录，也无法从项目指标推算")
                failures += 1
                continue
            created = True
            print(f"   目录：{target.name}（将新建）")
        else:
            print(f"   目录：{target.name}（已存在）")

        if dry_run:
            will_copy, will_skip = _counts(src, target)
            print(f"   [dry-run] 复制 {will_copy} 个文件，跳过已存在 {will_skip} 个")
            continue

        # 目标目录不存在时先建（花都的爱邻议事会目录还没建）
        if created:
            activities.create_activity_dirs(_project_dir(project_name), [{"dirname": target.name}])

        result = ingest.import_activity_tree(src, target)
        code = ingest.read_activity_code(target)
        imported_at = datetime.now(CN_TZ).isoformat(timespec="seconds")
        yaml_path = ingest.write_activity_yaml(
            target, code=code, source=str(src), imported_at=imported_at,
        )

        print(f"   复制 {len(result['copied'])}，跳过 {len(result['skipped'])}，"
              f"错误 {len(result['errors'])}")
        for err in result["errors"]:
            print(f"   [错误] {err}")
        print(f"   编号：{code or '（未读到）'}")
        print(f"   yaml：{yaml_path}")

        if result["errors"]:
            failures += 1

    print(f"\n完成：{len(IMPORTS)} 个包，失败 {failures} 个")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="活动包归档入库")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
