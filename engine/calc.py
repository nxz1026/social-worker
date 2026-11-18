#!/usr/bin/env python3
"""纯计算工具：人民币大写、金额格式化、按列求和。

本模块不做任何 IO，也不依赖第三方库，方便单测与在别处复用。
金额一律走 Decimal，避免浮点尾数把「分」算错。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# 大写数字与位权
_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_UNITS = ("", "拾", "佰", "仟")
_GROUP_UNITS = ("", "万", "亿", "万亿")

# 支持的最大金额：999999999999.99
_MAX_AMOUNT = Decimal("999999999999.99")


def _to_decimal(value) -> Decimal:
    """把 int / float / str / Decimal 转成 Decimal；非法值抛 ValueError。"""
    if isinstance(value, bool):
        raise ValueError(f"金额不是数字：{value!r}")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        raise ValueError(f"金额不是数字：{value!r}")


def _four(n: int) -> str:
    """0..9999 → 大写（不含「万/亿」组单位），内部连续零压成一个「零」。

    例：340 → 叁佰肆拾；1005 → 壹仟零伍；5 → 伍。
    """
    out: list[str] = []
    pending_zero = False
    for i, ch in enumerate(f"{n:04d}"):
        digit = int(ch)
        if digit == 0:
            pending_zero = True
            continue
        if pending_zero and out:
            out.append("零")
        pending_zero = False
        out.append(_DIGITS[digit] + _UNITS[3 - i])
    return "".join(out)


def _int_upper(n: int) -> str:
    """非负整数 → 大写，按四位一组套「万 / 亿」。

    例：10000 → 壹万；10005 → 壹万零伍；100000001 → 壹亿零壹。
    """
    if n == 0:
        return "零"

    groups: list[int] = []
    while n:
        groups.append(n % 10000)
        n //= 10000

    parts: list[str] = []
    for idx in range(len(groups) - 1, -1, -1):
        group = groups[idx]
        if group == 0:
            # 高位组后面还有低位非零组时才补零，且不重复补
            if parts and not parts[-1].endswith("零"):
                parts.append("零")
            continue
        # 例如 壹亿 + 零壹万：低位组不足四位要补零
        if parts and group < 1000 and not parts[-1].endswith("零"):
            parts.append("零")
        parts.append(_four(group) + _GROUP_UNITS[idx])

    # 末尾补出来的零（如 10000 → 壹万零）去掉
    return "".join(parts).rstrip("零") or "零"


def rmb_upper(amount) -> str:
    """人民币金额转大写，精确到分、四舍五入。

    - 0 → 零元整
    - 340.9 → 叁佰肆拾元玖角整（角有值、分为 0）
    - 340.94 → 叁佰肆拾元玖角肆分（角分都有）
    - 695 → 陆佰玖拾伍元整（角分都无）
    - 0.5 → 伍角整（不足一元不写「元」）
    - 340.05 → 叁佰肆拾元零伍分（角为 0、分有值）

    负数或超过 999999999999.99 抛 ValueError。
    """
    value = _to_decimal(amount)
    if value < 0:
        raise ValueError(f"金额不能为负数：{amount!r}")
    if value > _MAX_AMOUNT:
        raise ValueError(f"金额超出支持范围（最大 999999999999.99）：{amount!r}")

    value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cents = int(value * 100)
    yuan, rest = divmod(cents, 100)
    jiao, fen = divmod(rest, 10)

    if yuan == 0 and jiao == 0 and fen == 0:
        return "零元整"

    out = (_int_upper(yuan) + "元") if yuan else ""
    if fen == 0:
        # 分为 0：有角写「X角整」，没角写「元整」
        out += "整" if jiao == 0 else _DIGITS[jiao] + "角整"
    elif jiao == 0:
        # 角为 0、分有值：元零X分（不足一元时只写「零X分」）
        out += "零" + _DIGITS[fen] + "分"
    else:
        out += _DIGITS[jiao] + "角" + _DIGITS[fen] + "分"
    return out


def money(x) -> str:
    """金额格式化：保留 2 位小数 + 千分位。494 → '494.00'。"""
    value = _to_decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{value:,.2f}"


def sum_rows(rows: list[dict], key: str) -> float:
    """按 key 求和，返回 float。

    - 缺 key、值为 None 或空串的行跳过
    - 值存在但不是数字时抛 ValueError，并说明是第几行
    """
    total = Decimal("0")
    for index, row in enumerate(rows or [], start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 行不是字典：{row!r}")
        value = row.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, bool):
            raise ValueError(f"第 {index} 行 {key} 不是数字：{value!r}")
        try:
            total += Decimal(str(value).replace(",", "").strip())
        except (InvalidOperation, ValueError):
            raise ValueError(f"第 {index} 行 {key} 不是数字：{value!r}")
    return float(total)
