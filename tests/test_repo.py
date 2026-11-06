"""仓库结构 / 安全冒烟测试。

跑法：
    python3 -m pytest tests/ -q
或：
    python3 tests/test_repo.py

关注两件事：脚本能编译、示例配置能解析、真实密钥没有进仓。
"""
from __future__ import annotations

import py_compile
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["convert-md.py", "scan-inbox.py", "cognee.py"]

# 飞书 folder token 形态：27-28 位 base62
FEISHU_TOKEN = re.compile(r"\b[A-Za-z0-9]{27,28}\b")
# 常见密钥前缀
SECRET = re.compile(
    r"\b(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "tests"}


def test_scripts_compile():
    for name in SCRIPTS:
        py_compile.compile(str(ROOT / "scripts" / name), doraise=True)


def test_configs_parse():
    targets = [
        "config/feishu-flow.yaml.example",
        "config/entities.yaml",
        "skill/enabled-skills.yaml",
    ]
    for rel in targets:
        # enabled-skills.yaml 全是注释，解析成 None 是正常的；只要不抛异常即可
        assert yaml.safe_load((ROOT / rel).read_text(encoding="utf-8")) is not None or rel.endswith(
            "enabled-skills.yaml"
        ), rel


def test_skill_has_frontmatter():
    head = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
    assert head.startswith("---\n"), "SKILL.md 必须有 YAML frontmatter 才能被 DSH 注册"


def test_no_real_secrets_tracked():
    """真实密钥不得出现在任何将进仓的文件里。"""
    text_suffixes = {".md", ".yaml", ".yml", ".py", ".sh", ".example", ".txt", ""}
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or any(p in SKIP_DIRS for p in path.parts):
            continue
        if path.suffix not in text_suffixes:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not SECRET.search(text), f"{path.relative_to(ROOT)} 含疑似密钥"
        if path.name.endswith(".example"):
            assert not FEISHU_TOKEN.search(text), f"{path.relative_to(ROOT)} 含疑似真实 folder token"


def test_gitignore_covers_runtime_data():
    """运行时数据目录必须在 .gitignore 里。"""
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in ("vault-*/", "sources/", "state/", "inbox/", ".env"):
        assert entry in gi, f".gitignore 缺少 {entry}"


if __name__ == "__main__":
    for fn in (
        test_scripts_compile,
        test_configs_parse,
        test_skill_has_frontmatter,
        test_no_real_secrets_tracked,
        test_gitignore_covers_runtime_data,
    ):
        fn()
        print(f"ok  {fn.__name__}")
    print("all passed")
