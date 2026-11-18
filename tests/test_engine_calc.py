"""engine P3：纯计算（人民币大写 / 金额格式化 / 求和）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine.calc import money, rmb_upper, sum_rows  # noqa: E402


@pytest.mark.parametrize("amount,expected", [
    (0, "零元整"),
    (0.5, "伍角整"),
    (340.9, "叁佰肆拾元玖角整"),
    (340.94, "叁佰肆拾元玖角肆分"),
    (695, "陆佰玖拾伍元整"),
    (1000, "壹仟元整"),
    (1662, "壹仟陆佰陆拾贰元整"),
    (1234.56, "壹仟贰佰叁拾肆元伍角陆分"),
    (10000, "壹万元整"),
    (1000000, "壹佰万元整"),
])
def test_rmb_upper(amount, expected):
    assert rmb_upper(amount) == expected


def test_rmb_upper_zero_between_units():
    """组内 / 组间的零只写一个，且不吞掉低位的数字。"""
    assert rmb_upper(1005) == "壹仟零伍元整"
    assert rmb_upper(10005) == "壹万零伍元整"
    assert rmb_upper(340.05) == "叁佰肆拾元零伍分"


def test_rmb_upper_rounds_half_up():
    # 四舍五入到分：0.005 → 0.01，1.999 → 2.00
    assert rmb_upper(0.005) == rmb_upper(0.01)
    assert rmb_upper(1.999) == "贰元整"


def test_rmb_upper_rejects_negative_and_overflow():
    with pytest.raises(ValueError):
        rmb_upper(-1)
    with pytest.raises(ValueError):
        rmb_upper(10 ** 12)


def test_money():
    assert money(494) == "494.00"
    assert money(1234.5) == "1,234.50"
    assert money("0") == "0.00"


def test_sum_rows_skips_missing():
    rows = [{"金额": 100}, {"金额": "2.5"}, {"金额": None}, {"备注": "无金额"}]
    assert sum_rows(rows, "金额") == 102.5


def test_sum_rows_reports_bad_row():
    rows = [{"金额": 1}, {"金额": "abc"}]
    with pytest.raises(ValueError) as exc:
        sum_rows(rows, "金额")
    assert "第 2 行" in str(exc.value)
