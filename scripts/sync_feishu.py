#!/usr/bin/env python3
"""把本地 projects/<项目>/ 镜像到飞书云文档。

飞书结构：
    已分类/益嘉仁/<项目名>/...

用法：
    python3 sync_feishu.py --dry-run              全部项目
    python3 sync_feishu.py 花都社区激励资金项目    单个项目

依赖 lark-cli（已登录 user 身份）。

本文件同时被 API 复用：ensure() 建远端目录，push() 镜像文件，
sync() 串起两者；main() 只负责解析命令行参数，行为与旧版一致。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.projects import PROJECTS_ROOT  # noqa: E402

# 部署根目录（可用 SOCIAL_WORK_PROJECTS 覆盖，与 engine.projects 保持一致）
ROOT = Path(PROJECTS_ROOT)
# 飞书「已分类/益嘉仁」
FEISHU_YIJIAREN = "CRXgfg1ljlJiUadvmPMcJA0CnLg"


def lark(*args, timeout=3600) -> dict:
    """调 lark-cli；返回解析后的 JSON，失败时返回 {"ok": False, "raw": ...}。"""
    r = subprocess.run(["lark-cli", "drive", *args, "--json"],
                       capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"ok": False, "raw": (r.stdout + r.stderr)[:400]}


def child_folders(parent: str) -> dict[str, str]:
    """列出飞书目录下的子文件夹：{名字: token}。"""
    d = lark("files", "list", "--folder-token", parent, timeout=120)
    return {f["name"]: f["token"]
            for f in d.get("data", {}).get("files", []) if f.get("type") == "folder"}


def _project_dirs(project: str | list[str] | None = "") -> list[Path]:
    """待同步的项目目录。project 为空 = 全部；支持单个名字或名字列表。"""
    if isinstance(project, str):
        wanted = [project] if project.strip() else []
    else:
        wanted = [str(p).strip() for p in (project or []) if str(p).strip()]
    dirs = [p for p in sorted(ROOT.iterdir())
            if p.is_dir() and not p.name.startswith(".")]
    if wanted:
        dirs = [p for p in dirs if p.name in wanted]
    return dirs


def ensure(project: str, dry_run: bool = False) -> dict:
    """确保飞书「益嘉仁/<项目名>」目录存在。

    返回 {"name", "created_dir", "folder_token"}；dry_run 时不写飞书，
    folder_token 为 None。
    """
    name = str(project).strip()
    folders = child_folders(FEISHU_YIJIAREN)
    if name in folders:
        return {"name": name, "created_dir": False,
                "folder_token": folders[name]}
    if dry_run:
        print(f"    [dry-run] 将创建目录 {name}")
        return {"name": name, "created_dir": False, "folder_token": None}
    d = lark("+create-folder", "--folder-token", FEISHU_YIJIAREN,
             "--name", name, timeout=120)
    tok = d.get("data", {}).get("folder_token")
    print(f"    创建目录 {name} → {tok}")
    return {"name": name, "created_dir": bool(tok), "folder_token": tok}


def push(project: str, folder_token: str | None = None,
         dry_run: bool = False) -> dict:
    """把本地项目目录镜像到飞书（文件级，增量）。

    folder_token 为空时先调 ensure() 取远端目录。
    返回 {"name", "pushed", "uploaded", "skipped"}。
    """
    name = str(project).strip()
    if folder_token is None:
        folder_token = ensure(name, dry_run=dry_run)["folder_token"]
    if not folder_token:
        return {"name": name, "pushed": False, "uploaded": None, "skipped": None}

    proj_dir = ROOT / name
    if dry_run:
        print(f"    [dry-run] 将镜像 {proj_dir} → {folder_token}")
        return {"name": name, "pushed": False, "uploaded": None, "skipped": None}

    d = lark("+push", "--local-dir", str(proj_dir), "--folder-token", folder_token,
             "--if-exists", "smart", timeout=7200)
    if "ok" in d and not d["ok"]:
        print(f"    镜像失败：{str(d)[:300]}")
        return {"name": name, "pushed": False, "uploaded": None, "skipped": None}
    data = d.get("data") or {}
    # lark-cli 不同版本字段名不一，逐个兜底
    up = (data.get("uploaded") or data.get("uploaded_files")
          or data.get("upload") or data.get("results") or "见上方输出")
    skip = (data.get("skipped") or data.get("skipped_files")
            or data.get("skip") or "")
    print(f"    镜像完成：上传 {up} {('跳过 ' + str(skip)) if skip else ''}")
    return {"name": name, "pushed": True, "uploaded": up, "skipped": skip}


def sync(project: str | list[str] | None = "", dry_run: bool = False) -> list[dict]:
    """同步一个 / 多个 / 全部项目，返回逐项目结果。

    结果项：{"name", "created_dir", "pushed"}。
    """
    targets = _project_dirs(project)
    if not targets:
        return []
    if not child_folders(FEISHU_YIJIAREN) and not dry_run:
        raise RuntimeError("飞书「益嘉仁」目录不存在或不可读")

    out: list[dict] = []
    for proj_dir in targets:
        print(f"\n### {proj_dir.name}")
        info = ensure(proj_dir.name, dry_run=dry_run)
        pushed = push(proj_dir.name, info.get("folder_token"), dry_run=dry_run)
        out.append({"name": proj_dir.name,
                    "created_dir": bool(info.get("created_dir")),
                    "pushed": bool(pushed.get("pushed"))})
    return out


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    targets = [a for a in sys.argv[1:] if not a.startswith("--")]
    try:
        sync(targets or "", dry_run=dry_run)
    except RuntimeError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
