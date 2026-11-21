#!/usr/bin/env python3
"""删除一个已归档的文档：vault md + sources 原件 + 台账。

用法：
  delete.py <标识> [--mode trash|purge] --yes [--no-notify] [--config PATH]

<标识> 可以是：台账 token、文件名、md 相对路径、飞书 file_token。
没有 --yes 时只打印计划并拒绝执行（退出码 2），绝不落盘。
  trash（默认）—— md 移到 <vault>/.trash/<日期>/，原件移到
                    sources/.trash/<日期>/，台账 status=deleted，可恢复
  purge        —— md / 原件直接删除，台账 status=deleted，不可恢复

台账统一走 manifest.py 的 lock / load / update_entry，不在这里解析 YAML。
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

import cognee as _cg
import manifest as _mf
from reconcile import (TRASH_DIRNAME, TZ, delete_feishu, load_manifest, log,
                       now_iso)

# audit_log.py 只存在于生产目录；仓库里没有时跳过审计，不影响删除主流程。
try:  # pragma: no cover - 取决于部署环境
    from audit_log import append as audit_append
except ImportError:  # pragma: no cover
    audit_append = None


def find_entries(manifest, ident):
    """按 token / 文件名 / md 路径 / 飞书 token 找台账条目。"""
    out = []
    for token, entry in (manifest.get("files") or {}).items():
        if not isinstance(entry, dict):
            continue
        if ident in (token, entry.get("name"), entry.get("md"),
                     entry.get("feishu_token")):
            out.append((token, entry))
    return out


def resolve_vault(root, flow, project, md_rel):
    """确定 md 所在的 vault 目录。

    优先用 feishu-flow.yaml 里配置的 vault；没配置时从 md 相对路径的第一段
    （vault-huimin / vault-huadu）推断，最后才退回 <root>/vault。
    """
    pcfg = (flow.get("projects", {}) or {}).get(project, {}) or {}
    configured = pcfg.get("vault")
    if configured:
        return Path(root) / configured
    if md_rel:
        first = Path(md_rel).parts[0] if Path(md_rel).parts else ""
        if first.startswith("vault") and (Path(root) / first).is_dir():
            return Path(root) / first
    return Path(root) / "vault"


def trash_md(root, vault, md_rel):
    """把 md 移到 <vault>/.trash/<日期>/<vault 内相对路径>，返回目标相对路径。"""
    md_path = Path(root) / md_rel
    if not md_path.is_file():
        return None
    try:
        rel = Path(md_rel).relative_to(Path(vault).name)
    except ValueError:
        # md 不在预期的 vault 下：保留完整相对路径，避免同名文件互相覆盖
        rel = Path(md_rel)
    dest = (Path(vault) / TRASH_DIRNAME
            / datetime.now(TZ).strftime("%Y-%m-%d") / rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(md_path), str(dest))
    os.chmod(dest, 0o644)
    return dest.relative_to(root).as_posix()


def find_orphan_mds(root, vault, stem, keep_rel):
    """同名 md 的其他副本（重复上传残留的孤儿目录），返回相对路径列表。

    只认同一 vault/md 下、与台账 md 同 stem 的 .md；排除 .trash 与台账指向的那份。
    孤儿 md 不被台账引用，删台账 md 时不会被动到，随后会被 reconcile 重新索引，
    让已删文档在 Cognee 里复活 —— 所以删除时要一并清掉。
    """
    out = []
    if not stem:
        return out
    md_root = Path(vault) / "md"
    if not md_root.is_dir():
        return out
    keep = _mf.normalize_rel(str(keep_rel)) if keep_rel else ""
    for p in md_root.rglob("*.md"):
        if ".trash" in p.parts or p.stem != stem:
            continue
        rel = _mf.rel_posix(p, root)
        if rel and rel != keep:
            out.append(rel)
    return out


def trash_source(root, orig_rel):
    """把 sources 下的原件移到 sources/.trash/<日期>/<原相对路径>。

    返回目标相对路径；原件不存在返回 None。
    """
    src = Path(root) / orig_rel
    if not src.is_file() or TRASH_DIRNAME in src.parts:
        return None
    rel = src.relative_to(Path(root))
    sub = rel.parts[1:] if rel.parts[:1] == ("sources",) else (src.name,)
    dest = (Path(root) / "sources" / TRASH_DIRNAME
            / datetime.now(TZ).strftime("%Y-%m-%d") / Path(*sub))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return dest.relative_to(root).as_posix()


def purge_path(root, rel):
    """真删一个相对路径；文件不存在返回 False。"""
    if not rel:
        return False
    p = Path(root) / rel
    if not p.is_file():
        return False
    p.unlink()
    return True


def clear_inbox_copy(root, entry):
    """清掉 inbox 里的上传暂存副本，避免定时扫描把已删文档重新归档。"""
    inbox = Path(root) / "inbox"
    removed = []
    for name in {str(entry.get("original_name") or ""),
                 str(entry.get("name") or "")}:
        if not name:
            continue
        for p in (inbox / name, inbox / (name + ".meta.json")):
            try:
                if p.is_file():
                    p.unlink()
                    removed.append(p.relative_to(root).as_posix())
            except OSError:
                pass
    return removed


def main(argv=None):
    ap = argparse.ArgumentParser(description="删除一个已归档的文档")
    ap.add_argument("identifier", help="台账 token / 文件名 / md 路径 / 飞书 file_token")
    ap.add_argument("--mode", choices=["trash", "purge"], default="trash")
    ap.add_argument("--yes", action="store_true", help="确认执行（没有它只预览并拒绝）")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--config", metavar="PATH")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    root = Path(cfg.root)
    flow_path = root / "feishu-flow.yaml"
    flow = {}
    if flow_path.is_file():
        flow = yaml.safe_load(flow_path.read_text(encoding="utf-8")) or {}
    manifest = load_manifest(root)

    matches = find_entries(manifest, args.identifier)
    if not matches:
        print(f"找不到匹配的文档：{args.identifier}")
        return 1
    if len(matches) > 1:
        print("匹配到多个，请用更精确的标识：")
        for token, entry in matches:
            print(f"  {token}  {entry.get('name')}  [{entry.get('status')}]")
        return 2

    token, entry = matches[0]
    name = entry.get("name") or token
    project = entry.get("project")
    md_rel = entry.get("md")
    orig_rel = entry.get("original")
    vault = resolve_vault(root, flow, project, md_rel)

    print(f"删除 {name}")
    print(f"  项目/类别：{project} / {entry.get('category')}")
    print(f"  台账 token：{token}")
    print(f"  md：{md_rel}")
    print(f"  原件：{orig_rel}")
    print(f"  飞书：{entry.get('feishu_token')}")
    print(f"  模式：{args.mode}")

    if not args.yes:
        print("\n拒绝执行：删除会改动 md / 原件 / 台账，确认后加 --yes")
        return 2

    # 1) md（含同 stem 的孤儿 md：重复上传残留、台账不引用，
    #    只删台账那份的话孤儿会被 reconcile 重新索引，已删文档在 Cognee 复活）
    stem = Path(md_rel).stem if md_rel else Path(name).stem
    md_rels = [md_rel] if md_rel else []
    if md_rel:
        md_rels += find_orphan_mds(root, vault, stem, md_rel)
    for rel in md_rels:
        if args.mode == "trash":
            moved = trash_md(root, vault, rel)
            if moved:
                print(f"  md → {moved}")
        elif purge_path(root, rel):
            print(f"  md 已删除：{rel}")

    # 2) sources 原件
    if orig_rel:
        if args.mode == "trash":
            moved = trash_source(root, orig_rel)
            if moved:
                print(f"  原件 → {moved}")
        elif purge_path(root, orig_rel):
            print(f"  原件已删除：{orig_rel}")

    # 3) inbox 上传副本
    for rel in clear_inbox_copy(root, entry):
        print(f"  inbox 副本已清：{rel}")

    # 4) Cognee（失败不阻断：文件与台账才是真相源，索引可由对账重建）
    dataset = ((flow.get("projects", {}) or {}).get(project, {}) or {}).get("dataset")
    key = None
    if dataset:
        try:
            key = _cg.load_key()
            n = _cg.delete_by_name(dataset, stem, key, log=log)
            print(f"  Cognee 条目已删：{n}")
        except Exception as exc:  # noqa: BLE001 - 外部服务不可用不应阻断删除
            print(f"  ! Cognee 删除失败：{exc}")

    # 5) 飞书（删失败只告警，台账仍标 deleted；reconcile 会继续重试）
    if not delete_feishu(entry, root, False):
        print("  ! 飞书删除失败，台账仍标记 deleted")

    # 6) 台账：锁内只改本条目，避免覆盖并发写入
    deleted_at = now_iso()
    _mf.update_entry(Path(root) / "state" / "manifest.yaml", token, {
        "status": "deleted",
        "deleted_at": deleted_at,
        "delete_mode": args.mode,
    })

    # 7) 审计（仓库里没有 audit_log.py 时跳过）
    if audit_append is not None:
        try:
            audit_append(root, {
                "file": name, "sha256": entry.get("sha256"),
                "action": "delete", "project": project,
                "category": entry.get("category"), "md_path": md_rel,
                "status": "deleted",
                "note": f"mode={args.mode} identifier={args.identifier}",
            })
        except Exception as exc:  # noqa: BLE001 - 审计不可用不阻断删除
            print(f"  ! 审计写入失败：{exc}")

    # 8) cognify（删完条目后重建图谱）
    if dataset and key:
        try:
            _cg.cognify(dataset, key, log=log)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! cognify 失败：{exc}")

    # 9) 通知
    if not args.no_notify:
        notify_send(f"社工知识库：已删除 {name}（{args.mode}）", root)

    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
