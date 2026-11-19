#!/usr/bin/env python3
"""拷回仓库的旧流程脚本冒烟测试。

审计 #1 指出 archive.py / scan-inbox.py / reconcile.py 只在生产有、仓库没有，
全新部署会 500。已补回仓库，这里保证：
  1. 九个文件都能编译
  2. 依赖闭包完整（import 不报 ModuleNotFoundError）
  3. 关键 CLI 入口能打印 --help（说明 argparse 正常）
4. 不实际跑写盘逻辑（那需要飞书/PG/Cognee）
"""
from __future__ import annotations

import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# 旧流程脚本 + 它们依赖的本地模块
LEGACY = [
    "archive.py", "scan-inbox.py", "reconcile.py",
    "classify_rules.py", "image_policy.py", "manifest.py",
    "error_queue.py", "retry.py",
]


@pytest.mark.parametrize("name", LEGACY)
def test_legacy_script_exists(name):
    """文件必须在仓库里（审计 #1 的核心断言）。"""
    assert (SCRIPTS / name).is_file(), f"{name} 不在仓库，全新部署会失败"


@pytest.mark.parametrize("name", LEGACY)
def test_legacy_script_compiles(name):
    path = SCRIPTS / name
    if not path.is_file():
        pytest.skip(f"{name} 不存在")
    py_compile.compile(str(path), doraise=True)


@pytest.mark.parametrize("name", ["classify_rules", "image_policy", "manifest",
                                  "error_queue", "retry", "reconcile", "archive"])
def test_legacy_module_imports(name):
    """导入不能报 ModuleNotFoundError——依赖闭包要完整。"""
    code = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import {name}"
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{name} 导入失败：{r.stderr[-500:]}"


@pytest.mark.parametrize("name", ["archive.py", "reconcile.py"])
def test_legacy_cli_help_works(name):
    """argparse 入口能用（说明脚本结构没坏）。"""
    path = SCRIPTS / name
    if not path.is_file():
        pytest.skip(f"{name} 不存在")
    r = subprocess.run([sys.executable, str(path), "--help"],
                       capture_output=True, text=True, timeout=60,
                       cwd=str(SCRIPTS))
    assert r.returncode == 0, f"{name} --help 失败：{r.stderr[-500:]}"
    assert "usage" in r.stdout.lower()


def test_archive_documents_scan_inbox_dependency():
    """archive.py 依赖 scan-inbox.py；两个必须同时存在，否则归档链路断。"""
    assert (SCRIPTS / "archive.py").is_file()
    assert (SCRIPTS / "scan-inbox.py").is_file()
