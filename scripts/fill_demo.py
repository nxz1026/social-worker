#!/usr/bin/env python3
"""P3 验证脚本：扫描模板占位符，再用真实活动数据填充三个模板。

用法：
    python3 scripts/fill_demo.py --activity 惠民社区保障资金项目/11-共治行动-第一期

输出：
    /tmp/fill_out/ 下三个填好的文件，并打印每个文件的 filled / missing。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import activity, fill, projects             # noqa: E402
from engine.markdown import docx_to_md, xlsx_to_md      # noqa: E402

# 模板目录；部署时可用 SOCIAL_WORK_TEMPLATES 指向别处
TEMPLATE_DIR = Path(os.environ.get("SOCIAL_WORK_TEMPLATES")
                    or "/srv/social-work/templates/library")
DEFAULT_OUT = Path("/tmp/fill_out")

# 演示用三个模板：决算表、活动目录、报销单
TARGETS = ("决算表.docx", "活动目录.docx", "报销单.xlsx")

# 活动包里常见「04-20261110xxx.docx」这种日期前缀
DATE_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})")


def split_activity(arg: str, project: str | None) -> tuple[str, str]:
    """拆「项目名/活动目录」；只给活动目录时必须用 --project 指定项目。"""
    raw = str(arg).strip().strip("/")
    if project:
        return project, raw
    parts = Path(raw).parts
    if len(parts) < 2:
        raise SystemExit("--activity 需要写成「项目名/活动目录」，或用 --project 指定项目")
    return parts[0], str(Path(*parts[1:]))


def date_from_filenames(act_dir: Path) -> str | None:
    """从活动包文件名里的 8 位日期补活动时间（activity.yaml 里没有时用）。"""
    if not act_dir.is_dir():
        return None
    for path in sorted(act_dir.rglob("*")):
        if not path.is_file():
            continue
        match = DATE_RE.search(path.name)
        if match:
            year, month, day = match.groups()
            return f"{int(year)}年{int(month)}月{int(day)}日"
    return None


def print_placeholder_table() -> None:
    """打印 15 个模板的占位符总表。"""
    files = sorted(p for p in TEMPLATE_DIR.iterdir()
                   if p.suffix.lower() in (".docx", ".xlsx"))
    print(f"模板占位符总表（{len(files)} 个模板，目录 {TEMPLATE_DIR}）")
    print("-" * 72)
    total = 0
    for path in files:
        placeholders = fill.scan_placeholders(path)
        total += len(placeholders)
        print(f"{path.name}（{len(placeholders)}）：{' '.join(placeholders) or '（无）'}")
    print("-" * 72)
    print(f"合计 {total} 个占位符（按模板去重后累加）")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="模板填充演示")
    parser.add_argument("--activity", required=True,
                        help="活动目录，如 惠民社区保障资金项目/11-共治行动-第一期")
    parser.add_argument("--project", default=None, help="项目名（活动目录不带项目前缀时用）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    args = parser.parse_args()

    if not TEMPLATE_DIR.is_dir():
        print(f"模板目录不存在：{TEMPLATE_DIR}", file=sys.stderr)
        return 1

    project_name, activity_name = split_activity(args.activity, args.project)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print_placeholder_table()

    # activity.yaml 只有 code，没有活动时间；从活动包文件名补一个真实日期
    extra: dict = {}
    act_dir = Path(projects.PROJECTS_ROOT) / project_name / "活动" / activity_name
    date_text = date_from_filenames(act_dir)
    if date_text:
        extra["活动时间"] = date_text

    data = activity.build_data(project_name, activity_name, extra=extra)
    print(f"填充数据（{project_name} / {activity_name}）")
    print("-" * 72)
    for key, value in data.items():
        print(f"  {key}: {value if value is not None else '（缺）'}")
    print("-" * 72)
    print()

    results = []
    for name in TARGETS:
        src = TEMPLATE_DIR / name
        if not src.is_file():
            print(f"{name} → 模板缺失，跳过")
            continue
        dst = out_dir / name
        result = fill.render(src, dst, data)
        results.append((name, dst, result))
        print(f"{name} → {dst}")
        print(f"  filled={result['filled']}  missing={result['missing']}")

    print()
    print("=" * 72)
    print("填充后内容（markdown 预览）")
    print("=" * 72)
    for name, dst, _result in results:
        print(f"\n### {name}\n")
        if dst.suffix.lower() == ".docx":
            print(docx_to_md(dst))
        else:
            print(xlsx_to_md(dst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
