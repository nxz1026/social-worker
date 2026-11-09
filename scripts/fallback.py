#!/usr/bin/env python3
"""生成「待兜底清单」md —— 交给 DSH/LLM 处理的工单。

背景：分类置信度低、项目判不了、转换失败的文件会落 `_待确认/` 或进错误队列。
这些**必须有人处理**，但改之前没有任何汇总，只能靠人肉翻日志。
本脚本把当天的待处理项汇总成一份 md，丢给 DSH 就能让 LLM 处理。

兜底边界（写死在输出里，LLM 必须遵守）：
  - 只能做：判断项目/类别、给文件改名、补写说明、把结论写回 fallback_log
  - 不能做：决定隐私相关的事；直接往 sources/ 写或改原件；瞎猜

用法
  python3 scripts/fallback.py --date 2026-11-06          # 生成清单 md
  python3 scripts/fallback.py --date 2026-11-06 --out x.md
  python3 scripts/fallback.py --list                     # 只看有几条，不生成
  python3 scripts/fallback.py --log '{"file":"x.pdf","action":"renamed","note":"..."}'
"""

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from error_queue import list_open  # noqa: E402

TZ = timezone(timedelta(hours=8))

# 审计记录里需要人处理的 status
PENDING_STATUS = {"pending_review", "error", "unsupported", "failed"}

RULES = """## 处理规则（必须遵守）

1. **只能**做：判断项目/类别、给文件改名、补写说明、把结论写进 `state/fallback_log.jsonl`。
2. **不能**决定隐私相关的事（哪些能公开、能不能给外部看）—— 拿不准就写「需人工确认」。
3. **不能**直接往 `sources/` 写或改原件。
4. 每条都要填「结论」；判断不了的写「需人工确认」，**不要猜**。
"""


def _load_audit():
    spec = importlib.util.spec_from_file_location("audit", SCRIPT_DIR / "audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mask(file: str) -> str:
    """清单标题用脱敏名：保留目录与扩展名，文件名换 hash。"""
    p = PurePosixPath(str(file or "").replace("\\", "/"))
    digest = hashlib.sha256(p.stem.encode("utf-8")).hexdigest()[:8]
    return str(p.with_name(f"{digest}{p.suffix}")) if p.name else "?"


def _locate(root, file, md_path=None):
    """尽量给出文件的真实位置；找不到就说明它还没归档。"""
    if md_path:
        return md_path
    # 未分类原件落 sources/_待确认/<类别>/（见 scan-inbox.py），类别目录名不固定，
    # 逐个类别目录找一遍
    pending = root / "sources" / "_待确认"
    cands = [root / "inbox" / file, pending / file]
    if pending.is_dir():
        cands += [d / file for d in sorted(pending.iterdir()) if d.is_dir()]
    for cand in cands:
        if cand.is_file():
            return str(cand)
    return "未归档（原件可能还在飞书「未分类」或本地 inbox/）"


def collect(root, date=None):
    """收集待兜底项。返回 list[dict]（每项含 file/problem/where/task）。"""
    root = Path(root)
    day = date or datetime.now(TZ).strftime("%Y-%m-%d")
    items = []

    audit = _load_audit()
    for rec in audit.load_records(root, day):
        status = rec.get("status")
        if status not in PENDING_STATUS:
            continue
        problem = rec.get("error") or rec.get("note") or {
            "pending_review": "分类置信度低 / 项目判定不了",
            "unsupported": "格式不支持",
            "failed": "处理失败",
            "error": "处理出错",
        }.get(status, status)
        items.append({
            "file": rec.get("file", "?"),
            "display": rec.get("display") or _mask(rec.get("file", "?")),
            "problem": str(problem)[:200],
            "where": _locate(root, rec.get("file", "?"), rec.get("md_path")),
            "task": "判断该归哪个项目/类别（或确认是否该删）",
        })

    for err in list_open(root):
        items.append({
            "file": err.get("file", "?"),
            "display": err.get("display") or _mask(err.get("file", "?")),
            "problem": err.get("error", "?"),
            "where": _locate(root, err.get("file", "?")),
            "task": "判断错误原因；能修的修，不能修的说明",
        })

    # 同文件去重（审计里已有就不再从错误队列重复加）
    seen, uniq = set(), []
    for it in items:
        key = (it["file"], it["problem"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq


def render(items, date, root=None):
    now = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    out = [f"# 待兜底清单 · {date}", ""]
    out.append(f"> 生成时间：{now}")
    if root:
        out.append(f"> 根目录：`{root}`")
    out.append(f"> 待处理：**{len(items)}** 条")
    out.append("")
    out.append(RULES)

    if not items:
        out.append("（无待处理项）")
        return "\n".join(out) + "\n"

    for i, it in enumerate(items, 1):
        out.append(f"## {i}. {it['file']}")
        out.append("")
        out.append(f"- **问题**：{it['problem']}")
        out.append(f"- **当前位置**：`{it['where']}`")
        out.append(f"- **你的任务**：{it['task']}")
        out.append("- **结论**：<填这里>")
        out.append("")
    return "\n".join(out) + "\n"


def record_log(root, payload):
    """把 LLM 的兜底结论写进 `state/fallback_log.jsonl`。"""
    root = Path(root)
    p = root / "state" / "fallback_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now(TZ).isoformat(timespec="seconds")}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"note": payload}
    entry.update(payload or {})
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def main():
    ap = argparse.ArgumentParser(description="生成待兜底清单 md（供 LLM 处理）")
    ap.add_argument("--date", help="YYYY-MM-DD，默认今天")
    ap.add_argument("--root", default="/srv/social-work", help="服务器上的数据根目录")
    ap.add_argument("--out", help="输出路径，默认打印到 stdout")
    ap.add_argument("--list", action="store_true", help="只打印条数")
    ap.add_argument("--log", metavar="JSON", help="把一条兜底结论写进 fallback_log.jsonl")
    args = ap.parse_args()

    if args.log:
        entry = record_log(args.root, args.log)
        print(json.dumps(entry, ensure_ascii=False))
        return 0

    items = collect(args.root, args.date)
    if args.list:
        print(f"待兜底 {len(items)} 条")
        for it in items:
            print(f"  - {it['file']}：{it['problem'][:60]}")
        return 0

    text = render(items, args.date or datetime.now(TZ).strftime("%Y-%m-%d"), args.root)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"已写入 {out}（{len(items)} 条）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
