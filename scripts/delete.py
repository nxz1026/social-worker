#!/usr/bin/env python3
"""删除一个已归档的文档：vault md + Cognee + 飞书 + 台账。

用法：
  delete.py <标识> [--mode trash|purge] [--yes] [--no-notify] [--config PATH]

<标识> 可以是：台账 token、文件名、md 相对路径、飞书 file_token。
默认 dry-run，加 --yes 才真删。
  trash（默认）—— md 移到 <vault>/.trash/<日期>/，可恢复；sources/ 原件保留
  purge        —— 连 sources/ 原件一起删，不可恢复
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

from config import load_config
from notify import send as notify_send
from audit_log import append as audit_append

import cognee as _cg
from reconcile import (TRASH_DIRNAME, TZ, delete_feishu, load_manifest, log,
                       now_iso, save_manifest)


def find_entries(manifest, ident):
    """按 token / 文件名 / md 路径 / 飞书 token 找台账条目。"""
    out = []
    for token, e in (manifest.get("files") or {}).items():
        if ident in (token, e.get("name"), e.get("md"), e.get("feishu_token")):
            out.append((token, e))
    return out


def trash_md(root, vault, md_rel, dry_run):
    """把 md 移到 <vault>/.trash/<日期>/ 下，返回目标相对路径（dry-run 返回 None）。"""
    md_path = Path(root) / md_rel
    if not md_path.is_file():
        return None
    if dry_run:
        log(f"  [dry-run] md → .trash（{md_rel}）")
        return None
    # md_rel 一般以 <vault> 目录名开头；取不到就原样保留相对路径
    try:
        rel = Path(md_rel).relative_to(Path(vault).name)
    except ValueError:
        rel = Path(md_rel)
    dest = Path(vault) / TRASH_DIRNAME / datetime.now(TZ).strftime("%Y-%m-%d") / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(md_path), str(dest))
    os.chmod(dest, 0o644)
    return str(dest.relative_to(root))


def main():
    ap = argparse.ArgumentParser(description="删除一个已归档的文档")
    ap.add_argument("identifier", help="台账 token / 文件名 / md 路径 / 飞书 file_token")
    ap.add_argument("--mode", choices=["trash", "purge"], default="trash")
    ap.add_argument("--yes", action="store_true", help="真删（默认只预览）")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--config", metavar="PATH")
    args = ap.parse_args()

    cfg = load_config(args.config)
    root = Path(cfg.root)
    flow_path = root / "feishu-flow.yaml"
    flow = yaml.safe_load(flow_path.read_text(encoding="utf-8")) if flow_path.is_file() else {}
    flow = flow or {}
    manifest = load_manifest(root)

    matches = find_entries(manifest, args.identifier)
    if not matches:
        sys.exit(f"找不到匹配的文档：{args.identifier}")
    if len(matches) > 1:
        print("匹配到多个，请用更精确的标识：")
        for token, e in matches:
            print(f"  {token}  {e.get('name')}  [{e.get('status')}]")
        sys.exit(2)

    token, entry = matches[0]
    name = entry.get("name") or token
    project = entry.get("project")
    pcfg = (flow.get("projects", {}).get(project, {}) or {})
    vault = Path(root) / pcfg.get("vault", "vault")

    print(f"删除 {name}")
    print(f"  项目/类别：{project} / {entry.get('category')}")
    print(f"  台账 token：{token}")
    print(f"  md：{entry.get('md')}")
    print(f"  飞书：{entry.get('feishu_token')}")
    print(f"  模式：{args.mode}")
    if not args.yes:
        print("\n(dry-run，加 --yes 才真删)")
        return 0

    # 1) vault md
    md_rel = entry.get("md")
    if md_rel:
        if args.mode == "trash":
            moved = trash_md(root, vault, md_rel, False)
            if moved:
                print(f"  md → {moved}")
        else:
            p = Path(root) / md_rel
            if p.is_file():
                p.unlink()
                print("  md 已删除")
    # 2) sources 原件
    if args.mode == "purge":
        orig = entry.get("original")
        if orig and (Path(root) / orig).is_file():
            (Path(root) / orig).unlink()
            print(f"  原件已删除：{orig}")
    # 3) Cognee
    dataset = pcfg.get("dataset")
    stem = Path(md_rel).stem if md_rel else Path(name).stem
    if dataset:
        key = _cg.load_key()
        try:
            n = _cg.delete_by_name(dataset, stem, key, log=log)
            print(f"  Cognee 条目已删：{n}")
        except Exception as exc:
            print(f"  ! Cognee 删除失败：{exc}")
    # 4) 飞书
    delete_feishu(entry, root, False)
    # 5) 台账
    entry["status"] = "deleted"
    entry["deleted_at"] = now_iso()
    entry["delete_mode"] = args.mode
    save_manifest(root, manifest)
    # 6) 审计
    try:
        audit_append(root, {"file": name, "sha256": entry.get("sha256"),
                            "action": "delete", "project": project,
                            "category": entry.get("category"), "md_path": md_rel,
                            "status": "deleted",
                            "note": f"mode={args.mode} identifier={args.identifier}"})
    except Exception as exc:
        print(f"  ! 审计写入失败：{exc}")
    # 7) cognify
    if dataset:
        try:
            _cg.cognify(dataset, key, log=log)
        except Exception as exc:
            print(f"  ! cognify 失败：{exc}")
    # 8) 通知
    if not args.no_notify:
        notify_send(f"社工知识库：已删除 {name}（{args.mode}）", root)
    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
