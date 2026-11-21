#!/usr/bin/env python3
"""部署机验收：确认真实资产在场，且依赖它们的测试没有被跳过。

模板与业务数据不入库（用户定规），所以「干净检出」天然缺资产、相关测试会 skip。
本脚本的作用是把 skip 变成**显式验收项**：在部署机上跑，任何资产类测试
被跳过都算失败，避免「测试全绿」的假象。

用法：
    python3 scripts/verify_assets.py              # 检查资产 + 跑测试
    python3 scripts/verify_assets.py --assets-only # 只检查资产

退出码：0 全通过；1 有资产缺失或有测试被跳过。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("SOCIAL_WORK_ROOT", "/srv/social-work"))

# 测试代码只在开发仓里（deploy.sh 不同步 tests/），所以 REPO 优先取
# SOCIAL_WORK_TEST_REPO；没设就按「本文件在 <repo>/scripts/ 下」推断。
# 生产机上本文件在 /srv/social-work/scripts/，那里没有 tests/，
# 必须显式给 SOCIAL_WORK_TEST_REPO=/home/ubuntu/DSH/social-worker。
_env_repo = os.environ.get("SOCIAL_WORK_TEST_REPO", "").strip()
REPO = Path(_env_repo) if _env_repo else Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates" / "library"
PROJECTS = ROOT / "projects"

# 资产类测试：这些文件里的 skip 只允许在无资产环境下出现
ASSET_TESTS = [
    "tests/test_template_layout.py",    # 13 个真实模板的版式
    "tests/test_engine_fill.py",        # 真实模板的占位符扫描
    "tests/test_engine_markdown.py",    # 真实 docx/xlsx 转 md
    "tests/test_engine_plan_parse.py",  # 真实计划书解析
    "tests/test_engine_checks.py",      # 真实活动目录的 15 项核对
    "tests/test_readme.py",             # 真实活动目录的索引生成
    "tests/test_engine_p1.py",          # 两个项目资料目录
    "tests/test_engine_p2.py",          # 两个项目资料目录
    "tests/test_engine_ingest.py",      # 真实活动目录的编号读取
]


def check_assets() -> list[str]:
    """返回缺失项描述；空列表表示资产齐全。"""
    missing: list[str] = []
    if not TEMPLATES.is_dir():
        missing.append(f"模板目录不存在：{TEMPLATES}")
    else:
        n = len(list(TEMPLATES.glob("*.docx"))) + len(list(TEMPLATES.glob("*.xlsx")))
        if n < 10:
            missing.append(f"模板数量异常：{TEMPLATES} 下只有 {n} 个 docx/xlsx（预期 ≥10）")
    if not PROJECTS.is_dir():
        missing.append(f"项目目录不存在：{PROJECTS}")
    else:
        if not list(PROJECTS.glob("*/活动")):
            missing.append(f"项目下没有「活动」目录：{PROJECTS}")
    return missing


def run_tests() -> tuple[int, int, int, list[str]]:
    """跑测试，返回 (passed, failed, skipped, 资产类被跳过的用例名)。"""
    env = dict(os.environ)
    env["SOCIAL_WORK_TEMPLATES"] = str(TEMPLATES)
    env["SOCIAL_WORK_PROJECTS"] = str(PROJECTS)
    if not (REPO / "tests").is_dir():
        return 0, 0, 0, [f"找不到测试目录：{REPO}/tests（设 SOCIAL_WORK_TEST_REPO 指向开发仓）"]
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *ASSET_TESTS, "-q", "-rs",
         "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True, env=env, timeout=900)
    out = r.stdout or ""
    passed = failed = skipped = 0
    for n, kind in re.findall(r"(\d+) (passed|failed|skipped|error|errors)", out):
        v = int(n)
        if kind == "passed":
            passed = v
        elif kind in ("failed", "error", "errors"):
            failed += v
        elif kind == "skipped":
            skipped = v
    names = re.findall(r"SKIPPED \[(\d+)\] ([^:]+):\d+: (.+)", out)
    return passed, failed, skipped, [f"{f}（{n} 项）：{why}" for n, f, why in names]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="部署机资产与测试验收")
    ap.add_argument("--assets-only", action="store_true", help="只检查资产，不跑测试")
    args = ap.parse_args(argv)

    missing = check_assets()
    if missing:
        print("资产检查：")
        for m in missing:
            print(f"  ✗ {m}")
    else:
        n = len(list(TEMPLATES.glob("*.docx"))) + len(list(TEMPLATES.glob("*.xlsx")))
        print(f"资产检查：✓ 模板 {n} 个，项目 {len(list(PROJECTS.iterdir()))} 个")

    if args.assets_only:
        return 1 if missing else 0

    print("\n资产类测试：")
    passed, failed, skipped, names = run_tests()
    print(f"  {passed} passed, {failed} failed, {skipped} skipped")
    for n in names:
        print(f"  ⚠️ 跳过 {n}")

    if missing:
        print("\n结论：资产缺失，跳过不算通过")
        return 1
    if failed:
        print("\n结论：有测试失败")
        return 1
    if passed == 0:
        print("\n结论：一个测试都没跑起来，验收无效")
        return 1
    if skipped:
        print("\n结论：资产齐全但仍有测试被跳过，需查明原因")
        return 1
    print("\n结论：全部通过（无跳过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
