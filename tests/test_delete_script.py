"""scripts/delete.py 的删除契约测试。

覆盖：
  1. trash：md / 原件移到 .trash，台账 status=deleted
  2. purge：md / 原件真删，台账 status=deleted
  3. 缺 --yes：拒绝执行（退出码 2），文件与台账都不动

测试用临时 root（SOCIAL_WORK_ROOT 覆盖），不碰 /srv/social-work。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TOKEN = "local-delete-test"
MD_REL = "vault-huimin/md/项目计划/计划书/计划书.md"
ORIG_REL = "sources/projects/huimin/项目计划/计划书.docx"


def _make_root(tmp_path: Path) -> Path:
    """造一个最小台账 + md + 原件，返回 root。"""
    root = tmp_path / "root"
    (root / "state").mkdir(parents=True)
    md = root / MD_REL
    md.parent.mkdir(parents=True)
    md.write_text("# 计划书\n", encoding="utf-8")
    orig = root / ORIG_REL
    orig.parent.mkdir(parents=True)
    orig.write_bytes(b"docx-bytes")
    manifest = {
        "files": {
            TOKEN: {
                "name": "计划书.docx",
                "project": "huimin",
                "category": "项目计划",
                "original": ORIG_REL,
                "original_name": "计划书.docx",
                "md": MD_REL,
                "status": "done",
            }
        }
    }
    (root / "state" / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["SOCIAL_WORK_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "delete.py"), *args],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120,
    )


def _entry(root: Path) -> dict:
    data = yaml.safe_load((root / "state" / "manifest.yaml").read_text(encoding="utf-8"))
    return data["files"][TOKEN]


def test_trash_moves_md_and_source(tmp_path):
    root = _make_root(tmp_path)

    r = _run(root, TOKEN, "--mode", "trash", "--yes", "--no-notify")

    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / MD_REL).exists()
    assert not (root / ORIG_REL).exists()
    # md 进 vault 的 .trash，原件进 sources/.trash，且保留相对路径
    assert list((root / "vault-huimin" / ".trash").rglob("计划书.md"))
    assert list((root / "sources" / ".trash").rglob("计划书.docx"))

    entry = _entry(root)
    assert entry["status"] == "deleted"
    assert entry["delete_mode"] == "trash"
    assert entry["deleted_at"]


def test_purge_deletes_files(tmp_path):
    root = _make_root(tmp_path)

    r = _run(root, TOKEN, "--mode", "purge", "--yes", "--no-notify")

    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / MD_REL).exists()
    assert not (root / ORIG_REL).exists()
    # 真删不产生 .trash 副本
    assert not list(root.rglob("计划书.md"))
    assert not list(root.rglob("计划书.docx"))

    entry = _entry(root)
    assert entry["status"] == "deleted"
    assert entry["delete_mode"] == "purge"


def test_without_yes_is_rejected(tmp_path):
    root = _make_root(tmp_path)

    r = _run(root, TOKEN, "--mode", "purge", "--no-notify")

    assert r.returncode == 2, r.stdout + r.stderr
    assert "拒绝执行" in r.stdout
    # 文件与台账都保持原样
    assert (root / MD_REL).is_file()
    assert (root / ORIG_REL).is_file()
    assert _entry(root)["status"] == "done"


def test_unknown_identifier_returns_nonzero(tmp_path):
    root = _make_root(tmp_path)

    r = _run(root, "not-a-real-token", "--mode", "trash", "--yes", "--no-notify")

    assert r.returncode == 1
    assert (root / MD_REL).is_file()
