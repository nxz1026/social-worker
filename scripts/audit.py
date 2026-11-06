#!/usr/bin/env python3
"""审计日志 → 人读摘要。

读 state/audit/YYYY-MM-DD.jsonl（由 audit_log.append() 写入），
输出当日归档情况；`--send` 发飞书汇总。

用法
  python3 scripts/audit.py                      # 今天
  python3 scripts/audit.py --date 2026-11-06
  python3 scripts/audit.py --date 2026-11-06 --send
  python3 scripts/audit.py --date 2026-11-06 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent

TZ = timezone(timedelta(hours=8))

IMAGE_ACTIONS = {"archive_only", "ocr", "vision"}
ACTION_LABEL = {"archive_only": "只存档", "ocr": "OCR", "vision": "识图"}

# status → 归类
FAIL_STATUS = {"unsupported", "error", "failed"}


def load_records(root, date: str) -> list[dict]:
    p = Path(root) / "state" / "audit" / f"{date}.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def hhmm(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except Exception:
        return (ts or "")[-8:]


def project_labels(root) -> dict:
    """项目 key → 中文标签（feishu-flow.yaml 的 projects.<key>.label）。"""
    p = Path(root) / "feishu-flow.yaml"
    if not p.is_file():
        return {}
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {k: v.get("label", k) for k, v in (cfg.get("projects") or {}).items()
            if isinstance(v, dict)}


def summarize(records: list[dict], labels: dict | None = None) -> dict:
    labels = labels or {}
    docs = [r for r in records if r.get("action") not in IMAGE_ACTIONS]
    imgs = [r for r in records if r.get("action") in IMAGE_ACTIONS]
    ok_docs = [r for r in docs if r.get("status") == "done"]
    failed = [r for r in records if r.get("status") in FAIL_STATUS
              or (r.get("status") == "pending" and r.get("error"))]
    pending_review = [r for r in records if r.get("status") == "pending_review"]

    converter = Counter(r.get("converter") or "?" for r in ok_docs)
    projects = Counter(labels.get(r.get("project") or "", r.get("project") or "待确认")
                       for r in records)
    categories = Counter((r.get("category") or "待确认") for r in records)
    actions = Counter(ACTION_LABEL.get(r.get("action"), "?") for r in imgs)
    return {
        "total": len(records),
        "docs": len(docs),
        "docs_ok": len(ok_docs),
        "converter": dict(converter),
        "imgs": len(imgs),
        "actions": dict(actions),
        "projects": dict(projects),
        "categories": dict(categories),
        "failed": failed,
        "pending_review": pending_review,
    }


def render(s: dict, date: str) -> str:
    lines = [f"=== {date} 审计 ===", f"收到 {s['total']} 个文件"]
    if s["docs"]:
        conv = " / ".join(f"{k} {v}" for k, v in sorted(s["converter"].items()))
        lines.append(f"  文档 {s['docs']}：成功 {s['docs_ok']}"
                     + (f"（{conv}）" if conv else ""))
    if s["imgs"]:
        act = " / ".join(f"{k} {v}" for k, v in sorted(s["actions"].items()))
        lines.append(f"  图片 {s['imgs']}：{act}")
    if s["projects"]:
        lines.append("  项目分布：" + " / ".join(
            f"{k} {v}" for k, v in sorted(s["projects"].items(),
                                          key=lambda kv: (-kv[1], kv[0] == "待确认", kv[0]))))
    if s["categories"]:
        lines.append("  类别分布：" + " / ".join(
            f"{k} {v}" for k, v in sorted(s["categories"].items(),
                                          key=lambda kv: (-kv[1], kv[0] == "待确认", kv[0]))))
    if s["failed"]:
        lines.append(f"失败 {len(s['failed'])}")
        for r in s["failed"]:
            lines.append(f"  [{hhmm(r.get('ts',''))}] {r.get('file','')} — "
                         f"{r.get('error') or r.get('status','')}")
    if s["pending_review"]:
        lines.append(f"待兜底 {len(s['pending_review'])}")
        for r in s["pending_review"]:
            lines.append(f"  [{hhmm(r.get('ts',''))}] {r.get('file','')} — "
                         f"项目无法判定，落 _待确认/{r.get('category','')}/")
    if not s["failed"] and not s["pending_review"]:
        lines.append("无失败、无待兜底")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="审计日志摘要")
    ap.add_argument("--date", help="YYYY-MM-DD（默认今天）")
    ap.add_argument("--root", default="/srv/social-work")
    ap.add_argument("--send", action="store_true", help="发飞书汇总")
    ap.add_argument("--json", action="store_true", help="输出原始统计 JSON")
    a = ap.parse_args()

    date = a.date or datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        print(f"日期格式不对：{date}（要 YYYY-MM-DD）", file=sys.stderr)
        return 2

    records = load_records(a.root, date)
    s = summarize(records, project_labels(a.root))
    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    text = render(s, date)
    print(text)
    if a.send:
        # 延迟 import：不发送时不必依赖 notify.py
        import importlib.util
        spec = importlib.util.spec_from_file_location("notify", SCRIPT_DIR / "notify.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        ok = m.send(f"【社工知识库 {date}】\n{text}", a.root)
        print("飞书：已发送" if ok else "飞书：发送失败（见上面日志）")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
