"""pytest 全局配置：给每个 pytest 进程一个独立的 basetemp。

背景：pytest.ini 里固定的 `--basetemp=.pytest_tmp` 在多进程并行时（并行
coder、CI 分片）会互相删建同一目录，表现为随机的 FileNotFoundError 或
"Directory not empty"。改成按 pid 隔离，互不干扰。
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def pytest_configure(config):
    """未显式指定 basetemp 时，按 pid 给一个独立目录。"""
    if config.option.basetemp:
        return                      # 命令行/环境已指定，尊重
    base = _REPO / ".pytest_tmp" / f"p{os.getpid()}"
    base.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(base)
