#!/usr/bin/env python3
"""活动归档入库：把解压出来的活动包复制进活动目录，并记录编号。

活动包的结构是平铺的（00-目录.docx、01-计划书.docx、子目录……），
复制时保持相对路径不变；目标已存在的文件不覆盖，只记 skipped。
"""
from __future__ import annotations

import html
import re
import shutil
import zipfile
from pathlib import Path

import yaml

# 包内编号，兼容旧数据的小写 c
CODE_RE = re.compile(r"CDYJR\d{4}\d{3}-[Cc]-\d{6,7}")

ACTIVITY_YAML = "activity.yaml"


def import_activity_tree(src_dir: Path, dst_dir: Path) -> dict:
    """把解压出来的活动包目录树复制进 dst_dir。

    - 递归复制所有文件与子目录
    - 已存在的目标文件跳过（不覆盖），记入 skipped
    - 返回 {"copied": [...相对路径], "skipped": [...], "errors": [...]}
    """
    src = Path(src_dir)
    dst = Path(dst_dir)
    result: dict[str, list[str]] = {"copied": [], "skipped": [], "errors": []}

    if not src.is_dir():
        result["errors"].append(f"源目录不存在：{src}")
        return result

    # 目标活动目录不存在时先建（花都的爱邻议事会目录还没建）
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result["errors"].append(f"无法创建目标目录：{dst}：{exc}")
        return result

    # sorted 保证多次运行的 copied/skipped 顺序稳定
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src).as_posix()
        target = dst / rel
        if item.is_dir():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                result["errors"].append(f"{rel}：{exc}")
            continue
        if target.exists():
            result["skipped"].append(rel)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            result["copied"].append(rel)
        except OSError as exc:
            result["errors"].append(f"{rel}：{exc}")
    return result


def _docx_text(path: Path) -> str:
    """抽取 docx 正文纯文本。

    直接读 word/document.xml 里的 <w:t> 节点并拼接：不依赖 python-docx，
    且编号被 Word 拆到多个 run 里也能还原成连续字符串。
    """
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", "ignore")
    parts = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S)
    return html.unescape("".join(parts))


def _code_in_docx(path: Path) -> str | None:
    """从单个 docx 里取第一个编号；文件损坏当作没找到。"""
    try:
        text = _docx_text(path)
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    match = CODE_RE.search(text)
    return match.group(0) if match else None


def read_activity_code(root: Path) -> str | None:
    """从活动目录里找编号：

    1. 找 00-*.docx（目录文件），提取 CDYJRxxx-... 编号
    2. 失败再找 01-*.docx（计划书）
    3. 都失败返回 None
    """
    act_dir = Path(root)
    if not act_dir.is_dir():
        return None
    for prefix in ("00-", "01-"):
        for docx in sorted(act_dir.glob(f"{prefix}*.docx")):
            code = _code_in_docx(docx)
            if code:
                return code
    return None


def read_activity_yaml(act_dir: Path) -> dict:
    """读 activity.yaml，不存在返回 {}。"""
    path = Path(act_dir) / ACTIVITY_YAML
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"yaml 解析失败：{path}：{exc}")
    return data if isinstance(data, dict) else {}


def write_activity_yaml(act_dir: Path, *, code: str | None, source: str,
                        imported_at: str) -> Path:
    """在 act_dir 下写 activity.yaml：

    code: "CDYJR..."
    source: "/tmp/act4/xxx"        # 来源路径，追溯用
    imported_at: "2026-11-18T16:30:00+08:00"
    status: imported               # imported | generated | archived

    已存在则合并（保留原有字段，只更新这三项）。原子写。
    """
    act = Path(act_dir)
    act.mkdir(parents=True, exist_ok=True)
    path = act / ACTIVITY_YAML

    # 合并：原有字段原样保留，本次四项覆盖
    data = read_activity_yaml(act)
    data["code"] = code
    data["source"] = source
    data["imported_at"] = imported_at
    data["status"] = "imported"

    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path
