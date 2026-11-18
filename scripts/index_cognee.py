#!/usr/bin/env python3
"""把 projects/ 下的文档转成 Markdown，按项目分数据集送进 Cognee 建图。

用法：
  python3 scripts/index_cognee.py --dry-run
  python3 scripts/index_cognee.py --project 惠民社区保障资金项目
  python3 scripts/index_cognee.py

范围：
  projects/<项目>/方案|协议|报告/** 与 projects/<项目>/活动/** 下的
  docx / doc / xlsx / pdf。图片不转 md，只按活动生成一份照片清单入图。

中间 Markdown 只在内存里生成，不落 projects/。
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

from config import load_config  # noqa: E402
from engine import cognee_index  # noqa: E402
from engine.markdown import file_to_md  # noqa: E402

DOC_EXTS = {".docx", ".doc", ".xlsx", ".pdf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
FIXED_DIRS = (("方案", "方案"), ("协议", "协议"), ("报告", "报告"))
ACTIVITY_DIR = "活动"
ACTIVITY_YAML = "activity.yaml"


@dataclass
class Item:
    """一个待入图条目：真实文档，或某个活动的照片清单。"""
    project: str
    rel: str
    kind: str
    path: Path | None = None
    activity: str = ""
    activity_code: str = ""
    images: list[str] = field(default_factory=list)

    @property
    def is_image_list(self) -> bool:
        return self.path is None


def _activity_code(activity_dir: Path) -> str:
    """读活动目录下 activity.yaml 的 code；没有或坏了都返回空串。"""
    meta = activity_dir / ACTIVITY_YAML
    if not meta.is_file():
        return ""
    try:
        data = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        sys.stderr.write(f"[warn] 读取 {meta} 失败：{exc}\n")
        return ""
    return str(data.get("code") or "") if isinstance(data, dict) else ""


def scan_project(project_dir: Path) -> list[Item]:
    """扫单个项目的方案/协议/报告/活动，返回文档条目与照片清单条目。"""
    project = project_dir.name
    items: list[Item] = []

    for dirname, kind in FIXED_DIRS:
        base = project_dir / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix.lower() in DOC_EXTS:
                items.append(Item(project=project, kind=kind,
                                  path=path, rel=path.relative_to(project_dir).as_posix()))

    activity_root = project_dir / ACTIVITY_DIR
    if activity_root.is_dir():
        for act_dir in sorted(p for p in activity_root.iterdir() if p.is_dir()):
            code = _activity_code(act_dir)
            for path in sorted(act_dir.rglob("*")):
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix in DOC_EXTS:
                    items.append(Item(
                        project=project, kind="活动文档", path=path,
                        rel=path.relative_to(project_dir).as_posix(),
                        activity=act_dir.name, activity_code=code,
                    ))
            images = [
                p.relative_to(act_dir).as_posix()
                for p in sorted(act_dir.rglob("*"))
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS
            ]
            if images:
                items.append(Item(
                    project=project, kind="活动图片清单",
                    rel=f"{ACTIVITY_DIR}/{act_dir.name}",
                    activity=act_dir.name, activity_code=code, images=images,
                ))
    return items


def scan(root: Path, project: str | None = None) -> list[Item]:
    """扫所有项目（或指定项目）。"""
    projects_root = root / "projects"
    if not projects_root.is_dir():
        raise SystemExit(f"项目根目录不存在：{projects_root}")
    if project:
        target = projects_root / project
        if not target.is_dir():
            raise SystemExit(f"项目不存在：{target}")
        dirs = [target]
    else:
        dirs = sorted(p for p in projects_root.iterdir() if p.is_dir())
    items: list[Item] = []
    for project_dir in dirs:
        items.extend(scan_project(project_dir))
    return items


def metadata_block(item: Item) -> str:
    """生成 Markdown 头部的元数据块。"""
    meta: dict[str, str] = {
        "project": item.project,
        "path": item.rel,
        "type": item.kind,
    }
    if item.activity:
        meta["activity"] = item.activity
    if item.activity_code:
        meta["activity_code"] = item.activity_code
    # width 放大，避免长路径被折行
    body = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=10000).strip()
    return f"---\n{body}\n---\n\n"


def image_list_md(item: Item) -> str:
    """活动照片清单正文。"""
    lines = [f"# 活动照片清单：{item.activity}", ""]
    lines.extend(f"- {name}" for name in item.images)
    lines.extend(["", f"共 {len(item.images)} 张"])
    return "\n".join(lines)


def item_text(item: Item) -> str:
    """条目 → 带元数据的 Markdown 正文。"""
    body = image_list_md(item) if item.is_image_list else file_to_md(item.path)
    return metadata_block(item) + body


def upload_name(item: Item) -> str:
    """上传给 Cognee 的文件名（扁平化，便于在数据集里辨认）。"""
    if item.is_image_list:
        return f"{item.activity}__照片清单.md"
    return item.rel.replace("/", "__") + ".md"


def run(root: Path, project: str = "") -> dict:
    """入库并建图（供 API 后台任务复用，行为与 CLI 非 dry-run 一致）。

    返回 {"projects": [项目名], "added": 入库条数, "errors": [转换失败的文件],
    "empty": 是否没有可入图的文档}。
    """
    items = scan(root, project)
    if not items:
        print("没有找到可入图的文档")
        return {"projects": [], "added": 0, "errors": [], "empty": True}

    by_project: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        by_project[item.project].append(item)

    errors: list[str] = []
    added = 0
    for proj, pitems in by_project.items():
        dataset = cognee_index.ensure_dataset(root, proj)
        print(f"[{proj}] 数据集 id = {dataset['id']}", flush=True)
        for item in pitems:
            try:
                text = item_text(item)
            except RuntimeError as exc:
                sys.stderr.write(f"[error] {item.rel} 转换失败：{exc}\n")
                errors.append(item.rel)
                continue
            if not text.strip():
                print(f"  - 跳过空内容 {item.rel}", flush=True)
                continue
            data_id = cognee_index.add_text(root, proj, text, upload_name(item))
            added += 1
            print(f"  + {item.rel} -> {data_id}", flush=True)

    # 全部送完后，按项目统一 cognify
    for proj in by_project:
        print(f"[{proj}] cognify 开始（可能几分钟）……", flush=True)
        cognee_index.cognify(root, proj)
        print(f"[{proj}] cognify 完成", flush=True)

    return {"projects": list(by_project), "added": added, "errors": errors,
            "empty": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="文档转 Markdown 后送入 Cognee 建图")
    parser.add_argument("--dry-run", action="store_true", help="只列出将入图的文件")
    parser.add_argument("--project", help="只处理指定项目（目录名）")
    args = parser.parse_args()

    cfg = load_config()
    root = cfg.root

    if args.dry_run:
        items = scan(root, args.project)
        if not items:
            print("没有找到可入图的文档")
            return 0
        by_project: dict[str, list[Item]] = defaultdict(list)
        for item in items:
            by_project[item.project].append(item)
        for project, pitems in by_project.items():
            print(f"[{project}] 数据集 = {project}")
            for item in pitems:
                if item.is_image_list:
                    print(f"  {item.kind:<6} {item.rel}（共 {len(item.images)} 张）")
                else:
                    print(f"  {item.kind:<6} {item.rel}")
        docs = sum(1 for i in items if not i.is_image_list)
        lists = len(items) - docs
        print(f"\n共 {len(by_project)} 个项目 / {docs} 个文档 / {lists} 条照片清单")
        return 0

    result = run(root, args.project or "")
    if result["empty"]:
        return 0

    print(f"\n完成：入库 {result['added']} 条，转换失败 {len(result['errors'])} 条")
    if result["errors"]:
        for rel in result["errors"]:
            print(f"  失败：{rel}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
