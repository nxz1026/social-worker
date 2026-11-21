#!/usr/bin/env python3
"""打印当前端点数与测试数，供人工核对，不作为 CI 门禁。

用法：
    python3 scripts/stats.py              # 端点数（读本地服务的 openapi.json）
    python3 scripts/stats.py --tests      # 顺带跑一次 pytest 收集

设计意图：README 里不再写死数字（审计 C-11 指出 53/456 已过时），
改由本脚本现取，避免文档与实际漂移。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_URL = "http://127.0.0.1:8001/openapi.json"


def count_paths(url: str) -> int | None:
    """从 openapi.json 数路径条数；服务没起返回 None。"""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.load(r)
        return len(data.get("paths") or {})
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def count_tests() -> tuple[int, int, int] | None:
    """跑 pytest 收集，返回 (passed, failed, skipped)；失败返回 None。"""
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q",
                        "--no-header", "-p", "no:cacheprovider"],
                       cwd=str(REPO), capture_output=True, text=True, timeout=600)
    tail = (r.stdout or "").strip().splitlines()
    line = next((x for x in reversed(tail) if " passed" in x or "failed" in x), "")
    passed = failed = skipped = 0
    import re
    for n, kind in re.findall(r"(\d+) (passed|failed|skipped|error|errors)", line):
        if kind == "passed":
            passed = int(n)
        elif kind in ("failed", "error", "errors"):
            failed += int(n)
        elif kind == "skipped":
            skipped = int(n)
    return (passed, failed, skipped) if line else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打印端点数与测试数")
    ap.add_argument("--url", default=DEFAULT_URL, help="openapi.json 地址")
    ap.add_argument("--tests", action="store_true", help="顺带跑 pytest 收集")
    args = ap.parse_args(argv)

    paths = count_paths(args.url)
    if paths is None:
        print(f"端点：服务 {args.url} 未响应（先启动 social-work-api 或本地 uvicorn）")
    else:
        print(f"端点：{paths} 个路径")

    if args.tests:
        res = count_tests()
        if res is None:
            print("测试：pytest 未产出汇总行")
        else:
            passed, failed, skipped = res
            print(f"测试：{passed} passed, {failed} failed, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
