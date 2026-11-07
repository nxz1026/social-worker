#!/usr/bin/env python3
"""M10 文件名解析层测试：日期 / 单位 / 内容关键词 / 可用性判定。

手写资料的 OCR 不可能 100% 成功，所以归档只吃**文件名**里的两样元数据：
日期（建时间关系）和单位（建项目关系）。这个文件只测纯函数，不碰磁盘。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"sw_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


np = _load("name_parse")

# 与 config/project_aliases.yaml 同构（真配置由规则层加载，这里只给解析器喂别名）
ALIASES = {"huadu": ["花都", "世纪光华", "光华小区"],
           "huimin": ["惠民", "民乐北巷", "公平街道"]}
LABELS = {"huadu": "花都", "huimin": "惠民"}


# ---------- parse_date ----------

@pytest.mark.parametrize("name,expected", [
    ("花都-2026年11月5日-织围巾登记-名单.jpg", "2026-11-05"),
    ("2026-11-05-走访.jpg", "2026-11-05"),
    ("世纪光华2026.11.5入户照片.jpg", "2026-11-05"),
    ("惠民_2026_11_6_隔离桩.jpg", "2026-11-06"),
    ("微信图片_20261107103000.jpg", "2026-11-07"),   # 14 位手机导出时间戳
    ("IMG_20261105_160849.jpg", "2026-11-05"),       # 8 位紧凑日期
    ("乱七八糟.jpg", ""),
    ("2026-13-05.jpg", ""),                          # 月份非法
    ("2026年11月.jpg", ""),                          # 没有日
    ("IMG_2026.jpg", ""),                            # 4 位数字不算日期
])
def test_parse_date(name, expected):
    assert np.parse_date(name) == expected


# ---------- parse_unit ----------

def test_parse_unit_from_project_keyword():
    assert np.parse_unit("花都-2026年11月5日-织围巾登记.jpg", ALIASES, LABELS) == \
        ("huadu", "花都", "花都")


def test_parse_unit_prefers_longest_keyword():
    # 「世纪光华」比「花都」长，命中词应该是前者（实体更精确）
    key, label, matched = np.parse_unit("花都-世纪光华-入户名单.jpg", ALIASES, LABELS)
    assert (key, label, matched) == ("huadu", "花都", "世纪光华")


def test_parse_unit_no_keyword():
    assert np.parse_unit("花都-2026年11月5日.jpg", {"huimin": ["惠民"]}, LABELS) == ("", "", "")


def test_parse_unit_label_falls_back_to_key():
    key, label, _ = np.parse_unit("惠民-走访.jpg", {"huimin": ["惠民"]}, {})
    assert (key, label) == ("huimin", "huimin")


# ---------- parse_content ----------

def test_parse_content_strips_date_unit_and_noise():
    name = "花都-2026年11月5日-织围巾登记-名单.jpg"
    assert np.parse_content(name, "2026-11-05", "花都") == "织围巾登记-名单"


def test_parse_content_keeps_two_char_keyword():
    assert np.parse_content("2026-11-05-走访.jpg", "2026-11-05", "") == "走访"


def test_parse_content_drops_pure_noise():
    assert np.parse_content("微信图片_20261107103000.jpg", "2026-11-07", "") == ""


def test_parse_content_drops_digits_only():
    assert np.parse_content("图片12345.jpg", "", "") == ""


# ---------- parse / usable / describe ----------

def test_parse_returns_all_fields():
    info = np.parse("世纪光华2026.11.5入户照片.jpg", ALIASES, LABELS)
    assert info == {"date": "2026-11-05", "unit": "huadu", "unit_label": "花都",
                    "matched": "世纪光华", "content": "入户"}


def test_usable_needs_date_or_unit():
    assert np.usable({"date": "2026-11-05", "unit": ""}) is True
    assert np.usable({"date": "", "unit": "huimin"}) is True
    # 只有内容关键词不算「识别得了」—— 时间与项目都建不起关系
    assert np.usable({"date": "", "unit": "", "content": "乱七八糟"}) is False
    assert np.usable(None) is False


def test_describe_line():
    info = np.parse("花都-2026年11月5日-织围巾登记.jpg", ALIASES, LABELS)
    assert np.describe(info) == "日期 2026-11-05 / 单位 花都 / 内容 织围巾登记"
    assert np.describe({}) == "日期 无 / 单位 无"
