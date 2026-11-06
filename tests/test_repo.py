"""仓库结构 / 安全冒烟测试。

跑法：
    python3 -m pytest tests/ -q
或：
    python3 tests/test_repo.py

关注两件事：脚本能编译、示例配置能解析、真实密钥没有进仓。
"""
from __future__ import annotations

import os
import py_compile
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["config.py", "convert-md.py", "scan-inbox.py", "cognee.py", "classify_rules.py",
          "image_policy.py", "audit_log.py"]
# 纯函数模块 / 只接收外部传入 root 的模块，不自己解析路径，不需要 load_config()
PURE_MODULES = {"classify_rules.py", "image_policy.py", "audit_log.py"}

sys.path.insert(0, str(ROOT / "scripts"))
import config as sw_config  # noqa: E402

# 飞书 folder token 形态：27-28 位 base62
FEISHU_TOKEN = re.compile(r"\b[A-Za-z0-9]{27,28}\b")
# 常见密钥前缀
SECRET = re.compile(
    r"\b(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "tests"}

# 脚本里不该再出现写死的服务端根目录 / 容器名赋值（文档字符串里的说明文字不算）
HARDCODED = re.compile(
    r"(?m)^\s*(ROOT|HOST_ROOT|KEY_FILE|CONTAINER|COGNEE_CONTAINER)\s*=\s*[^\n]*"
    r"[\"'](?:/srv/social-work|social-work-cognee)[\"']"
)


@contextmanager
def _env(**overrides):
    """临时设置/清除环境变量。"""
    saved = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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


def test_config_yaml_complete():
    """config/config.yaml 存在且字段齐全。"""
    path = ROOT / "config" / "config.yaml"
    assert path.is_file(), "config/config.yaml 必须存在"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "config/config.yaml 必须是映射"
    for key in ("root", "container", "container_root", "key_file",
                "audit_retention_days", "datasets", "llm"):
        assert key in data, f"config/config.yaml 缺少 {key}"
    assert isinstance(data["root"], str)
    assert isinstance(data["container"], str)
    assert isinstance(data["container_root"], str)
    assert isinstance(data["key_file"], str)
    assert isinstance(data["audit_retention_days"], int)
    assert isinstance(data["datasets"], dict)
    assert isinstance(data["llm"], dict)


def test_config_defaults():
    """默认值与改造前完全一致，现有定时任务不受影响。"""
    with _env(SOCIAL_WORK_ROOT=None, SOCIAL_WORK_CONFIG=None):
        cfg = sw_config.load_config()
    assert cfg.root == Path("/srv/social-work")
    assert cfg.container == "social-work-cognee"
    assert cfg.container_root == "/host/social-work"
    assert cfg.key_file == Path("/srv/social-work/.cognee-key")
    assert cfg.audit_retention_days == 90
    assert cfg.datasets["shared"] == "shared-policies"
    assert cfg.datasets["huadu"] == "huadu"
    assert cfg.datasets["huimin"] == "huimin"


def test_config_root_env_override():
    """SOCIAL_WORK_ROOT 覆盖 root，key_file 跟着 root 走。"""
    with _env(SOCIAL_WORK_ROOT="/tmp/x", SOCIAL_WORK_CONFIG=None):
        cfg = sw_config.load_config()
    assert cfg.root == Path("/tmp/x")
    assert cfg.key_file == Path("/tmp/x/.cognee-key")


def test_scripts_use_config():
    """三个脚本不再写死根目录/容器名，统一走 load_config()。"""
    for name in SCRIPTS:
        if name in PURE_MODULES:
            continue
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert not HARDCODED.search(text), f"{name} 仍在硬编码服务端路径/容器名"
        assert "load_config(" in text, f"{name} 未使用 load_config()"


if __name__ == "__main__":
    for fn in (
        test_scripts_compile,
        test_configs_parse,
        test_skill_has_frontmatter,
        test_no_real_secrets_tracked,
        test_gitignore_covers_runtime_data,
        test_config_yaml_complete,
        test_config_defaults,
        test_config_root_env_override,
        test_scripts_use_config,
    ):
        fn()
        print(f"ok  {fn.__name__}")
    print("all passed")
