#!/usr/bin/env python3
"""M5 一条命令归档。

DSH 侧只调这一条，不再手工分步（落 sources/ + 转 md + 入库 + 写飞书）。
手工分步的漏项（飞书归档 / 台账 / cognify）从架构上消失。

    python3 scripts/archive.py --file <路径> [--project huimin]
                               [--action auto|archive_only|ocr|vision]

流程：校验 → 定项目 → 放进 inbox/ 对应位置（目录即授权）→ 跑 scan-inbox.py
      → 校验结果（台账有记录 / md 存在）
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import config as _cfg
import classify_rules as _cr
import image_policy as _ip

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tiff", ".tif"}


def cheap_preview(path, limit=2000):
    """便宜地取一段正文，只用于定项目。

    不跑完整转换（PDF 走 docling 要几十秒，转两遍太亏）：
    docx → pandoc 纯文本，pdf → pdftotext（扫描件取不到就返回空）。
    """
    cmd = {".docx": ["pandoc", str(path), "-t", "plain"],
           ".pdf": ["pdftotext", str(path), "-"]}.get(Path(path).suffix.lower())
    if not cmd:
        return ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return (r.stdout or "")[:limit]
    except Exception:
        return ""


def decide_project(name, path, rules, projects, explicit):
    """定项目。给了就用；没给先按文件名判，判不了读正文再判；都不行返回 (None, decision)。"""
    if explicit:
        return explicit, None
    decision = _cr.classify_rule(name, "", rules, projects=projects,
                                 require_category=False)
    if decision and decision.get("project"):
        return decision["project"], decision
    preview = cheap_preview(path)
    if preview:
        decision = _cr.classify_rule(name, preview, rules, projects=projects,
                                     require_category=False)
        if decision and decision.get("project"):
            return decision["project"], decision
    return None, decision


def resolve_action(action_arg, ext, policy):
    """把 --action 解析成具体动作。非图片返回 None（图片策略不适用）。"""
    if action_arg and action_arg != "auto":
        return action_arg
    return _ip.decide_action(Path("x"), ext, policy, IMAGE_EXTS)


def dest_rel(action, name):
    """动作 → inbox 里的相对位置（目录即授权）。"""
    if action == "ocr":
        return Path("要OCR") / name
    if action == "vision":
        return Path("要识图") / name
    return Path(name)


def verify(root, name):
    """校验结果。返回 (ok, lines)。"""
    import yaml

    lines, ok = [], True
    mpath = root / "state" / "manifest.yaml"
    entry = None
    if mpath.is_file():
        try:
            manifest = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
            for rec in (manifest.get("files") or {}).values():
                if rec.get("name") == name:
                    entry = rec
                    break
        except Exception as exc:
            lines.append(f"台账读取失败：{exc}")
            ok = False
    if entry:
        lines.append(f"台账有记录：{entry.get('project')} / {entry.get('category')}")
        md = entry.get("md")
        if md:
            md_abs = root / md
            lines.append(f"md {'存在' if md_abs.is_file() else '缺失'}：{md}")
            ok = ok and md_abs.is_file()
        else:
            lines.append("未生成 md（只存档 / 非文档类）")
    else:
        lines.append("台账里没有这个文件 —— 归档可能失败了，看 scan-inbox 输出")
        ok = False
    return ok, lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="一条命令归档（M5）")
    ap.add_argument("--file", required=True, help="要归档的文件（任意路径）")
    ap.add_argument("--project", help="项目（huadu/huimin）；不给则先按文件名判，判不了读正文再判")
    ap.add_argument("--action", default="auto",
                    choices=["auto", "archive_only", "ocr", "vision"],
                    help="图片处置动作；auto=按 config/image_policy.yaml")
    ap.add_argument("--root", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只看会做什么，不动文件")
    args = ap.parse_args(argv)

    CFG = _cfg.load_config(Path(args.root) if args.root else None)
    root = CFG.root
    src = Path(args.file).expanduser()

    print(f"=== 归档：{src} ===")
    if not src.is_file():
        print(f"✗ 文件不存在：{src}")
        return 2
    if src.parent.resolve() == (root / "inbox").resolve():
        print("· 文件已在 inbox/，跳过复制")

    rules = _cr.load_rules(CFG.path.parent if getattr(CFG, "path", None)
                           else root / "config")
    policy = _ip.load_policy(CFG.path.parent if getattr(CFG, "path", None)
                             else root / "config")
    projects = sorted(rules["projects"]) if isinstance(rules.get("projects"), dict) else None
    categories = sorted(rules["categories"]) if isinstance(rules.get("categories"), dict) else None

    project, decision = decide_project(src.name, src, rules, projects, args.project)
    if not project:
        print(f"✗ 定不了项目（文件名和正文里都没有项目线索）")
        print(f"  请加 --project huadu|huimin")
        return 3
    print(f"· 项目：{project}" + ("（规则判定）" if decision else "（指定）"))

    action = resolve_action(args.action, src.suffix.lower(), policy)
    if args.action != "auto" and src.suffix.lower() not in IMAGE_EXTS:
        print(f"· 注意：--action {args.action} 对非图片文件没有意义（只有图片走图片策略）")
    rel = dest_rel(action, src.name)
    dest = root / "inbox" / rel
    print(f"· 动作：{action or '（非图片，正常转换）'}")
    print(f"· 放入：inbox/{rel}")

    if args.dry_run:
        print("（dry-run，未执行）")
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
        print(f"  已复制（{src.stat().st_size} 字节）")

    # 指定项目时写元数据，scan-inbox 会尊重它（项目强制，类别仍自动判）
    if args.project:
        meta = dest.with_name(dest.name + ".meta.json")
        meta.write_text(json.dumps({"project": args.project}, ensure_ascii=False),
                        encoding="utf-8")
        print(f"  元数据：{meta.name}（project={args.project}）")

    t0 = time.time()
    print("\n=== 跑 scan-inbox.py ===")
    proc = subprocess.run([sys.executable, str(root / "scripts" / "scan-inbox.py")],
                          cwd=str(root), capture_output=True, text=True)
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip()][-12:]
    for ln in tail:
        print(ln)
    if proc.returncode != 0:
        print(f"✗ scan-inbox 退出码 {proc.returncode}")
        if proc.stderr.strip():
            print(proc.stderr.strip()[-500:])
        return 4

    print(f"\n=== 校验结果（{time.time() - t0:.1f}s）===")
    ok, lines = verify(root, src.name)
    for ln in lines:
        print(f"  {'✓' if ok else '·'} {ln}")
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(main())
