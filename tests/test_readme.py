"""D1：活动 / 项目 README 索引生成与全量刷新。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import projects, readme  # noqa: E402

REAL_PROJECT = "惠民社区保障资金项目"
REAL_ACTIVITY = "06-爱邻议事会-第三期"
REAL_ACT = (Path(projects.PROJECTS_ROOT) / REAL_PROJECT
            / "活动" / REAL_ACTIVITY)


def _write(path: Path, text: str = "x") -> None:
    """造一个占位文件，内容无所谓。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """把项目根指到 tmp，避免碰真实数据。"""
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path)
    return tmp_path


def _make_activity(root: Path, project: str, dirname: str,
                   code: str = "CDYJR2026008-C-1110001") -> Path:
    """建一个最小活动目录（带 activity.yaml）。"""
    act = root / project / "活动" / dirname
    act.mkdir(parents=True)
    _write(act / "activity.yaml", f"code: {code}\nstatus: imported\n")
    return act


def test_status_icon_mapping():
    """四种状态图标与工单约定一致。"""
    assert readme.STATUS_ICON == {
        "ok": "✅", "partial": "⚠️", "missing": "❌", "na": "—",
    }


def test_render_activity_table_and_icons(fake_root):
    """表格里有活动名、编号，四种状态图标都落到对应行。"""
    act = _make_activity(fake_root, "测试项目", "01-走访调研-第一期")
    _write(act / "01-计划书.docx", "x" * 2048)
    _write(act / "03-签到表.docx")          # 未签字 docx → partial
    _write(act / "11-活动照片" / "a.jpg")    # 目录里有图片 → ok

    text = readme.render_activity_readme("测试项目", "01-走访调研-第一期")

    assert "# 走访调研（第一期）" in text
    assert "编号：CDYJR2026008-C-1110001" in text
    assert "## 材料清单" in text
    assert "| 序号 | 材料 | 状态 | 文件 |" in text
    assert "| 1 | 活动计划 | ✅ | [[01-计划书.docx]] |" in text
    assert "| 3 | 签到表 | ⚠️ 待补 | （无） |" in text
    assert "| 5 | 总结报告 | ❌ | （无） |" in text
    assert "| 15 | 其他 | — | （无） |" in text
    # 文件列表：目录显示文件数，内部文件不进清单
    assert "- 11-活动照片/（1 个文件）" in text
    assert "activity.yaml" not in text


def test_render_project_readme_lists_activities(fake_root):
    """项目 README 含 19 项核对与活动链接。"""
    _make_activity(fake_root, "测试项目", "01-走访调研-第一期")

    text = readme.render_project_readme("测试项目")

    assert "# 测试项目" in text
    assert "## 项目材料" in text
    assert "| 1 | 项目协议 |" in text
    assert "## 活动" in text
    assert "[[01-走访调研-第一期/_01-走访调研-第一期-资料索引|走访调研（第一期）]]" in text


def test_write_activity_readme_idempotent(fake_root):
    """第一次写返回 True，内容没变第二次返回 False。"""
    act = _make_activity(fake_root, "测试项目", "01-走访调研-第一期")
    _write(act / "01-计划书.docx")

    assert readme.write_activity_readme("测试项目", "01-走访调研-第一期") is True
    assert (act / "_01-走访调研-第一期-资料索引.md").is_file()
    assert readme.write_activity_readme("测试项目", "01-走访调研-第一期") is False


def test_refresh_all_in_tmp(fake_root):
    """dry-run 不落盘；正式跑写 4 个；再跑全部跳过。"""
    _make_activity(fake_root, "甲项目", "01-走访调研-第一期")
    _make_activity(fake_root, "乙项目", "01-走访调研-第一期")

    dry = readme.refresh_all(dry_run=True)
    assert dry["errors"] == []
    assert len(dry["written"]) == 4          # 2 个活动 + 2 个项目
    assert not list(fake_root.rglob("*资料索引.md"))

    first = readme.refresh_all()
    assert first["errors"] == []
    assert len(first["written"]) == 4
    assert first["skipped"] == 0
    assert (fake_root / "甲项目" / "_甲项目-项目资料索引.md").is_file()
    assert (fake_root / "甲项目" / "活动" / "01-走访调研-第一期"
            / "_01-走访调研-第一期-资料索引.md").is_file()

    second = readme.refresh_all()
    assert second["written"] == []
    assert second["skipped"] == 4


@pytest.mark.skipif(not REAL_ACT.is_dir(), reason=f"真实活动目录不存在：{REAL_ACT}")
def test_real_activity_readme():
    """真实活动：标题、编号、材料清单表格齐全。"""
    text = readme.render_activity_readme(REAL_PROJECT, REAL_ACTIVITY)
    assert "# 爱邻议事会（第三期）" in text
    assert "CDYJR2026008-C-1110001" in text
    assert "## 材料清单" in text
    assert "| 序号 | 材料 | 状态 | 文件 |" in text
