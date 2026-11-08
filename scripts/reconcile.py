#!/usr/bin/env python3
"""对账：vault md 的增/改/删 → Cognee / 飞书 / 台账 / 审计。

vault 是真相源。用户在 Obsidian 里改的、删的，经 Syncthing 同步到服务器后，
由这条命令传播到所有投影：
  - Cognee：镜像 md（加/改/删），有变更才 cognify
  - 飞书：md 消失 → 删掉对应飞书文档；md 恢复 → 重新上传一份
  - 台账：维护 md_sha256 / status / deleted_at / restored_at
  - 通知：本轮有变更时发飞书私信

用法：
  reconcile.py [--dry-run] [--dataset NAME] [--no-notify] [--config PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from config import load_config
from notify import send as notify_send

import cognee as _cg

TZ = timezone(timedelta(hours=8))
MANIFEST_REL = "state/manifest.yaml"
TRASH_DIRNAME = ".trash"


# ---------- PG 审计 ----------
def pg_connect(dsn: str | None = None):
    """连接社工系统 PG（audit_log）。失败返回 None（审计不可用不阻断主流程）。"""
    dsn = dsn or os.environ.get("SOCIALWORK_DB_DSN", "")
    if not dsn:
        return None
    try:
        import psycopg
        return psycopg.connect(dsn)
    except Exception as exc:
        log(f"  ! PG 审计连接失败：{exc}")
        return None


def pg_audit(conn, action: str, project: str = None, target: str = None,
             detail: dict | None = None):
    """写 audit_log（user_id=NULL 表示系统操作）。"""
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO audit_log(user_id, action, project, target, detail, ip)
                   VALUES(NULL, %s, %s, %s, %s, 'reconcile')""",
                (action, project, target,
                 json.dumps(detail, ensure_ascii=False) if detail else None))
        conn.commit()
    except Exception as exc:
        log(f"  ! PG 审计写入失败：{exc}")


def log(msg):
    print(msg, flush=True)


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(root):
    p = Path(root) / MANIFEST_REL
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("files", {})
    return data


def save_manifest(root, manifest):
    p = Path(root) / MANIFEST_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


def lark(args, root, timeout=600):
    cmd = ["lark-cli"] + args + ["--as", "user", "--format", "json"]
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                          timeout=timeout)
    out = proc.stdout or ""
    starts = [i for i in (out.find("{"), out.find("[")) if i >= 0]
    if proc.returncode != 0 or not starts:
        raise RuntimeError((proc.stderr or out).strip()[:300])
    return json.loads(out[min(starts):])


def delete_feishu(entry, root, dry_run):
    """删掉台账里记的飞书文档。返回是否删了（dry-run 下「会删」）。"""
    token = entry.get("feishu_token")
    if not token:
        return False
    if dry_run:
        log(f"  [dry-run] 删除飞书文档 {token}")
        return True
    try:
        lark(["drive", "+delete", "--file-token", token, "--type",
              entry.get("feishu_type") or "file", "--yes"], root)
        return True
    except Exception as exc:
        log(f"  ! 飞书删除失败 {token}：{exc}")
        return False


def restore_feishu(entry, root, flow, dry_run):
    """md 恢复了 → 重新上传一份到飞书「已分类」，返回新 token（失败返回空串）。"""
    original = entry.get("original")
    project = entry.get("project")
    category = entry.get("category")
    if not (original and project and category):
        return ""
    src = Path(root) / original
    if not src.is_file():
        log(f"  ! 恢复失败：原件不存在 {original}")
        return ""
    label = (flow.get("projects", {}).get(project, {}) or {}).get("label", project)
    dest_folder = (flow.get("category_folders", {}).get(label, {}) or {}).get(category)
    if not dest_folder:
        log(f"  ! 恢复失败：找不到 {label}/{category} 的飞书目录")
        return ""
    if dry_run:
        log(f"  [dry-run] 重新上传飞书 {src.name} → {dest_folder}")
        return ""
    try:
        data = lark(["drive", "+upload", "--file", str(src.relative_to(root)),
                     "--folder-token", dest_folder], root)
    except Exception as exc:
        log(f"  ! 飞书重传失败 {src.name}：{exc}")
        return ""
    return ((data.get("data") or {}).get("file_token") or data.get("file_token") or "")


def md_state(root, entry):
    """返回 (state, current_sha256)。state ∈ {no_md, missing, changed, same}。

    注意：台账里的 md_sha256 是 sha256（scan-inbox 写入的也是 sha256），
    这里必须用 sha256 比对，否则每次对账都会误判成「内容变了」。
    """
    rel = entry.get("md")
    if not rel:
        return "no_md", None
    p = Path(root) / rel
    if not p.is_file():
        return "missing", None
    cur = sha256_of(p)
    return ("same" if cur == entry.get("md_sha256") else "changed"), cur


def reconcile_dataset(root, pcfg, flow, manifest, project_key, key, dry_run,
                      pg_conn=None, purge=False):
    """对账一个项目，返回统计 dict（含 changes 列表）。"""
    dataset = pcfg["dataset"]
    vault = Path(root) / pcfg["vault"]
    stats = {"added": 0, "updated": 0, "deleted": 0, "restored": 0,
             "feishu_deleted": 0, "purged": 0, "errors": 0, "changes": []}

    st = _cg.sync_dataset(vault / "md", dataset, key, dry_run=dry_run, log=log)
    stats["added"] += st.get("added", 0)
    stats["updated"] += st.get("updated", 0)
    stats["deleted"] += st.get("deleted", 0)
    if st.get("error"):
        log(f"  ! Cognee：{st['error']}")
        stats["errors"] += 1

    for token, entry in list(manifest["files"].items()):
        if entry.get("project") != project_key:
            continue
        if entry.get("status") in ("pending", "pending_review", "unsupported"):
            continue
        state, cur = md_state(root, entry)
        if state == "no_md":
            continue
        name = entry.get("name") or token

        if entry.get("status") == "deleted":
            if state == "missing":
                continue
            # md 回来了 → 恢复（内容可能又被改过）
            new_token = restore_feishu(entry, root, flow, dry_run)
            if new_token:
                entry["feishu_token"] = new_token
            elif not dry_run:
                # 真跑但恢复失败（原件缺失 / 飞书上传失败）：保持 deleted 下轮重试，
                # 不能静默把飞书文档丢掉（spec 参考实现会直接标 done，这里改掉）
                log(f"  ! 恢复未成功：{name}（原件缺失或飞书上传失败），下轮重试")
                stats["errors"] += 1
                continue
            entry["md_sha256"] = cur
            entry["status"] = "done"
            entry["restored_at"] = now_iso()
            entry.pop("deleted_at", None)
            entry.pop("delete_mode", None)
            stats["restored"] += 1
            stats["changes"].append(f"恢复 {name}")
            log(f"  · 恢复 {name}")
            pg_audit(pg_conn, "restore", project_key, name,
                     {"md": entry.get("md")})
            continue

        if state == "missing":
            if delete_feishu(entry, root, dry_run):
                stats["feishu_deleted"] += 1
            if purge:
                # 连原件一起删（彻底删除，不可恢复）
                orig = entry.get("original")
                if orig and (Path(root) / orig).is_file():
                    if not dry_run:
                        try:
                            (Path(root) / orig).unlink()
                        except OSError as exc:
                            log(f"  ! 原件删除失败 {orig}：{exc}")
                            stats["errors"] += 1
                            continue
                    stats["purged"] += 1
            entry["status"] = "deleted"
            entry["deleted_at"] = now_iso()
            entry["delete_mode"] = "purge" if purge else "auto"
            stats["deleted"] += 1
            stats["changes"].append(f"删除 {name}" + ("（含原件）" if purge else ""))
            log(f"  · 删除 {name}（md 已消失" + ("，原件已删）" if purge else "）"))
            pg_audit(pg_conn, "delete", project_key, name,
                     {"md": entry.get("md"), "feishu_deleted": True,
                      "purge": purge})
            continue

        if state == "changed":
            if not entry.get("md_sha256"):
                entry["md_sha256"] = cur      # 老台账补字段，不算变更
                continue
            entry["md_sha256"] = cur
            stats["updated"] += 1
            stats["changes"].append(f"更新 {name}")
            pg_audit(pg_conn, "update", project_key, name,
                     {"md": entry.get("md")})
    return stats


def main():
    ap = argparse.ArgumentParser(description="对账：vault md → Cognee / 飞书 / 台账")
    ap.add_argument("--config", metavar="PATH")
    ap.add_argument("--dataset", action="append", default=[],
                    help="只处理指定数据集（可多次）；默认全部")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--pg-dsn", default=None, help="社工系统 PG DSN（写审计）")
    ap.add_argument("--purge", action="store_true",
                    help="删除时连原件（sources/）一起删，不可恢复")
    args = ap.parse_args()

    cfg = load_config(args.config)
    root = Path(cfg.root)
    flow_path = root / "feishu-flow.yaml"
    flow = yaml.safe_load(flow_path.read_text(encoding="utf-8")) if flow_path.is_file() else {}
    flow = flow or {}
    manifest = load_manifest(root)

    key = _cg.load_key()
    projects = flow.get("projects") or {}
    targets = set(args.dataset) or {p.get("dataset") for p in projects.values()}
    pg_conn = pg_connect(args.pg_dsn)

    total = {"added": 0, "updated": 0, "deleted": 0, "restored": 0,
             "feishu_deleted": 0, "purged": 0, "errors": 0}
    lines = []
    for project_key, pcfg in projects.items():
        if pcfg.get("dataset") not in targets:
            continue
        log(f"=== {project_key}（{pcfg.get('dataset')}）===")
        st = reconcile_dataset(root, pcfg, flow, manifest, project_key, key,
                               args.dry_run, pg_conn, purge=args.purge)
        for k in total:
            total[k] += st.get(k, 0)
        if st["changes"]:
            lines.append(f"{project_key}：" + "、".join(st["changes"]))

    # dry-run 只报计划，不落盘
    if not args.dry_run:
        save_manifest(root, manifest)
    if pg_conn is not None:
        pg_conn.close()
    log(f"\n完成：+{total['added']} ~{total['updated']} -{total['deleted']}"
        f" 恢复 {total['restored']} 飞书删除 {total['feishu_deleted']}"
        + (f" 原件清除 {total['purged']}" if total["purged"] else "")
        + (f" 失败 {total['errors']}" if total["errors"] else ""))
    if lines and not args.no_notify and not args.dry_run:
        notify_send("社工知识库对账：\n" + "\n".join(lines), root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
