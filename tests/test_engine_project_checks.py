"""项目级核对：按《项目管理目录.docx》的 19 项扫四个资料子目录。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import project_checks  # noqa: E402


def _write(path: Path, text: str = "x") -> None:
    """造一个占位文件，内容无所谓。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_all_19_items_complete(tmp_path):
    """19 项都有对应文件时 complete=True。"""
    files = {
        1: "方案/项目协议.docx",
        2: "方案/项目方案书.docx",
        3: "其他/项目人员劳动合同.docx",
        4: "方案/项目管理制度.docx",
        5: "其他/项目立项通知.pdf",
        6: "其他/项目变更申请表.docx",
        7: "报告/项目进度管理表.xlsx",
        8: "其他/项目资源信息库.xlsx",
        9: "其他/项目服务对象数据库.xlsx",
        10: "报告/督导记录表.docx",
        11: "其他/项目沟通记录表.docx",
        12: "协议/活动物资采购协议书.pdf",
        13: "报告/项目服务相关方评价表.docx",
        14: "其他/媒体宣传信息清单.xlsx",
        15: "其他/项目交流台账.xlsx",
        16: "报告/项目案例.docx",
        17: "报告/项目中期自评报告.docx",
        18: "报告/保障资金项目季度汇报.docx",
        19: "其他/项目结项申请.docx",
    }
    for rel in files.values():
        _write(tmp_path / rel)

    result = project_checks.check_project(tmp_path)
    assert result["complete"] is True
    assert result["ok_count"] == 19
    assert result["partial_count"] == 0
    assert result["missing_count"] == 0
    assert result["na_count"] == 0
    assert [it["no"] for it in result["items"]] == list(range(1, 20))


def test_missing_items(tmp_path):
    """只有一份协议：第 1 项齐，其余 18 项缺。"""
    _write(tmp_path / "协议" / "项目协议.pdf")
    result = project_checks.check_project(tmp_path)
    by_no = {it["no"]: it for it in result["items"]}
    assert by_no[1]["status"] == "ok"
    assert result["ok_count"] == 1
    assert result["missing_count"] == 18
    assert result["complete"] is False


def test_na_by_filename_mark(tmp_path):
    """文件名含「不适用」时该项判 na，不计入缺失。"""
    _write(tmp_path / "协议" / "项目协议-不适用.docx")
    result = project_checks.check_project(tmp_path)
    by_no = {it["no"]: it for it in result["items"]}
    assert by_no[1]["status"] == "na"
    assert result["na_count"] == 1
    assert result["missing_count"] == 18


def test_recursive_and_ignores_activity_dirs(tmp_path):
    """四个子目录递归生效；活动目录里的文件不参与项目级核对。"""
    _write(tmp_path / "方案" / "2026" / "项目申报书.docx")
    _write(tmp_path / "活动" / "01-走访" / "项目协议.docx")

    result = project_checks.check_project(tmp_path)
    by_no = {it["no"]: it for it in result["items"]}
    assert by_no[2]["status"] == "ok"
    # 活动目录里的协议不应被算进来
    assert by_no[1]["status"] == "missing"


def test_hidden_dirs_ignored(tmp_path):
    """隐藏目录（如回收站）里的文件不算数。"""
    _write(tmp_path / "协议" / ".trash" / "项目协议.docx")
    result = project_checks.check_project(tmp_path)
    by_no = {it["no"]: it for it in result["items"]}
    assert by_no[1]["status"] == "missing"


def test_missing_project_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        project_checks.check_project(tmp_path / "不存在")


def test_to_markdown(tmp_path):
    _write(tmp_path / "协议" / "项目协议.pdf")
    text = project_checks.to_markdown(project_checks.check_project(tmp_path))
    assert "| 序号 | 材料 | 状态 | 说明 |" in text
    assert "| 1 | 项目协议 | 齐 |" in text
    assert "| 2 | 项目方案书 | 缺 |" in text
    assert "**汇总**" in text
    assert "未完整" in text
