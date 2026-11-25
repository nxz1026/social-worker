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
from engine import cognee_index
from engine import readme as _readme  # noqa: E402
from engine.markdown import file_to_md  # noqa: E402

DOC_EXTS = {".docx", ".doc", ".xlsx", ".pdf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
# 用户 2026-11-25：图片与 txt 也要进知识库，图片只按文件名入条目、不做 OCR。
# 但「纯照片目录」（10-物资照片资料 / 11-活动照片资料 之类）仍走照片清单，
# 不逐张入图 —— 那里动辄几十张，逐张建节点只会把图谱冲垮。
ASSET_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".txt"}
# 这些目录名里的图片交给「照片清单」聚合，不单独建条目
PHOTO_DIR_MARKERS = ("物资照片", "活动照片", "发票")
# `.md` 是 Obsidian 骨架（由 docx 转出，如 01-计划书.docx + 01-计划书.md 成对存在）。
# 用户 2026-11-24 要求 `.md` 也要能搜到，但成对入图会让同一内容重复建边 ——
# 所以只在**没有同名原件**时才索引 .md（见 drop_md_with_twin）。
MD_EXTS = {".md", ".markdown"}
# 项目级固定目录。`其他/` 是「未命中归档清单」的资料落点（联络表、阶段成果等），
# 上传端点会把它们放进去 —— 不索引就等于「存得下、搜不到」（2026-11-24 补）。
FIXED_DIRS = (("方案", "方案"), ("协议", "协议"), ("报告", "报告"), ("其他", "其他"))
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
        """是否「照片清单」条目。

        原来只判断 path is None，导致任何没传 path 的普通文档条目都被判成
        照片清单（实测：Item(project="P", rel="方案/sub/a.docx", kind="方案")
        的 upload_name 返回 '__照片清单.md'）。照片清单是活动级的特殊产物，
        用 activity 字段才是准的。
        """
        return self.path is None and bool(self.activity)


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


def _is_md(path: Path) -> bool:
    return path.suffix.lower() in MD_EXTS


def _twin(path: Path) -> Path | None:
    """同一目录下的同名原件（docx/doc/xlsx/pdf），没有返回 None。

    `01-计划书.md` 的原件是 `01-计划书.docx`。原件扩展名大小写不敏感：
    实测有 `.DOCX` 残留，用 `with_suffix(".docx").is_file()` 会漏判。
    """
    stem = path.stem.lower()
    for sibling in path.parent.iterdir():
        if (sibling.is_file() and sibling != path
                and sibling.stem.lower() == stem
                and sibling.suffix.lower() in DOC_EXTS):
            return sibling
    return None


def drop_md_with_twin(paths: list[Path]) -> list[Path]:
    """去掉「有同名原件」的 .md，只保留原件；只有 .md 的才留 .md。

    一个活动里 `01-计划书.docx` 与 `01-计划书.md` 通常成对存在（后者是前者转出的
    Obsidian 骨架）。两个都送给 Cognee，同一份内容会重复入图、重复建边 ——
    所以有原件就跳过 .md，没有原件才索引 .md。
    规则按「同目录 + 同主名」判断，不同目录的同名文件互不影响。
    """
    return [p for p in paths if not (_is_md(p) and _twin(p) is not None)]


# 明确不入知识库的材料（用户 2026-11-24：支出证明只存档）。
# 注意这里按「文件名关键词」匹配，因为文件可能被改名带序号。
# 支出证明是纯财务凭据，进图谱只会制造无关实体（单位名、金额）噪音。
INDEX_EXCLUDE_KEYWORDS = ("支出证明",)

# 自动生成的索引/目录文件不入图：
# - `_X-资料索引.md`（活动级）、`_X-项目资料索引.md`（项目级）：内容就是材料清单
#   表，只有文件名和核对状态，没有业务知识；文件名已由各自 docx 入图
# - `活动目录.docx` / `项目管理目录.docx`：同样是目录清单
# 不入图能避免两类噪音：① 45 个活动索引里 30 个指向空目录；
# ② 索引里的 [[链接]] 文本会在图谱里制造指向不存在节点的边。
def _excluded(path: Path) -> bool:
    """判定文件是否不入知识图谱。

    导航/索引文件走 engine.readme.is_generated_nav() —— 单一口径，
    新增生成物时只改那边一处，这里自动跟随，不必维护第二份规则。
    """
    if any(kw in path.name for kw in INDEX_EXCLUDE_KEYWORDS):
        return True
    return _readme.is_generated_nav(path)


def _visible(path: Path, base: Path) -> bool:
    """相对 base 的路径里有没有以 `.` 开头的段。

    `.trash/`（回收站）、`.preview/`（PDF 预览）、`.activity.lock` 这些不是资料，
    入图只会重复或污染。与 engine/checks.py::_visible_files_under 同一口径。
    """
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return False
    if any(part.startswith(".") for part in parts):
        return False
    return not _excluded(path)


def _in_photo_dir(path: Path, base: Path) -> bool:
    """文件是否落在「照片/发票目录」里（那些由照片清单聚合，不逐张入图）。"""
    try:
        parts = path.relative_to(base).parts[:-1]
    except ValueError:
        return False
    return any(any(m in part for m in PHOTO_DIR_MARKERS) for part in parts)


def _candidate_files(base: Path) -> list[Path]:
    """base 下可入图的文件：原件 + .md + 图片/txt（图片按文件名，不 OCR）。"""
    out: list[Path] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or not _visible(p, base):
            continue
        suffix = p.suffix.lower()
        if suffix in DOC_EXTS | MD_EXTS:
            out.append(p)
        elif suffix in ASSET_EXTS and not _in_photo_dir(p, base):
            # 散落在普通目录里的图片/txt（如 13-其他佐证/ 的海报、通知记录）
            out.append(p)
    return out


def scan_project(project_dir: Path) -> list[Item]:
    """扫单个项目的方案/协议/报告/活动，返回文档条目与照片清单条目。"""
    project = project_dir.name
    items: list[Item] = []

    for dirname, kind in FIXED_DIRS:
        base = project_dir / dirname
        if not base.is_dir():
            continue
        for path in drop_md_with_twin(_candidate_files(base)):
            items.append(Item(project=project, kind=kind,
                              path=path, rel=path.relative_to(project_dir).as_posix()))

    activity_root = project_dir / ACTIVITY_DIR
    if activity_root.is_dir():
        for act_dir in sorted(p for p in activity_root.iterdir() if p.is_dir()):
            if act_dir.name.startswith("."):
                continue
            code = _activity_code(act_dir)
            for path in drop_md_with_twin(_candidate_files(act_dir)):
                items.append(Item(
                    project=project, kind="活动文档", path=path,
                    rel=path.relative_to(project_dir).as_posix(),
                    activity=act_dir.name, activity_code=code,
                ))
            images = [
                p.relative_to(act_dir).as_posix()
                for p in sorted(act_dir.rglob("*"))
                if p.is_file() and _visible(p, act_dir)
                and p.suffix.lower() in IMAGE_EXTS
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


def run(root: Path, project: str = "", *, wait: bool = True) -> dict:
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
    # 建图逐个做。**单个项目失败不能中断后面项目**：cognify 很慢（约 2 条/分钟），
    # 大项目超时是常态，但那不该让其它项目一条都建不了
    # （2026-11-25 实测：惠民超时后花都两个项目 0/152、0/32 完全没开始）。
    cognify_errors: list[str] = []
    for proj in by_project:
        print(f"[{proj}] cognify 开始（大项目可能 1-2 小时）……", flush=True)
        try:
            cognee_index.cognify(root, proj, wait=wait)
            print(f"[{proj}] cognify 完成", flush=True)
        except Exception as exc:
            cognify_errors.append(f"{proj}: {exc}")
            print(f"[{proj}] cognify 失败（数据已入库，不影响检索）：{exc}",
                  flush=True)
    if cognify_errors:
        print(f"\n建图失败 {len(cognify_errors)} 个项目（数据均已入库，"
              f"可稍后重跑补图）：", flush=True)
        for e in cognify_errors:
            print(f"  - {e}", flush=True)

    return {"projects": list(by_project), "added": added, "errors": errors,
            "empty": False,
            # 建图失败是「数据已入库、图谱关系没建」的状态，调用方要能看到
            "cognify_errors": cognify_errors}


def main() -> int:
    parser = argparse.ArgumentParser(description="文档转 Markdown 后送入 Cognee 建图")
    parser.add_argument("--dry-run", action="store_true", help="只列出将入图的文件")
    parser.add_argument("--project", help="只处理指定项目（目录名）")
    parser.add_argument("--no-wait", action="store_true",
                        help="只提交建图不等待（给定时器用：cognify 一次可能跑几小时）")
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

    result = run(root, args.project or "", wait=not args.no_wait)
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
