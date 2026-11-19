"""engine P4：归档核对（15 项清单）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import checks, projects  # noqa: E402

HUADU_BAOZHANG = "花都社区保障资金项目"
# 工单指定的完整包；机器上没有就跳过，不伪造
REAL_ACT = (Path(projects.PROJECTS_ROOT) / HUADU_BAOZHANG
            / "活动" / "13-爱邻议事会-第一期")


def _write(path: Path, text: str = "x") -> None:
    """造一个占位文件，内容无所谓。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.skipif(not REAL_ACT.is_dir(), reason=f"真实活动目录不存在：{REAL_ACT}")
def test_real_activity_complete():
    """实测包：03 有签字 PDF、04 有签字照片，09 只有 docx → 待补。

    函数名沿用旧名（不删已有测试）；规则变更后此包不再 complete。
    """
    result = checks.check(REAL_ACT)
    assert result["complete"] is False
    assert result["missing_count"] == 0
    assert result["ok_count"] == 13
    assert result["partial_count"] == 1
    assert result["na_count"] == 1
    assert [it["no"] for it in result["items"]] == list(range(1, 16))
    item8 = next(it for it in result["items"] if it["no"] == 8)
    assert item8["status"] == "partial"
    assert "未签字 docx" in item8["detail"]
    assert result["items"][-1]["status"] == "na"


def test_empty_activity_missing_count(tmp_path):
    """只有 01-x.docx：01 齐，其余 13 项缺，第 15 项不适用。"""
    _write(tmp_path / "01-x.docx")
    result = checks.check(tmp_path)
    assert result["complete"] is False
    assert result["ok_count"] == 1
    assert result["partial_count"] == 0
    assert result["missing_count"] == 13
    assert result["na_count"] == 1
    assert result["items"][0]["status"] == "ok"


def test_not_applicable_from_yaml(tmp_path):
    """activity.yaml 的 not_applicable 让指定序号变 na。"""
    _write(tmp_path / "01-x.docx")
    _write(tmp_path / "activity.yaml", "not_applicable: [15]\n")
    result = checks.check(tmp_path)
    item15 = next(it for it in result["items"] if it["no"] == 15)
    assert item15["status"] == "na"
    assert result["na_count"] == 1
    # 第 15 项本来就是不适用，再标 3 才看得出 yaml 生效
    _write(tmp_path / "activity.yaml", "not_applicable: [3, 15]\n")
    result = checks.check(tmp_path)
    item3 = next(it for it in result["items"] if it["no"] == 3)
    assert item3["status"] == "na"
    assert result["na_count"] == 2
    assert result["missing_count"] == 12


def test_na_by_filename_mark(tmp_path):
    """文件名里写「不适用」也算标记。"""
    _write(tmp_path / "03-签到表-不适用.txt")
    result = checks.check(tmp_path)
    item3 = next(it for it in result["items"] if it["no"] == 3)
    assert item3["status"] == "na"


def test_04_dir_does_not_count_as_survey_docx(tmp_path):
    """第 14 项：只有 docx 算待补，04- 目录里的签字照片才算齐。

    函数名沿用旧名（不删已有测试）；旧断言「04- 目录里的 jpg 不算」
    已按新规则反转。
    """
    _write(tmp_path / "04-服务对象满意度调查表.docx")
    result = checks.check(tmp_path)
    item14 = next(it for it in result["items"] if it["no"] == 14)
    assert item14["status"] == "partial"
    assert "缺签字扫描件" in item14["detail"]

    _write(tmp_path / "04-满意度调查表资料" / "01.jpg")
    result = checks.check(tmp_path)
    item14 = next(it for it in result["items"] if it["no"] == 14)
    assert item14["status"] == "ok"


def test_03_signin_needs_signed_photo(tmp_path):
    """第 3 项：签到表 docx 只是待补，签字照片/PDF 才算齐。"""
    _write(tmp_path / "03-签到表.docx")
    result = checks.check(tmp_path)
    item3 = next(it for it in result["items"] if it["no"] == 3)
    assert item3["status"] == "partial"

    _write(tmp_path / "03-签到表" / "扫描件.pdf")
    result = checks.check(tmp_path)
    item3 = next(it for it in result["items"] if it["no"] == 3)
    assert item3["status"] == "ok"


def test_09_supervision_only_docx_is_partial(tmp_path):
    """第 8 项：只有未签字 docx 判待补，不能算齐。"""
    _write(tmp_path / "09-监督记录表.docx")
    result = checks.check(tmp_path)
    item8 = next(it for it in result["items"] if it["no"] == 8)
    assert item8["status"] == "partial"
    assert "仅有未签字 docx" in item8["detail"]
    assert result["complete"] is False

    _write(tmp_path / "09-监督记录表-签字.pdf")
    result = checks.check(tmp_path)
    item8 = next(it for it in result["items"] if it["no"] == 8)
    assert item8["status"] == "ok"


def test_10_needs_dir_with_media(tmp_path):
    """第 10 项要目录里有图片；只有同名 docx 不算。"""
    _write(tmp_path / "10-物资照片.docx")
    result = checks.check(tmp_path)
    item10 = next(it for it in result["items"] if it["no"] == 10)
    assert item10["status"] == "missing"

    _write(tmp_path / "10-物资照片资料" / "a.JPG")
    result = checks.check(tmp_path)
    item10 = next(it for it in result["items"] if it["no"] == 10)
    assert item10["status"] == "ok"


def test_09_and_21_both_accepted(tmp_path):
    """报销单两种编号都认。"""
    _write(tmp_path / "21-报销单.xlsx")
    result = checks.check(tmp_path)
    item9 = next(it for it in result["items"] if it["no"] == 9)
    assert item9["status"] == "ok"

    other = tmp_path / "另一份"
    _write(other / "09-报销单.xlsx")
    result = checks.check(other)
    item9 = next(it for it in result["items"] if it["no"] == 9)
    assert item9["status"] == "ok"


def test_12_and_13_need_content(tmp_path):
    """12-/13- 空目录不算齐。"""
    (tmp_path / "12-发票").mkdir()
    (tmp_path / "13-其他佐证").mkdir()
    result = checks.check(tmp_path)
    by_no = {it["no"]: it for it in result["items"]}
    assert by_no[12]["status"] == "missing"
    assert by_no[13]["status"] == "missing"

    _write(tmp_path / "12-发票" / "a.pdf")
    _write(tmp_path / "13-其他佐证" / "b.txt")
    result = checks.check(tmp_path)
    by_no = {it["no"]: it for it in result["items"]}
    assert by_no[12]["status"] == "ok"
    assert by_no[13]["status"] == "ok"


def test_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        checks.check(tmp_path / "不存在")


def test_to_markdown(tmp_path):
    _write(tmp_path / "01-x.docx")
    text = checks.to_markdown(checks.check(tmp_path))
    assert "| 序号 | 材料 | 状态 | 说明 |" in text
    assert "| 1 | 活动计划 | 齐 |" in text
    assert "| 2 | 活动通知 | 缺 |" in text
    assert "| 15 | 其他 | 不适用 |" in text
    assert "**汇总**" in text
    assert "未完整" in text
