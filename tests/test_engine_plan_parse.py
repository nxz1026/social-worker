"""计划书解析：按表头名从已有 docx 里取环节 / 困难 / 物资。

真实计划书的列顺序、列数都可能与模板不同，这里用自造 docx 验证「按表头匹配」，
再对仓库外的真实计划书做一次端到端校验（文件不在时跳过）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from docx import Document

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import plan_parse  # noqa: E402

# 真实计划书：只在部署环境存在，CI 上跳过
REAL_ACT = Path("/srv/social-work/projects/惠民社区保障资金项目/活动/11-共治行动-第一期")


def _write_docx(path: Path, tables) -> None:
    """把 [(表头, 数据行)] 写成 docx 的三张表。"""
    doc = Document()
    for headers, rows in tables:
        table = doc.add_table(rows=1 + len(rows), cols=len(headers))
        for col, header in enumerate(headers):
            table.cell(0, col).text = header
        for row_index, row in enumerate(rows, start=1):
            for col, value in enumerate(row):
                table.cell(row_index, col).text = value
    doc.save(str(path))


def _shifted_tables():
    """列顺序全部打乱，验证解析不依赖列序号。"""
    steps = (
        ("时间", "阶段", "内容", "人员", "所需物资", "备注"),
        [
            ("9:00", "准备阶段", "签到", "社工", "签到表", ""),
            # 阶段列纵向合并时，后续行为空，应按上一行补齐
            ("9:30", "", "分组清理通道", "志愿者", "手套、垃圾袋", ""),
        ],
    )
    difficulties = (
        ("解决措施", "预计困难"),
        [("提前张贴告知", "车主不配合")],
    )
    materials = (
        ("物资名称", "类别", "单价（元）", "数量\\规格", "小计（元）"),
        [("横幅", "广告宣传费", "50", "2条", "100")],
    )
    return (steps, difficulties, materials)


def test_parse_matches_columns_by_header_name(tmp_path: Path):
    act_dir = tmp_path / "01-测试活动-第一期"
    act_dir.mkdir()
    _write_docx(act_dir / "01-计划书-测试.docx", _shifted_tables())

    book = plan_parse.parse_existing_plan(act_dir)
    assert book is not None
    assert len(book["环节"]) == 2
    assert book["环节"][0] == {"阶段": "准备阶段", "时间": "9:00", "内容": "签到",
                               "所需物资": "签到表", "人员": "社工", "备注": ""}
    assert book["环节"][1]["阶段"] == "准备阶段"        # 合并格向下补齐
    assert book["环节"][1]["所需物资"] == "手套、垃圾袋"
    assert book["困难"] == [{"预计困难": "车主不配合", "解决方案": "提前张贴告知"}]
    assert book["物资"][0]["物资名称"] == "横幅"
    assert book["物资"][0]["单价"] == 50
    assert book["物资"][0]["小计"] == 100
    assert book["物资合计"] == 100


def test_parse_returns_none_without_plan_docx(tmp_path: Path):
    act_dir = tmp_path / "01-空活动-第一期"
    act_dir.mkdir()
    assert plan_parse.parse_existing_plan(act_dir) is None


def test_parse_returns_none_when_no_step_table(tmp_path: Path):
    """只有困难表、没有流程表时视为解析失败。"""
    act_dir = tmp_path / "01-残缺-第一期"
    act_dir.mkdir()
    _write_docx(act_dir / "01-计划书-残缺.docx", [
        (("预计困难", "解决方案"), [("下雨", "改期")]),
    ])
    assert plan_parse.parse_existing_plan(act_dir) is None


@pytest.mark.skipif(not REAL_ACT.is_dir(), reason="部署环境外没有真实计划书")
def test_parse_real_plan_has_at_least_five_steps():
    book = plan_parse.parse_existing_plan(REAL_ACT)
    assert book is not None
    assert len(book["环节"]) >= 5
    assert book["环节"][0]["阶段"] == "准备阶段"
    assert len(book["困难"]) >= 2
    assert len(book["物资"]) >= 2
    assert book["物资合计"] > 0
