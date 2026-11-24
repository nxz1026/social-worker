"""活动目录自动整理：把放错位置 / 命名不规范的材料归位并改名。

判定完全复用 engine.checks 的核对结果，不另起一套规则——
「核对认为应该在哪」就是「整理该搬到哪」。

规则：
1. 根目录 + 规范名（`03-签到表.pdf`）→ 不动
2. 有文件名但对不上序号前缀 → 改名到根目录规范名
3. 放在子目录里（如在 11-活动照片资料/ 里放签到表.pdf）→ 移到根目录并改名
4. 目标名已被占用 → 不覆盖，生成带后缀的新名并提示

只搬/改名，**不动内容**。所有动作先出方案，用户确认后执行。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import checks


def _target_name(item: dict, src: Path) -> str:
    """规范文件名：`<首个序号前缀><清单项名><原扩展名>`。

    保留扩展名；不保留原文件名里的活动名等内容（那部分在根目录已由
    序号+项名表达，再加反而乱）。
    """
    prefixes = checks._item_prefixes(item)
    head = prefixes[0] if prefixes else ""
    return f"{head}{item['name']}{src.suffix.lower()}"


# 15 项清单之外的补充编号（报销单 21-、领取表 23~26 等），已规范，不动
_EXTRA_SEQ = {"21-", "22-", "23-", "24-", "25-", "26-", "27-"}


def _has_valid_ext_seq(src: Path) -> bool:
    """文件名是否已带清单外的合法补充编号。"""
    return any(src.name.startswith(seq) for seq in _EXTRA_SEQ)


def _is_canonical(path: Path, act_dir: Path, item: dict) -> bool:
    """是否已经在规范位置 + 规范名。"""
    try:
        rel = path.relative_to(act_dir.resolve())
    except ValueError:
        return False
    if len(rel.parts) != 1:
        return False                       # 不在根目录
    prefixes = checks._item_prefixes(item)
    return any(rel.name.startswith(p) for p in prefixes)


def plan(act_dir: Path) -> dict:
    """给一个活动目录出整理方案。

    返回 {"actions": [...], "skipped": [...]}：
      actions 每项 {item, item_name, from, to, reason}
      from/to 都是相对活动目录的 posix 路径
    """
    act_dir = Path(act_dir).resolve()
    result = checks.check(act_dir)
    actions: list[dict] = []
    skipped: list[dict] = []
    taken: set[str] = set()

    for got in result["items"]:
        rel = got.get("path")
        if not rel:
            continue
        src = (act_dir / rel)
        item = next((c for c in checks.CHECKLIST if c["no"] == got["no"]), None)
        if item is None:
            continue
        if got["status"] not in ("ok", "partial"):
            continue
        if src.is_dir():
            # 目录类：只处理「名字不符合 序号 开头」的目录，不搬内容
            prefixes = checks._item_prefixes(item)
            if src.parent == act_dir and any(src.name.startswith(p) for p in prefixes):
                continue
            dst_name = _target_name(item, src)
            actions.append({
                "item": item["no"], "item_name": item["name"],
                "from": rel, "to": dst_name, "kind": "dir",
                "reason": f"目录名不是 {prefixes[0] if prefixes else ''} 开头",
            })
            continue
        if _is_canonical(src, act_dir, item):
            continue
        # 已经带「清单范围之外」的规范序号（21-报销单、25-物资领取表…）就别动：
        # 它们是补充编号，本身就是对的。只整理真正没编号或编号不匹配的。
        if _has_valid_ext_seq(src):
            skipped.append({"item": item["no"], "from": rel,
                            "reason": f"已带补充编号 {src.name[:3]}，保持原样"})
            continue
        dst_name = _target_name(item, src)
        if dst_name in taken or (act_dir / dst_name).exists():
            stem, suffix = dst_name.rsplit(".", 1) if "." in dst_name else (dst_name, "")
            n = 2
            while f"{stem}-{n}.{suffix}" in taken or (act_dir / f"{stem}-{n}.{suffix}").exists():
                n += 1
            dst_name = f"{stem}-{n}.{suffix}" if suffix else f"{stem}-{n}"
            reason = "目标名已被占用，改用带序号的新名"
        else:
            reason = ("在子目录里" if src.parent != act_dir
                      else "缺序号前缀")
        taken.add(dst_name)
        actions.append({
            "item": item["no"], "item_name": item["name"],
            "from": rel, "to": dst_name, "kind": "file", "reason": reason,
        })

    # 同名 .md 骨架跟着搬/改名
    for act in actions:
        src = act_dir / act["from"]
        md = src.with_suffix(".md")
        if act["kind"] == "file" and md.is_file() and md.parent == src.parent:
            md_to = str(Path(act["to"]).with_suffix(".md"))
            if not (act_dir / md_to).exists():
                act["md_from"] = md.relative_to(act_dir).as_posix()
                act["md_to"] = md_to

    return {"actions": actions, "skipped": skipped, "dir": act_dir.name}


def apply(act_dir: Path, actions: list[dict]) -> dict:
    """执行方案。逐条搬，失败即停并回报已完成的部分（不做回滚，
    因为 mv 是原子的、且都在活动目录内）。"""
    act_dir = Path(act_dir).resolve()
    done: list[dict] = []
    errors: list[dict] = []
    for act in actions:
        src = act_dir / act["from"]
        dst = act_dir / act["to"]
        try:
            if src.is_dir():
                if dst.exists():
                    raise FileExistsError(f"目标已存在：{act['to']}")
                shutil.move(str(src), str(dst))
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    raise FileExistsError(f"目标已存在：{act['to']}")
                shutil.move(str(src), str(dst))
            if act.get("md_from"):
                md_src = act_dir / act["md_from"]
                md_dst = act_dir / act["md_to"]
                if md_src.is_file() and not md_dst.exists():
                    shutil.move(str(md_src), str(md_dst))
            done.append(act)
        except Exception as exc:                      # noqa: BLE001
            errors.append({"from": act.get("from"), "error": str(exc)})
    return {"moved": len(done), "errors": errors, "actions": done}
