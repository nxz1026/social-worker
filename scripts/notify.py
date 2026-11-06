#!/usr/bin/env python3
"""飞书通知统一出口。

M3 的每日汇总、M4 的失败告警都走这里，避免两处各写一份 lark-cli 调用。

失败只返回 False 并打日志，**不抛异常** —— 通知挂掉不该拖垮归档主流程。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml


def _dsh_owner_ids() -> list[str]:
    """回落收件人：DSH 飞书机器人配置里的 ownerOpenIds。"""
    p = Path.home() / ".dsh" / "integrations" / "dsh-feishu" / "config.json"
    if not p.is_file():
        return []
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[str] = []
    for bot in cfg.get("bots", []):
        for uid in bot.get("ownerOpenIds") or []:
            if str(uid).startswith("ou_"):
                out.append(str(uid))
    return out


def load_targets(root, config_path: Path | None = None) -> list[str]:
    """收件人 open_id 列表。

    优先 `config/notify.yaml`（服务器本地，不入库，可放多人）；
    缺失或没有有效 open_id 时回落到 DSH 的飞书机器人配置。
    """
    p = Path(config_path) if config_path else (Path(root) / "config" / "notify.yaml")
    if p.is_file():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        ids = [str(x) for x in (data.get("to") or []) if str(x).startswith("ou_")]
        if ids:
            return ids
    return _dsh_owner_ids()


def send(text: str, root, config_path: Path | None = None,
         as_identity: str = "bot", timeout: int = 120) -> bool:
    """发飞书私信。全部成功返回 True，否则 False。"""
    targets = load_targets(root, config_path)
    if not targets:
        print("notify: 没有收件人（config/notify.yaml 缺失且读不到 dsh-feishu 配置）",
              file=sys.stderr)
        return False
    ok = True
    for uid in targets:
        cmd = ["lark-cli", "im", "+messages-send",
               "--user-id", uid, "--text", text,
               "--as", as_identity, "--format", "json"]
        try:
            r = subprocess.run(cmd, cwd=str(root), capture_output=True,
                               text=True, timeout=timeout)
        except Exception as exc:
            print(f"notify: 发送给 {uid} 异常：{exc}", file=sys.stderr)
            ok = False
            continue
        if r.returncode != 0:
            print(f"notify: 发送给 {uid} 失败：{(r.stderr or r.stdout).strip()[:200]}",
                  file=sys.stderr)
            ok = False
    return ok


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="发飞书通知（测试用）")
    ap.add_argument("text", nargs="+", help="消息内容")
    ap.add_argument("--root", default="/srv/social-work")
    ap.add_argument("--as", dest="identity", default="bot", choices=["bot", "user"])
    a = ap.parse_args()
    sys.exit(0 if send(" ".join(a.text), a.root, as_identity=a.identity) else 1)
