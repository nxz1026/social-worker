#!/usr/bin/env python3
"""项目编号生成与解析。

编号格式（2026-11-18 确认）：
    CDYJR{YYYY}{立项月:03d}-C-{活动类型号}{活动月:02d}{流水号:04d}
例：CDYJR2026008-C-1110001（2026 年 8 月立项，类型 1，活动月 11，流水 0001）

旧数据里存在小写 -c- 且位数错位的编号，parse_code 只做大小写容错，
不猜测错位字段；写出的编号一律大写。
"""
from __future__ import annotations

import re

ORG_PREFIX = "CDYJR"

# 结构固定：4 位年 + 3 位立项月 + -C- + 1 位类型 + 2 位活动月 + 4 位流水
_CODE_RE = re.compile(
    r"^(?P<prefix>CDYJR)(?P<year>\d{4})(?P<establish_month>\d{3})"
    r"-[cC]-(?P<act_type>\d)(?P<act_month>\d{2})(?P<serial>\d{4})$"
)


def _check_int(name: str, value, low: int, high: int) -> int:
    """字段校验：必须是整数且落在 [low, high]，否则 ValueError。"""
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数：{value!r}")
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是整数：{value!r}")
    if num < low or num > high:
        raise ValueError(f"{name} 超出范围 {low}..{high}：{num}")
    return num


def make_code(*, year: int, establish_month: int, act_type: int,
              act_month: int, serial: int) -> str:
    """生成完整编号，任一参数越界抛 ValueError。

    year 2000..2100、establish_month 1..12、act_type 0..9、
    act_month 1..12、serial 1..9999。
    """
    year = _check_int("year", year, 2000, 2100)
    establish_month = _check_int("establish_month", establish_month, 1, 12)
    act_type = _check_int("act_type", act_type, 0, 9)
    act_month = _check_int("act_month", act_month, 1, 12)
    serial = _check_int("serial", serial, 1, 9999)
    return (f"{ORG_PREFIX}{year}{establish_month:03d}-C-"
            f"{act_type}{act_month:02d}{serial:04d}")


def parse_code(code: str) -> dict:
    """解析编号，返回各字段（均为 int，prefix 为字符串）。

    接受旧数据的 -c-；格式不符抛 ValueError。
    """
    raw = str(code if code is not None else "").strip()
    m = _CODE_RE.match(raw)
    if not m:
        raise ValueError(f"编号格式不符：{raw!r}")
    return {
        "prefix": m.group("prefix"),
        "year": int(m.group("year")),
        "establish_month": int(m.group("establish_month")),
        "act_type": int(m.group("act_type")),
        "act_month": int(m.group("act_month")),
        "serial": int(m.group("serial")),
    }


def next_serial(existing_codes: list[str], *, act_type: int, year: int) -> int:
    """同一年 + 同一活动类型下，现有最大流水号 + 1；没有则 1。

    无法解析的编号直接跳过（旧数据 / 人工命名不应阻断生成）。
    """
    act_type = _check_int("act_type", act_type, 0, 9)
    year = _check_int("year", year, 2000, 2100)
    max_serial = 0
    for code in existing_codes or []:
        try:
            parsed = parse_code(code)
        except ValueError:
            continue
        if parsed["year"] != year or parsed["act_type"] != act_type:
            continue
        max_serial = max(max_serial, parsed["serial"])
    return max_serial + 1
