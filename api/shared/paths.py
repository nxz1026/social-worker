#!/usr/bin/env python3
"""路径工具：相对路径归一、越界防护、生成件覆盖保护与输出路径解析。

从 app.py 抽出，供上传 / 下载 / 模板填充等多个端点共用；
只依赖 api.errors 与 api.state，不反向 import app.py。
"""
from __future__ import annotations

import posixpath
import urllib.parse
from pathlib import Path

from fastapi import HTTPException

from ..errors import ApiError
from ..state import ROOT


def normalize_rel(rel: str, *, root: Path | None = None) -> str:
    """把台账里的路径归一成相对 ROOT 的 posix 形式；空值返回空串。

    root 默认取 state.ROOT；app.py 调用时显式传入自己的 ROOT，
    保持原有「测试 monkeypatch app.ROOT 即生效」的行为。
    """
    base = root or ROOT
    rel = str(rel or "").strip().replace("\\", "/")
    if not rel:
        return ""
    p = Path(rel)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            return ""
    # 不能用 lstrip("./")：那是字符集剥离，会把 .trash/ 这类点开头的目录
    # 剥成 trash/（回收站恢复因此找不到文件，审计 PR-06）。
    # 这里显式处理：normpath 归一后，丢掉所有指向父级的 ".." 段，
    # 效果与原 lstrip 一致（"../../etc/passwd" → "etc/passwd"），
    # 但不会误伤 .trash 这种正常目录名。
    normed = posixpath.normpath(rel)
    segments = [seg for seg in normed.split("/") if seg not in ("", ".", "..")]
    return "/".join(segments)


def safe_rel_path(base: Path, rel: str, *, allow_empty: bool = False) -> Path:
    """把请求里的相对路径限制在 base 目录内。

    先拒绝绝对路径与任何 `..` 段，再归一化并做 resolve 越界检查；
    这样 `../`、`a/../../b`、绝对路径都回 400，不会被 normpath 悄悄抹平。
    """
    # 先 URL 解码再校验：否则 `..%2f..%2fx.docx`、`%2Fabs.docx` 这类编码形式
    # 能绕过 `..` 与绝对路径检查（审计 P1-6 实测过）。
    # 解码两次覆盖 `%252e` 这种双重编码。
    raw_text = str(rel or "").strip()
    for _ in range(2):
        decoded = urllib.parse.unquote(raw_text)
        if decoded == raw_text:
            break
        raw_text = decoded
    raw = raw_text.replace("\\", "/")
    if not raw:
        if allow_empty:
            return base.resolve()
        raise HTTPException(400, "缺 path")
    if raw.startswith("/"):
        raise HTTPException(400, "路径必须是相对路径")
    if any(seg == ".." for seg in raw.split("/")):
        raise HTTPException(400, "路径不能包含 ..")
    # 文件名长度上限：Linux NAME_MAX 255，留余量给扩展名与 .trash 前缀
    if any(len(seg.encode("utf-8")) > 200 for seg in raw.split("/")):
        raise HTTPException(400, "路径段过长（单段上限 200 字节）")
    norm = normalize_rel(raw, root=base)
    if not norm:
        raise HTTPException(400, "路径非法")
    path = (base / norm).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(400, "路径越界")
    return path


def force_flag(value) -> bool:
    """解析 force 参数。非法值给 force 专属的报错文案，不复用 enabled 的。

    空串 / None 按 False 处理：表单没填这一项时不该报错。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "":
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ApiError(400, "VALIDATION", f"force 必须是布尔值，收到 {value!r}",
                   "传 true 或 false，或不传")


# 生成件被覆盖的保护：这些状态下目标文件视为「用户可能改过」，
# 不加 force=true 不允许覆盖（覆盖不可撤销，手改内容会直接消失）。
_OVERWRITE_GUARD_STATUS = {"generated", "imported", "signed", "edited", "reviewed"}


def overwrite_block_reason(act_dir: Path, dst: Path) -> str:
    """判断覆盖 dst 是否需要显式确认；返回原因，可覆盖时返回空串。

    规则：
      1. 目标不存在 → 可写
      2. activity.yaml 里 status 属于「可能被人工改过」的集合 → 需确认
      3. status 明确是纯生成（未标 status 也算纯生成）→ 允许直接覆盖
    """
    if not dst.exists():
        return ""
    # activity.yaml 读不出来（损坏/解析失败）时按「状态不明」处理，要求确认。
    # 否则把 yaml 写坏就等于关掉了保护 —— 审计发现的绕过路径。
    # 延迟 import：engine 依赖仓库根在 sys.path，且保持测试可 monkeypatch。
    from engine import ingest as _ingest
    raw_meta = _ingest.read_activity_yaml  # 便于测试 monkeypatch
    try:
        meta = raw_meta(act_dir)
    except ValueError:
        return ("activity.yaml 无法解析，无法判断是否有人工修改；"
                "确认覆盖请传 force=true（建议先修好 activity.yaml）")
    status = str(meta.get("status") or "").strip().lower()
    if status in _OVERWRITE_GUARD_STATUS - {"generated"}:
        return (f"目标文件已存在且活动状态为 {status}，"
                f"覆盖会丢失人工修改内容；确认覆盖请传 force=true")
    return ""


# 模板名 → 归档序号（与 engine.checks.CHECKLIST 的 prefix 对应）。
# 模板文件名和清单项名不完全一致（计划书↔活动计划、监督记录↔监督记录表），
# 所以用显式映射，不做模糊匹配（模糊匹配会把「项目管理目录」算错）。
# 不在「生成材料」里出现的模板：生成出来都是空壳，必须人工按实际票据填。
# 判定放在共享位置，前端过滤、/fill、/api/sw/run 三处共用同一份，
# 否则会出现「界面看不到但 API 能生成」（2026-11-24 审计）。
NO_GENERATE = {
    "决算表": "需按实际票据人工填写，系统只做校验不生成",
    "报销单": "需按实际票据人工填写，系统只做校验不生成",
}


TEMPLATE_SERIAL = {
    "计划书": "01",
    "活动通知": "02",
    "签到表": "03",
    "服务对象满意度调查表": "04",
    "满意度测评汇总表": "05",
    "总结报告": "06",
    "简报": "07",
    "决算表": "08",
    "监督记录": "09",
    "报销单": "21",          # 报销单在清单里是 09-/21- 双前缀，成品统一 21
    "发票明细表": "22",
    "劳务领取表": "23",
    "志愿者补贴领取表": "24",
    "物资领取表": "25",
    "支出证明单": "26",
    "对公付款申请单": "27",
    # 活动目录 / 项目管理目录 是归档索引，不带序号
}


def template_serial(tpl_name: str) -> str:
    """模板名 → 两位数序号；未登记的返回空串（不加前缀）。"""
    return TEMPLATE_SERIAL.get(str(tpl_name).strip(), "")


def fill_output(act_dir: Path, body: dict, tpl: Path,
                *, check_overwrite: bool = True) -> Path:
    """解析输出路径：默认「序号-模板名.扩展名」，限制在活动目录内。

    默认名按归档规范带序号（01-计划书.docx）；模板没登记序号时退回
    「模板名.扩展名」。调用方显式给了 output 就完全按它来。
    check_overwrite=True 时（默认），目标已存在且活动状态表明可能人工改过，
    且调用方没给 force=true，就抛 409 NEED_FORCE，避免静默覆盖。
    """
    serial = template_serial(tpl.stem)
    default = f"{serial}-{tpl.stem}{tpl.suffix}" if serial else f"{tpl.stem}{tpl.suffix}"
    out_rel = str(body.get("output") or default).strip()
    dst = safe_rel_path(act_dir, out_rel)
    if not dst.suffix:
        dst = dst.with_name(dst.name + tpl.suffix)
    if check_overwrite and not force_flag(body.get("force", False)):
        reason = overwrite_block_reason(act_dir, dst)
        if reason:
            raise ApiError(409, "NEED_FORCE", reason,
                           "确认无人工修改后重发请求并带上 force=true")
    return dst
