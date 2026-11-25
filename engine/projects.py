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


# ---------- 项目简称（short）校验 ----------
# short 是**目录名 / Cognee 数据集名 / API 路径参数**三合一，比项目名更严格：
# 路径分隔符、控制字符、Windows 保留名都不能出现，长度也要收着（数据集名有长度限制）。
SHORT_MAX_LEN = 40
# 前端同步了一份（web/util.js 的 SHORT_MAX_LEN）；改这里要一起改
_SHORT_BAD_CHARS = set('/\\:*?"<>|')
# Windows 保留设备名：sht 拼出的目录在同步到 Windows 时会被拒
_SHORT_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}


def validate_short(short: str) -> str:
    """校验项目简称，合法就返回清洗后的值，非法抛 ValueError（调用方转 400）。

    规则（硬性，不做「帮你修好」的兜底 —— 自动改名字会让目录名与用户预期不符）：
      - 非空
      - 不含路径分隔符 / 通配符 / 控制字符
      - 不是 `.` / `..`，不以 `.` 开头（隐藏目录）
      - 长度 ≤ SHORT_MAX_LEN（40）
      - 不是 Windows 保留设备名
      - 中文 / 字母 / 数字 / 中文标点（·）（－）都可以，与现有项目名同风格
    """
    raw = str(short if short is not None else "").strip()
    if not raw:
        raise ValueError("项目简称（short）不能为空")
    if raw in (".", ".."):
        raise ValueError(f"项目简称非法：{raw}")
    if raw.startswith("."):
        raise ValueError("项目简称不能以「.」开头（会成为隐藏目录）")
    if Path(raw).is_absolute():
        raise ValueError(f"项目简称不能是绝对路径：{raw}")
    bad = sorted({ch for ch in raw if ch in _SHORT_BAD_CHARS})
    if bad:
        raise ValueError(f"项目简称不能包含字符：{''.join(bad)}")
    ctrl = [ch for ch in raw if ord(ch) < 32 or ord(ch) == 127]
    if ctrl:
        raise ValueError("项目简称含控制字符")
    if len(raw) > SHORT_MAX_LEN:
        raise ValueError(f"项目简称最多 {SHORT_MAX_LEN} 个字符，当前 {len(raw)} 个")
    if raw.lower() in _SHORT_RESERVED:
        raise ValueError(f"项目简称「{raw}」是系统保留名，请换一个")
    return raw


def find_short_conflict(short: str, *, exclude: str = "") -> dict | None:
    """short 是否已被别的项目占用；占用返回冲突项目信息，否则 None。

    判定口径：与任一现有项目的 **short** 相同，或与任一现有项目的 **目录名** 相同。
    后者是必须的 —— 目录名就是 short 的一半语义（历史项目 short 与目录名一致），
    新项目若取一个已存在的目录名，mkdir 会直接失败或写到别人目录里去。

    exclude：正在改的既有项目目录名，比对时跳过（改名场景）。
    """
    raw = str(short or "").strip()
    if not raw:
        return None
    for item in list_projects():
        name = str(item.get("name") or "")
        if name and name == exclude:
            continue
        data = item.get("data") or {}
        proj = data.get("project") if isinstance(data.get("project"), dict) else {}
        existing_short = str(proj.get("short") or "").strip()
        if raw == existing_short or raw == name:
            return {"name": name, "short": existing_short or name}
    return None


# 建项目时要建齐的五个资料子目录（与现有三个项目一致）
PROJECT_SUBDIRS = ("方案", "协议", "报告", "活动", "其他")


def create_project_skeleton(name: str, short: str, *,
                            subdirs: tuple[str, ...] = PROJECT_SUBDIRS,
                            yaml_data: dict | None = None) -> tuple[Path, Path]:
    """建项目目录骨架 + 项目 yaml，返回 (项目目录, yaml 路径)。

    **所有建项目的入口都走这里**（POST /api/projects 与 /api/projects/from-docx），
    否则两处的目录清单 / yaml 文件名约定迟早会分叉。

    约定（不要改）：
      - yaml 文件名是 `<项目目录名>.yaml`，与目录名一致 ——
        _yaml_in() 优先找 `<目录名>.yaml`，换了名字 list_projects 仍能兜底扫到，
        但 project_meta / save_project_code 等按名取文件的路径会不一致。
      - short 校验与唯一性由调用方做（这里只按入参落盘），
        因为两个入口对「重名」的处置不同：老入口 409、新入口也要 409，但提示文案不同。

    yaml_data=None 时写最小 yaml（只有 project.short / code_rules）。
    传了 yaml_data 就整体落盘（from-docx 用），并强制覆盖 project.short。
    """
    target = _project_dir(name)
    if target.exists():
        raise FileExistsError(f"项目目录已存在：{name}")
    target.mkdir(parents=True)
    for sub in subdirs:
        (target / sub).mkdir()

    yaml_path = target / f"{name}.yaml"
    if yaml_data is None:
        text = yaml.safe_dump({"project": {"short": short, "code_rules": {}}},
                              allow_unicode=True, sort_keys=False)
    else:
        data = dict(yaml_data)
        data.pop("_parsed", None)
        proj = dict(data.get("project") or {})
        proj["short"] = short
        data["project"] = proj
        text = dump_project_yaml(data)
    _ylock.atomic_write_text(yaml_path, text)
    return target, yaml_path


def dump_project_yaml(data: dict) -> str:
    """项目 yaml 序列化：固定段落顺序 + 顶部来源注释，便于人工看。

    现有三个项目 yaml 的字段顺序是 project / parties / staff / budget /
    indicators / personnel / tax / management_fee；这里按同一顺序输出，
    不写任何没解析到的假值（dict 里没有的键就不会出现在文件中）。
    """
    ordered: dict = {}
    for key in ("project", "parties", "staff", "budget",
                "indicators", "personnel", "tax", "management_fee"):
        if key in data:
            ordered[key] = data[key]
    # 未知字段原样保留（调用方手工塞的扩展字段不能被丢掉）
    for key, value in data.items():
        if key not in ordered and not str(key).startswith("_"):
            ordered[key] = value
    header = ("# 由 POST /api/projects/from-docx 依据项目申报书自动生成。\n"
              "# 解析不到的字段留空，具体缺口见创建接口返回的 warnings；\n"
              "# 手工补全后本文件即可作为项目的唯一主数据来源。\n")
    return header + yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False,
                                   default_flow_style=False)


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
