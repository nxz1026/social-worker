#!/usr/bin/env python3
"""知识图谱索引排除规则 —— 新项目不必人工记账也能自动受约束。

2026-11-24 踩坑：45 个活动索引页被送进知识图谱（其中 30 个指向空目录）。
根因是索引脚本自己维护关键词表，生成的其实是 `-资料索引.md`（连字符）
而关键词写成了 `_资料索引.md`（下划线），只匹配前缀不匹配后缀。
修法是让「是不是生成的导航页」收敛到 engine.readme.is_generated_nav()
一处判定，索引脚本引用它。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import readme  # noqa: E402


@pytest.fixture(scope="module")
def idx_mod():
    spec = importlib.util.spec_from_file_location(
        "_idx_cognee_test", REPO / "scripts" / "index_cognee.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_idx_cognee_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", [
    "_01-走访调研-第一期-资料索引.md",
    "_惠民社区保障资金项目-项目资料索引.md",
    "活动目录.docx",
    "项目管理目录.docx",
])
def test_generated_nav_recognized(name):
    assert readme.is_generated_nav(Path(name)) is True


@pytest.mark.parametrize("name", [
    "01-计划书.docx", "06-总结报告.md", "签到表.pdf",
    "21-报销单.xlsx", "联络表.docx", "阶段成果.pptx",
])
def test_business_files_not_nav(name):
    assert readme.is_generated_nav(Path(name)) is False


def test_index_script_uses_shared_judgement(idx_mod):
    """索引脚本必须走 readme.is_generated_nav，不能再有自己的关键词表。"""
    nav = Path("/p/活动/01-x/_01-x-资料索引.md")
    assert idx_mod._excluded(nav) is True
    business = Path("/p/其他/联络表.docx")
    assert idx_mod._excluded(business) is False


def test_new_project_nav_auto_excluded(idx_mod, tmp_path):
    """模拟将来新增的项目/活动：只要用生成器命名，就自动排除。"""
    act = tmp_path / "某新项目" / "活动" / "01-新活动-第一期"
    act.mkdir(parents=True)
    nav_name = readme.readme_filename("01-新活动-第一期")
    nav = act / nav_name
    nav.write_text("# 索引", encoding="utf-8")
    real = act / "01-计划书.docx"
    real.write_bytes(b"x")

    assert idx_mod._excluded(nav) is True, f"{nav_name} 应被排除"
    assert idx_mod._excluded(real) is False


def test_expense_certificate_excluded(idx_mod):
    assert idx_mod._excluded(Path("/p/其他/21-支出证明单.xlsx")) is True


def test_readme_filename_still_matches_marker():
    """生成器产出的名字必须命中 is_generated_nav —— 改命名时这条会失败提醒。"""
    for name in (readme.readme_filename("01-测试活动"),
                 readme.readme_filename("", "测试项目")):
        assert readme.is_generated_nav(Path(name)), (
            f"{name} 由生成器产出，却不被 is_generated_nav 识别；"
            "改命名规则时必须同步 _INDEX_MARKERS")
