#!/usr/bin/env python3
"""飞书登录健康检查：真实调用一次 drive 接口，判断 token 是否还有效。

为什么不直接用 `lark-cli auth check --scope drive`：
它比的是**声明的 scope**，而实际权限可能来自更宽的授权。实测
2026-11-21 时 auth check 报缺 drive，但对同一 folder 的读/写/删全部成功。
所以这里改用真实 API 调用做判定 —— 能读到文件才算有权限。

用法：
    python3 scripts/check_feishu_auth.py           # 检查，失败退出 1
    python3 scripts/check_feishu_auth.py --json    # 机器可读

退出码：0 正常；1 失效（需要人工重新授权）；2 配置问题。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# 配置跟 scan-inbox.py 同源：服务根目录下的 feishu-flow.yaml
# （生产 /srv/social-work/feishu-flow.yaml），不是仓库 config/ 下。
ROOT = Path(os.environ.get("SOCIAL_WORK_ROOT", "/srv/social-work"))
CONFIG = ROOT / "feishu-flow.yaml"


def _lark(args: list[str], timeout: int = 90) -> dict:
    try:
        r = subprocess.run(["lark-cli", *args, "--json"],
                           capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return {"ok": False, "error": {"message": "lark-cli 调用失败"}}


def check() -> tuple[int, dict]:
    """返回 (退出码, 结果字典)。"""
    import yaml
    if not CONFIG.is_file():
        return 2, {"ok": False, "reason": f"配置不存在：{CONFIG}"}
    try:
        cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return 2, {"ok": False, "reason": f"配置解析失败：{exc}"}

    root = str((cfg.get("feishu") or {}).get("root") or "")
    if not root:
        return 2, {"ok": False, "reason": "配置缺 feishu.root"}

    # 真实调用：列根目录第一页。能列出来 = 身份有效 + 有 drive 读权限。
    d = _lark(["api", "GET", "/open-apis/drive/v1/files",
               "--params", json.dumps({"folder_token": root, "page_size": 1})])
    if d.get("ok"):
        files = (d.get("data") or {}).get("files") or []
        return 0, {"ok": True, "reason": "飞书身份有效",
                   "root": root, "sample": [f.get("name") for f in files][:1]}

    err = d.get("error") or {}
    msg = str(err.get("message") or err.get("subtype") or "")
    code = err.get("code") or (err.get("type") or "")
    # 认证类失败才算「需要重新授权」，其它（网络、限流）不算
    auth_hint = any(k in str(msg).lower() for k in
                    ("authoriz", "token", "permission", "identity"))
    return (1 if auth_hint else 2), {
        "ok": False, "reason": msg or "无法列出根目录（token 失效或 folder 不存在）",
        "code": code, "auth_hint": auth_hint,
        "need_login": bool(auth_hint),
        "hint": ("运行 lark-cli auth login --domain drive --no-wait --json，"
                 "把 verification_url 交给用户确认") if auth_hint else "",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="飞书登录健康检查")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    code, res = check()
    if args.json:
        print(json.dumps({"code": code, **res}, ensure_ascii=False))
    else:
        if res.get("ok"):
            print(f"  ✅ {res['reason']}（root={res.get('root')}）")
        else:
            print(f"  ❌ {res.get('reason')}")
            if res.get("need_login"):
                print(f"     需要重新授权：{res.get('hint')}")
    return code


if __name__ == "__main__":
    sys.exit(main())
