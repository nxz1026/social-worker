#!/usr/bin/env python3
"""原始文件 → markdown，落进对应项目的 vault，供 Obsidian 浏览、Cognee 检索。

转换规则
  .docx                         → pandoc（快，结构干净，自动抽图到 media/）
  .xlsx .pptx .pdf .csv .xls
  .ppt .odt .ods .rtf .html     → docling（容器内，表格/图片更好）
  .md .txt .markdown            → 直接复制
  图片 / 音频 / 视频             → 复制到 vault/media/（Obsidian 原生支持，不转）

转换后按实体名册自动插入 [[链接]]，让 Obsidian 图谱长出边。

用法
  python3 convert-md.py                    # 两个项目，增量
  python3 convert-md.py --project huimin
  python3 convert-md.py --dry-run          # 只报计划
  python3 convert-md.py --force            # 忽略时间戳，全部重转
  python3 convert-md.py --jobs 8           # 并行度
  python3 convert-md.py --no-links         # 不插链接
  python3 convert-md.py --no-media         # 不复制独立图片/音视频
  python3 convert-md.py --prune            # 删掉源文件已不存在的 md
  python3 convert-md.py --config <path>    # 指定配置文件（默认 config/config.yaml）

路径与容器名来自 config/config.yaml（见 scripts/config.py）。
"""

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path

import yaml

from config import load_config

SCRIPT_DIR = Path(__file__).resolve().parent

CFG = None
ROOT = None
PROJECTS: dict = {}
CONTAINER_ROOT: Path | None = None
COGNEE_CONTAINER: str | None = None


def apply_config(cfg) -> None:
    """把配置对象落到模块级路径（scan-inbox 复用本模块时也会调用）。"""
    global CFG, ROOT, PROJECTS, CONTAINER_ROOT, COGNEE_CONTAINER
    CFG = cfg
    ROOT = cfg.root
    CONTAINER_ROOT = Path(cfg.container_root)
    COGNEE_CONTAINER = cfg.container
    PROJECTS = {
        "huadu": {"vault": ROOT / "vault-huadu", "sources": ROOT / "sources/projects/huadu"},
        "huimin": {"vault": ROOT / "vault-huimin", "sources": ROOT / "sources/projects/huimin"},
    }


apply_config(load_config())

# 需要转换的文档格式 → 转换器
CONVERTIBLE = {
    ".docx": "pandoc",
    ".xlsx": "docling", ".pptx": "docling", ".pdf": "docling", ".csv": "docling",
    ".xls": "docling", ".ppt": "docling", ".odt": "docling", ".ods": "docling",
    ".rtf": "docling", ".html": "docling", ".htm": "docling",
}
# 纯文本：直接复制
TEXTUAL = {".md", ".txt", ".markdown"}
# 媒体：直接复制（Obsidian 原生支持）
MEDIA = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tif", ".tiff",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga",
    ".mp4", ".mov", ".mkv", ".webm", ".avi",
}
# 图片子集：入库时额外做 OCR
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

DOCLING = "/app/.venv/bin/python"

DOCLING_SCRIPT = r"""
import pathlib, sys
from docling.document_converter import DocumentConverter
from docling_core.types.doc.base import ImageRefMode

src = pathlib.Path(sys.argv[1])
outdir = pathlib.Path(sys.argv[2])
outname = sys.argv[3]
outdir.mkdir(parents=True, exist_ok=True)

result = DocumentConverter().convert(src)
md = result.document.export_to_markdown(
    image_mode=ImageRefMode.REFERENCED,
    image_dir=str(outdir / "media"),
    image_uri_prefix="./media",
)
(outdir / outname).write_text(md, encoding="utf-8")
print("OK", len(md))
"""


def load_entities(cfg: dict) -> list[str]:
    """全局名册 + 项目级名册合并去重。"""
    names: set[str] = set()
    for path in (ROOT / "entities.yaml", cfg["sources"] / "entities.yaml"):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # 名册坏了不该挡住转换
            print(f"  ! 名册解析失败 {path}: {exc}")
            continue
        if isinstance(data, dict):
            for values in data.values():
                if isinstance(values, list):
                    names.update(str(v).strip() for v in values if str(v).strip())
        elif isinstance(data, list):
            names.update(str(v).strip() for v in data if str(v).strip())
    return sorted((n for n in names if len(n) >= 2), key=len, reverse=True)


def insert_links(text: str, names: list[str]) -> tuple[str, int]:
    """在正文里把实体名包成 [[链接]]，跳过代码块/行内代码/已有链接。"""
    if not names:
        return text, 0
    by_first: dict[str, list[str]] = defaultdict(list)
    for name in names:
        by_first[name[0]].append(name)

    out: list[str] = []
    i, n, hits = 0, len(text), 0
    while i < n:
        ch = text[i]

        # 围栏代码块
        if text.startswith("```", i) or text.startswith("~~~", i):
            fence = text[i:i + 3]
            j = text.find(fence, i + 3)
            if j == -1:
                out.append(text[i:])
                break
            out.append(text[i:j + 3])
            i = j + 3
            continue

        # 行内代码
        if ch == "`":
            j = text.find("`", i + 1)
            if j == -1:
                out.append(text[i:])
                break
            out.append(text[i:j + 1])
            i = j + 1
            continue

        # 已有 wikilink（含嵌入 ![[...]]）
        if text.startswith("[[", i) or text.startswith("![[", i):
            start = i + 1 if ch == "!" else i
            j = text.find("]]", start + 2)
            if j == -1:
                out.append(ch)
                i += 1
                continue
            out.append(text[i:j + 2])
            i = j + 2
            continue

        matched = None
        for name in by_first.get(ch, ()):
            if text.startswith(name, i):
                matched = name
                break
        if matched:
            out.append(f"[[{matched}]]")
            i += len(matched)
            hits += 1
            continue

        out.append(ch)
        i += 1
    return "".join(out), hits


IMG_TAG_RE = re.compile(r"<img\s+src=\"([^\"]+)\"[^>]*/?>")


def normalize_images(text: str, outdir: Path) -> str:
    """把 <img src="/abs/..."> 之类统一成 vault 相对路径的 ![](...)。"""
    def repl(m: re.Match) -> str:
        src = m.group(1)
        p = Path(src)
        if p.is_absolute():
            rel = os.path.relpath(p, outdir)
        else:
            rel = src
        return f"![]({rel.replace(os.sep, '/')})"
    return IMG_TAG_RE.sub(repl, text)


def convert_pandoc(src: Path, outdir: Path, outname: str) -> str:
    cmd = [
        "pandoc", str(src),
        "-t", "gfm",
        "--extract-media", str(outdir),
        "-o", str(outdir / outname),
        "--wrap=none",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"pandoc: {r.stderr.strip()[:400]}")
    return (outdir / outname).read_text(encoding="utf-8")


def to_container(path: Path) -> Path:
    """宿主机路径 → 容器内路径（root 映射到 container_root）。"""
    p = Path(path)
    try:
        rel = p.relative_to(ROOT)
    except ValueError:
        raise RuntimeError(f"{p} 不在服务端根目录 {ROOT} 下，无法映射到容器内路径")
    return CONTAINER_ROOT / rel


def convert_docling(src: Path, outdir: Path, outname: str) -> str:
    """在 Cognee 容器里跑 docling，产物经 docker cp 取回。"""
    # 宿主机 root 在容器里挂在 container_root（默认 /srv/social-work → /host/social-work）
    container_src = to_container(src)
    tmp = f"/tmp/conv-{uuid.uuid4().hex[:12]}"
    run = subprocess.run(
        ["docker", "exec", "-i", COGNEE_CONTAINER, DOCLING, "-",
         container_src.as_posix(), tmp, outname],
        input=DOCLING_SCRIPT, capture_output=True, text=True, timeout=1800,
    )
    if run.returncode != 0:
        raise RuntimeError(f"docling: {(run.stderr or run.stdout).strip()[:400]}")
    os.makedirs(outdir, exist_ok=True)
    cp = subprocess.run(
        ["docker", "cp", f"{COGNEE_CONTAINER}:{tmp}/.", str(outdir)],
        capture_output=True, text=True, timeout=600,
    )
    subprocess.run(["docker", "exec", COGNEE_CONTAINER, "rm", "-rf", tmp], capture_output=True)
    if cp.returncode != 0:
        raise RuntimeError(f"docker cp: {cp.stderr.strip()[:400]}")
    md = outdir / outname
    if not md.is_file():
        raise RuntimeError("docling 没有产出 md")
    return md.read_text(encoding="utf-8")


def process_one(job: dict) -> dict:
    """转换单个文件。job 由 build_jobs 构造。"""
    src: Path = job["src"]
    outdir: Path = job["outdir"]
    outname: str = job["outname"]
    converter: str = job["converter"]
    entities: list[str] = job["entities"]
    link_count = 0

    try:
        if converter == "copy":
            os.makedirs(outdir, exist_ok=True)
            shutil.copy2(src, outdir / outname)
        elif converter == "pandoc":
            text = convert_pandoc(src, outdir, outname)
            text = normalize_images(text, outdir)
            text, link_count = insert_links(text, entities)
            (outdir / outname).write_text(text, encoding="utf-8")
        elif converter == "docling":
            text = convert_docling(src, outdir, outname)
            text = normalize_images(text, outdir)
            text, link_count = insert_links(text, entities)
            (outdir / outname).write_text(text, encoding="utf-8")
        else:
            raise RuntimeError(f"未知转换器 {converter}")
        return {**job, "status": "ok", "links": link_count, "error": ""}
    except Exception as exc:
        return {**job, "status": "fail", "links": link_count, "error": str(exc)[:400]}


def build_jobs(cfg: dict, force: bool, use_media: bool) -> list[dict]:
    src_root: Path = cfg["sources"]
    vault: Path = cfg["vault"]
    entities = load_entities(cfg)
    jobs: list[dict] = []

    if not src_root.is_dir():
        return jobs

    for src in sorted(src_root.rglob("*")):
        if not src.is_file() or src.name.startswith(".git"):
            continue
        ext = src.suffix.lower()
        rel = src.relative_to(src_root)

        if ext in MEDIA:
            if not use_media:
                continue
            dst = vault / "media" / rel
            if not force and dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                continue
            jobs.append({"src": src, "outdir": dst.parent, "outname": dst.name,
                         "converter": "copy", "entities": entities, "kind": "media"})

        elif ext in TEXTUAL:
            outdir = vault / "md" / rel.parent / src.stem
            dst = outdir / src.name
            if not force and dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                continue
            jobs.append({"src": src, "outdir": outdir, "outname": src.name,
                         "converter": "copy", "entities": entities, "kind": "doc"})

        elif ext in CONVERTIBLE:
            outdir = vault / "md" / rel.parent / src.stem
            outname = src.stem + ".md"
            dst = outdir / outname
            if not force and dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                continue
            jobs.append({"src": src, "outdir": outdir, "outname": outname,
                         "converter": CONVERTIBLE[ext], "entities": entities, "kind": "doc"})

    return jobs


def prune(cfg: dict) -> list[str]:
    """删掉源文件已不存在的 md 目录。"""
    src_root: Path = cfg["sources"]
    md_root: Path = cfg["vault"] / "md"
    removed: list[str] = []
    if not md_root.is_dir():
        return removed
    for d in sorted(md_root.rglob("*")):
        if not d.is_dir():
            continue
        # md/<rel.parent>/<stem>/ → 对应源文件 src_root/<rel.parent>/<stem>.<ext>
        rel_parent = d.relative_to(md_root).parent
        stem = d.name
        candidates = list((src_root / rel_parent).glob(stem + ".*"))
        candidates = [c for c in candidates if c.suffix.lower() in CONVERTIBLE]
        if not candidates:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(str(d.relative_to(cfg["vault"])))
    return removed


def relink(cfg: dict, dry_run: bool = False) -> tuple[int, int]:
    """只对已有 md 重新插 `[[实体]]`，不重转、不覆盖正文。

    名册（entities.yaml）补了人/机构/地点之后，已有 md 不会自动长出链接：
    build_jobs 只挑源文件比 md 新的，名册变更不在它的判断里。
    这里直接扫 vault 里的 md 做一次幂等补链（已有 [[...]] 会跳过）。
    返回 (改写的篇数, 新增链接数)。
    """
    names = load_entities(cfg)
    md_root: Path = cfg["vault"] / "md"
    changed = links = 0
    if not md_root.is_dir() or not names:
        return 0, 0
    for path in sorted(md_root.rglob("*.md")):
        if "_待确认" in path.parts or ".trash" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        new, n = insert_links(text, names)
        if n and new != text:
            if not dry_run:
                path.write_text(new, encoding="utf-8")
            changed += 1
            links += n
    return changed, links


def main() -> int:
    ap = argparse.ArgumentParser(description="原始文件 → vault 里的 markdown")
    ap.add_argument("--project", choices=[*PROJECTS, "all"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略时间戳全部重转")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-links", action="store_true")
    ap.add_argument("--no-media", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--relink-only", action="store_true",
                    help="不转换，只给已有 md 重新插 [[实体]]（名册变更后用）")
    ap.add_argument("--config", metavar="PATH", help="配置文件路径（默认 config/config.yaml）")
    args = ap.parse_args()

    if args.config:
        try:
            apply_config(load_config(args.config))
        except FileNotFoundError as exc:
            print(f"  ! {exc}")
            return 1

    if args.project == "all":
        items = list(PROJECTS.items())
    else:
        items = [(args.project, PROJECTS[args.project])]

    if args.relink_only:
        for project, cfg in items:
            changed, links = relink(cfg, args.dry_run)
            verb = "将改写" if args.dry_run else "改写"
            print(f"{project}：{verb} {changed} 篇，新增链接 {links}")
        return 0

    total_ok = total_fail = total_links = 0
    failures: list[str] = []

    for project, cfg in items:
        jobs = build_jobs(cfg, args.force, not args.no_media)
        if args.no_links:
            for j in jobs:
                j["entities"] = []
        print(f"\n=== {project}：{len(jobs)} 个待处理 ===")
        if args.dry_run:
            for j in jobs:
                print(f"  [{j['converter']:7}] {j['src'].relative_to(cfg['sources'])}")
            continue

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for r in pool.map(process_one, jobs):
                rel = r["src"].relative_to(cfg["sources"])
                if r["status"] == "ok":
                    total_ok += 1
                    total_links += r["links"]
                    extra = f"  链接 {r['links']}" if r["links"] else ""
                    print(f"  ok   {rel}{extra}")
                else:
                    total_fail += 1
                    failures.append(f"{project}/{rel}: {r['error']}")
                    print(f"  FAIL {rel}: {r['error']}")

        if args.prune:
            for path in prune(cfg):
                print(f"  prune {path}")

    print(f"\n完成：成功 {total_ok}，失败 {total_fail}，插入链接 {total_links}")
    if failures:
        print("\n失败明细：")
        for line in failures:
            print("  " + line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
