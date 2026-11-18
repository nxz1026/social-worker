#!/usr/bin/env python3
"""项目编号生成与解析。

编号格式（2026-11-18 方案 B：加一位活动年）：
    CDYJR{立项年4}{立项月:03d}-C-{活动类型号1}{活动年个位1}{活动月:02d}{流水号:04d}
例：CDYJR2026008-C-16110001（2026 年 8 月立项，类型 1，活动年 2026，活动月 11，流水 0001）

活动年只取个位（2026→6、2027→7），用来区分跨年活动：同样 5 月、同样流水，
2026 与 2027 不会再撞号。

旧格式（无活动年）仍可解析，parse_code 返回的 act_year 为 None：
    CDYJR{立项年4}{立项月:03d}-C-{活动类型号1}{活动月:02d}{流水号:04d}
例：CDYJR2026008-C-1110001

旧数据里存在小写 -c- 且位数错位的编号，parse_code 只做大小写容错，
不猜测错位字段；写出的编号一律大写。
"""
from __future__ import annotations

import re

ORG_PREFIX = "CDYJR"

# 新格式：4 位年 + 3 位立项月 + -C- + 1 位类型 + 1 位活动年 + 2 位活动月 + 4 位流水
_CODE_RE = re.compile(
    r"^(?P<prefix>CDYJR)(?P<year>\d{4})(?P<establish_month>\d{3})"
    r"-[cC]-(?P<act_type>\d)(?P<act_year>\d)(?P<act_month>\d{2})(?P<serial>\d{4})$"
)

# 旧格式：结构相同，但只有 2 位活动月、没有活动年
_LEGACY_CODE_RE = re.compile(
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
              act_month: int, serial: int, act_year: int) -> str:
    """生成完整编号，任一参数越界抛 ValueError。

    year 2000..2100（立项年）、establish_month 1..12、act_type 0..9、
    act_year 2000..2100（活动年，只取个位进编号）、act_month 1..12、
    serial 1..9999。
    """
    year = _check_int("year", year, 2000, 2100)
    establish_month = _check_int("establish_month", establish_month, 1, 12)
    act_type = _check_int("act_type", act_type, 0, 9)
    act_year = _check_int("act_year", act_year, 2000, 2100)
    act_month = _check_int("act_month", act_month, 1, 12)
    serial = _check_int("serial", serial, 1, 9999)
    return (f"{ORG_PREFIX}{year}{establish_month:03d}-C-"
            f"{act_type}{act_year % 10}{act_month:02d}{serial:04d}")


def parse_code(code: str) -> dict:
    """解析编号，返回各字段（act_year 为个位数，prefix 为字符串）。

    先按新格式（含活动年）匹配，失败再按旧格式匹配；旧格式的 act_year 为 None
    （信息本来就丢了，不还原成四位年）。接受旧数据的 -c-；格式不符抛 ValueError。
    """
    raw = str(code if code is not None else "").strip()
    m = _CODE_RE.match(raw)
    if m:
        return {
            "prefix": m.group("prefix"),
            "year": int(m.group("year")),
            "establish_month": int(m.group("establish_month")),
            "act_type": int(m.group("act_type")),
            "act_year": int(m.group("act_year")),
            "act_month": int(m.group("act_month")),
            "serial": int(m.group("serial")),
        }
    m = _LEGACY_CODE_RE.match(raw)
    if not m:
        raise ValueError(f"编号格式不符：{raw!r}")
    return {
        "prefix": m.group("prefix"),
        "year": int(m.group("year")),
        "establish_month": int(m.group("establish_month")),
        "act_type": int(m.group("act_type")),
        "act_year": None,          # 旧格式没有活动年，无从还原
        "act_month": int(m.group("act_month")),
        "serial": int(m.group("serial")),
    }


def next_serial(existing_codes: list[str], *, act_type: int,
                year: int, act_year: int) -> int:
    """同立项年 + 同类型 + 同活动年(个位) 下，现有最大流水号 + 1；没有则 1。

    旧格式编号（无活动年）无法确定归属哪个活动年，直接跳过不参与计算；
    无法解析的编号同样跳过（旧数据 / 人工命名不应阻断生成）。
    """
    act_type = _check_int("act_type", act_type, 0, 9)
    year = _check_int("year", year, 2000, 2100)
    act_year = _check_int("act_year", act_year, 2000, 2100) % 10
    max_serial = 0
    for code in existing_codes or []:
        try:
            parsed = parse_code(code)
        except ValueError:
            continue
        if parsed["act_year"] is None:
            continue
        if (parsed["year"] != year or parsed["act_type"] != act_type
                or parsed["act_year"] != act_year):
            continue
        max_serial = max(max_serial, parsed["serial"])
    return max_serial + 1
