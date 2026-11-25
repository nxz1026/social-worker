#!/usr/bin/env python3
"""按 indicators 拆活动清单。

活动目录命名规则：<两位序号>-<指标名>-第<中文序数>期。
序号全局连续，不按 indicator 的 code 走（code 可能写成 "02-04" 这种区间）。
"""
from __future__ import annotations

from typing import Callable

import re
from pathlib import Path

import yaml

CN_NUMS = "一二三四五六七八九十"
_CN_DIGITS = "零一二三四五六七八九"


def ordinal(n: int) -> str:
    """1→一 … 10→十 … 12→十二 … 20→二十；超出 1..99 直接 str(n)。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 1 or n > 99:
        return str(n)
    if n <= 10:
        return CN_NUMS[n - 1]
    tens, ones = divmod(n, 10)
    head = "" if tens == 1 else _CN_DIGITS[tens]
    return head + "十" + (_CN_DIGITS[ones] if ones else "")


def plan_activities(data: dict) -> list[dict]:
    """按 indicators 拆活动。

    返回 [{seq, dirname, indicator_name, target, budget, activity_budget}]；
    count 缺省 1，count=N 时拆成 N 期，预算按 N 均摊。indicators 为空返回 []。
    """
    indicators = (data or {}).get("indicators") or []
    out: list[dict] = []
    seq = 0
    for ind in indicators:
        if not isinstance(ind, dict):
            continue
        name = str(ind.get("name") or "").strip()
        # 期数只认显式 count 字段。
        # 试过从 target 文本推（「6次」→6期），但 target 是**服务量指标**不是期数：
        # 花都激励「楼栋网格矛盾排查 target: ≥3次」实际只建了 1 个活动目录，
        # 按 target 拆会凭空多出 2 个（2026-11-25 实测 11 → 13）。
        # 需要多期就在指标里显式写 count。
        # 需要「本项目某个指标实际分几期」时，看磁盘上已存在的目录数，不看 target。
        try:
            count = int(ind.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            count = 1
        budget = ind.get("budget")
        if budget is None or budget == "":
            budget = 0
        elif not isinstance(budget, (int, float)):
            try:
                budget = float(budget)
            except (TypeError, ValueError):
                budget = 0
        # periods 可选：把每期的小区名登记下来，目录名就用小区名而不是「第N期」。
        # 例：安全隐患专项排查按小区分 6 期，目录名是「12-安全隐患专项排查-七彩花都B1」。
        declared = ind.get("periods")
        by_period: dict[int, dict] = {}
        if isinstance(declared, list):
            for item in declared:
                if not isinstance(item, dict):
                    continue
                try:
                    no = int(item.get("period"))
                except (TypeError, ValueError):
                    continue
                by_period[no] = item

        for period in range(1, count + 1):
            seq += 1
            spec = by_period.get(period) or {}
            community = str(spec.get("community") or "").strip()
            # 有小区名用小区名，没有就用「第N期」
            tail = community or f"第{ordinal(period)}期"
            out.append({
                "seq": seq,
                "dirname": f"{seq:02d}-{name}-{tail}",
                "indicator_name": name,
                "target": ind.get("target"),
                "period": period,
                "community": community,
                "placeholder": bool(spec.get("placeholder")),
                "budget": budget,
                # count=1 时就是 budget 本身，不引入浮点尾数
                "activity_budget": budget if count == 1 else round(budget / count, 2),
            })
    return out


def list_existing_activities(project_dir: Path) -> list[str]:
    """读 <project_dir>/活动/ 下的子目录名，排序返回；目录不存在返回 []。"""
    act_dir = Path(project_dir) / "活动"
    if not act_dir.is_dir():
        return []
    return sorted(p.name for p in act_dir.iterdir() if p.is_dir())


_SEQ_PREFIX_RE = re.compile(r"^\d{2,3}-")


def activity_key(dirname: str) -> str:
    """活动目录的比对键：去掉序号前缀后的名字。

    目录序号是历史遗留，同一个活动在不同时期可能叫：
      12-安全隐患专项排查-七彩花都B1   （用户手工建的）
      13-安全隐患专项排查-七彩花都B1   （按指标顺序算出来的）
    序号不同但活动是同一个。比对「是否已存在」必须忽略序号，
    否则 split 会把已有活动当成新的再建一遍（2026-11-25 实测：
    dry_run 建议新建 10 个，其中 7 个已存在）。
    """
    return _SEQ_PREFIX_RE.sub("", str(dirname or "").strip())


def match_existing(planned_names: list[str],
                   existing: list[str]) -> tuple[list[str], list[str]]:
    """把计划目录名与已有目录名配对，返回 (待建, 已存在)。

    配对规则（方案 A）：**按去掉序号后的活动名精确匹配**。
    不用模糊包含 —— 「爱邻议事会-第一期」与「爱邻议事会-第十期」
    会互相误配。
    """
    existing_keys = {activity_key(n) for n in existing}
    to_create, to_skip = [], []
    for name in planned_names:
        if activity_key(name) in existing_keys:
            to_skip.append(name)
        else:
            to_create.append(name)
    return to_create, to_skip


def no_extras(code: str) -> str:
    """项目编号校验占位：返回去空白后的 code；是否为空由调用方判断。"""
    return str(code if code is not None else "").strip()


def _safe_dirname(name: str) -> str:
    """活动目录名校验：空串、`.`、`..`、含路径分隔符一律拒绝。"""
    raw = str(name if name is not None else "").strip()
    if not raw:
        raise ValueError("活动目录名不能为空")
    if raw in (".", "..") or ".." in raw:
        raise ValueError(f"活动目录名非法：{raw}")
    if "/" in raw or "\\" in raw:
        raise ValueError(f"活动目录名不能包含路径分隔符：{raw}")
    return raw


# 活动目录名规范：`<两位序号>-<活动名>-<期次或小区>`
# 例：12-安全隐患专项排查-七彩花都B1、04-爱邻议事会-第一期
_DIRNAME_RE = re.compile(r"^(\d{2,3})-([^-]+)-(.+)$")
_FORBIDDEN_CHARS = set('\\/:*?"<>|\t\n\r')


class DuplicateActivityError(ValueError):
    """同名活动已存在。调用方应返回 409 而不是 400。"""


def validate_activity_dirname(name: str, *,
                              existing: list[str] | None = None,
                              allow_duplicate_key: bool = False) -> list[str]:
    """校验新建活动目录名是否合规，返回告警列表；硬性不合规抛 ValueError。

    硬性规则（违反即拒绝）：
      1. 非空、不含路径分隔符、不含文件系统非法字符
      2. 必须是 `<序号>-<活动名>-<期次|小区>` 三段式，序号两位数字
      3. 活动名与期次段都不能为空
    软性规则（返回告警，不拒绝）：
      4. 去掉序号后与已有目录重名（同一活动重复建）
      5. 序号与已有目录重复（历史遗留，方案 A 允许）
    """
    raw = str(name if name is not None else "").strip()
    _safe_dirname(raw)                                  # 规则 1（含路径穿越）

    bad = sorted({c for c in raw if c in _FORBIDDEN_CHARS})
    if bad:
        raise ValueError(f"活动目录名含非法字符 {' '.join(bad)}：{raw}")
    if len(raw) > 80:
        raise ValueError(f"活动目录名过长（{len(raw)} 字符，上限 80）：{raw}")

    m = _DIRNAME_RE.match(raw)
    if not m:
        raise ValueError(
            f"活动目录名格式不符：{raw}；"
            "要求「两位序号-活动名-期次或小区」，如 12-安全隐患专项排查-七彩花都B1")
    seq, title, tail = m.group(1), m.group(2).strip(), m.group(3).strip()
    if not title:
        raise ValueError(f"活动目录名缺活动名：{raw}")
    if not tail:
        raise ValueError(f"活动目录名缺期次或小区：{raw}")

    warnings: list[str] = []
    if existing:
        # 同名活动已存在 → 硬性拒绝（409），不给「换序号再建一个」留口子。
        # 同一活动在不同序号下是同一个活动，重复建会让归档与核对全部错乱。
        key = activity_key(raw)
        same_key = [n for n in existing if activity_key(n) == key]
        if same_key and not allow_duplicate_key:
            raise DuplicateActivityError(
                f"同活动已存在：{'、'.join(same_key)}（如需重建请先删除旧目录）")
        # 序号重复只是提示（方案 A：历史序号不强制对齐）
        same_seq = [n for n in existing if n.startswith(f"{seq}-")]
        if same_seq:
            warnings.append(
                f"序号 {seq} 已被 {len(same_seq)} 个目录使用"
                f"（{same_seq[0]}…），允许，但编号不会自动对齐")
    return warnings


def activity_dirs(project_dir: Path) -> dict[str, Path]:
    """返回 {目录名: 绝对路径}，只收 <project_dir>/活动/ 下的一级子目录。"""
    act_dir = Path(project_dir) / "活动"
    if not act_dir.is_dir():
        return {}
    return {p.name: p.resolve() for p in sorted(act_dir.iterdir()) if p.is_dir()}


def find_activity_dir(project_dir: Path, keyword: str,
                      ordinal: str = "") -> Path | None:
    """按关键词匹配活动目录名，返回唯一匹配的 Path；无匹配或多匹配返回 None。

    例：find_activity_dir(proj, "爱邻议事会", "第三期")
    匹配规则：目录名同时包含 keyword 和 ordinal（ordinal 为空则不校验）。
    """
    key = str(keyword if keyword is not None else "").strip()
    period = str(ordinal if ordinal is not None else "").strip()
    if not key:
        return None
    hits = [
        path for name, path in activity_dirs(project_dir).items()
        if key in name and (not period or period in name)
    ]
    return hits[0] if len(hits) == 1 else None


def create_activity_dirs(project_dir: Path, planned: list[dict],
                         code_maker: "Callable[[dict, set[str]], str | None] | None" = None
                         ) -> list[str]:
    """按 planned 建活动目录，幂等：已存在跳过。

    原来「只 mkdir，不写任何文件」——于是 split 建出来的活动天然没有
    activity.yaml，一进入生成接口就报 CODE_MISSING，且当时没有任何 API
    能补编号，活动就此卡死（2026-11-24 客户端实测）。

    code_maker(planned_item, used_codes) -> code 由调用方注入（编号规则在
    api/routes 层，engine 不反向依赖）。给了就在建目录时写最小 activity.yaml；
    失败不阻断建目录（编号可事后用 PATCH .../code 补）。

    返回实际新建的目录名列表。
    """
    if not planned:
        return []
    act_dir = Path(project_dir) / "活动"
    act_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    used: set[str] = set()
    for item in planned:
        dirname = _safe_dirname(item.get("dirname") if isinstance(item, dict) else item)
        target = act_dir / dirname
        if target.exists():
            continue
        target.mkdir()
        created.append(dirname)
        if code_maker is not None:
            try:
                code = code_maker(item if isinstance(item, dict) else {"dirname": dirname}, used)
            except Exception:
                code = None
            if code:
                used.add(code)
                try:
                    from . import ylock as _ylock
                    payload = {"code": code, "status": "created"}
                    # 顺手把计划里已知的字段写进 yaml，省得建完还要手工补。
                    # 只写有值的键，空串会让 build_data 拿到空字段。
                    name = str(item.get("indicator_name") or "").strip()
                    community = str(item.get("community") or "").strip()
                    if name:
                        payload["活动名称"] = (f"{name}（{community}）" if community
                                                else name)
                    if community:
                        payload["活动地点"] = community
                        payload["服务对象"] = "辖区居民"
                    _ylock.atomic_write_text(
                        target / "activity.yaml",
                        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                    )
                except Exception:
                    pass
    return created


def rename_activity_dir(project_dir: Path, old: str, new: str) -> None:
    """重命名活动目录。

    old 不存在抛 FileNotFoundError；new 已存在抛 ValueError；名字非法抛 ValueError。
    """
    old_name = _safe_dirname(old)
    new_name = _safe_dirname(new)
    act_dir = Path(project_dir) / "活动"
    old_path = act_dir / old_name
    new_path = act_dir / new_name
    if not old_path.is_dir():
        raise FileNotFoundError(f"活动目录不存在：{old_name}")
    if new_path.exists():
        raise ValueError(f"目标目录已存在：{new_name}")
    old_path.rename(new_path)
