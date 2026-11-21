#!/usr/bin/env python3
"""项目主数据读取。

数据落在 /srv/social-work/projects/<目录名>/<目录名>.yaml，一个项目一个目录。
本模块只做读 + 编号写回，不建目录、不碰活动目录。
"""
from __future__ import annotations

from . import ylock as _ylock

import json
import os
from pathlib import Path

import yaml

from .activities import _safe_dirname

# 部署根目录；测试 / 本地可用 SOCIAL_WORK_PROJECTS 指向别处
PROJECTS_ROOT = Path(os.environ.get("SOCIAL_WORK_PROJECTS") or "/srv/social-work/projects")


def _safe_name(name: str) -> str:
    """项目名校验：空串、`.`、`..`、含斜杠、绝对路径一律拒绝。"""
    raw = str(name if name is not None else "").strip()
    if not raw:
        raise ValueError("项目名不能为空")
    if raw in (".", "..") or ".." in raw:
        raise ValueError(f"项目名非法：{raw}")
    if "/" in raw or "\\" in raw:
        raise ValueError(f"项目名不能包含路径分隔符：{raw}")
    if Path(raw).is_absolute():
        raise ValueError(f"项目名不能是绝对路径：{raw}")
    return raw


def _project_dir(name: str) -> Path:
    """校验后的项目目录；顺带做一次 resolve 越界检查。"""
    raw = _safe_name(name)
    root = Path(PROJECTS_ROOT)
    path = root / raw
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"项目路径越界：{raw}")
    return path


def _yaml_in(project_dir: Path) -> Path | None:
    """目录里的项目 yaml：优先 <目录名>.yaml，否则取排序后第一个。"""
    if not project_dir.is_dir():
        return None
    candidates = sorted(project_dir.glob("*.yaml"))
    if not candidates:
        return None
    same = project_dir / f"{project_dir.name}.yaml"
    return same if same.is_file() else candidates[0]


def _read_yaml(path: Path) -> dict:
    """读 yaml；解析失败抛 ValueError（调用方决定怎么报错）。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"yaml 解析失败：{path.name}：{exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"yaml 根节点不是映射：{path.name}")
    return data


def list_projects() -> list[dict]:
    """扫 projects/*/*.yaml。

    返回 [{name(目录名), yaml_path, data, has_config}]；
    目录存在但没有 yaml 的也返回，data=None、has_config=False。
    """
    root = Path(PROJECTS_ROOT)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        yaml_path = _yaml_in(project_dir)
        out.append({
            "name": project_dir.name,
            "yaml_path": str(yaml_path) if yaml_path else None,
            "data": _read_yaml(yaml_path) if yaml_path else None,
            "has_config": yaml_path is not None,
        })
    return out


def load_project(name: str) -> dict:
    """读单个项目 yaml。目录 / yaml 不存在抛 FileNotFoundError，解析失败抛 ValueError。"""
    project_dir = _project_dir(name)
    if not project_dir.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{name}")
    yaml_path = _yaml_in(project_dir)
    if yaml_path is None:
        raise FileNotFoundError(f"项目没有 yaml：{name}")
    return _read_yaml(yaml_path)


def _as_int(value) -> int | None:
    """yaml 里的数字可能是字符串；转不了就给 None。"""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def project_meta(name: str) -> dict:
    """项目元数据：{name, short, code, code_rules, establish_month, act_types, has_config}。

    code_rules 取自 yaml 的 project.code_rules；establish_month / act_types
    是它的两个常用字段，单独摊平方便调用方直接用。
    目录不存在抛 FileNotFoundError；目录在但没 yaml 时 has_config=False。
    """
    project_dir = _project_dir(name)
    if not project_dir.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{name}")
    yaml_path = _yaml_in(project_dir)
    if yaml_path is None:
        return {
            "name": name,
            "short": name,
            "code": "",
            "code_rules": {},
            "establish_month": None,
            "act_types": {},
            "has_config": False,
        }

    data = _read_yaml(yaml_path)
    proj = data.get("project") if isinstance(data.get("project"), dict) else {}
    rules = proj.get("code_rules")
    if not isinstance(rules, dict):
        rules = {}
    act_types = rules.get("act_types")
    if not isinstance(act_types, dict):
        act_types = {}
    return {
        "name": name,
        "short": str(proj.get("short") or name),
        "code": str(proj.get("code") or ""),
        "code_rules": rules,
        "establish_month": _as_int(rules.get("establish_month")),
        # 类型号统一成 int，yaml 里手写成字符串也能用
        "act_types": {str(k): _as_int(v) for k, v in act_types.items()},
        "has_config": True,
    }


def activity_dir(project_name: str, dirname: str) -> Path:
    """返回 <PROJECTS_ROOT>/<project_name>/活动/<dirname>，做路径安全校验。

    只拼路径、不建目录；项目名和活动目录名都不允许越界。
    """
    raw_project = _safe_name(project_name)
    # 复用活动模块的目录名校验（空串 / .. / 路径分隔符一律拒绝）
    raw_dir = _safe_dirname(dirname)
    root = Path(PROJECTS_ROOT)
    path = root / raw_project / "活动" / raw_dir
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"活动路径越界：{raw_project}/活动/{raw_dir}")
    return path


def save_project_code(name: str, code: str) -> None:
    """只改 project.code 并原子写回（写 .tmp 再 replace），其余行原样保留。"""
    project_dir = _project_dir(name)
    yaml_path = _yaml_in(project_dir)
    if yaml_path is None:
        raise FileNotFoundError(f"项目没有 yaml：{name}")

    lines = yaml_path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_project = False
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            if stripped == "project:" or stripped.startswith("project:"):
                in_project = True
                continue
            if in_project:
                break                      # project 段结束
        if in_project and stripped.startswith("code:"):
            # 用 JSON 字符串字面量写值：空串得到 ""，与文件里现有写法一致
            lines[i] = " " * indent + "code: " + json.dumps(code, ensure_ascii=False) + "\n"
            replaced = True
            break
    if not replaced:
        raise ValueError(f"yaml 里没有 project.code：{name}")

    _ylock.atomic_write_text(yaml_path, "".join(lines))
