"""engine P2.5：活动包归档入库 + 编号读取 + activity.yaml。"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import activities, ingest, projects  # noqa: E402

HUIMIN = "惠民社区保障资金项目"
# 工单指定的真实样本；机器上没有就跳过，不伪造
# 真实活动目录（编码为 CDYJR2026008-C-1110002 的那份）。
# 原先硬编码 /tmp/act4/…，那是解压临时目录、早被清理，导致这条测试长期跳过；
# 改成走 SOCIAL_WORK_PROJECTS，与其他资产类测试口径一致。
_PROJECTS = Path(os.environ.get("SOCIAL_WORK_PROJECTS",
                                "/srv/social-work/projects"))
REAL_DOCX_DIR = _PROJECTS / "惠民社区保障资金项目" / "活动" / "11-共治行动-第一期"


def _make_docx(path: Path, text: str) -> None:
    """造一个最小 docx：只放 word/document.xml，够 read_activity_code 读。"""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


@pytest.mark.skipif(not (REAL_DOCX_DIR / "00-目录.docx").is_file(),
                    reason=f"真实活动目录不存在：{REAL_DOCX_DIR}")
def test_read_activity_code_from_real_docx():
    assert ingest.read_activity_code(REAL_DOCX_DIR) == "CDYJR2026008-C-1110002"


def test_read_activity_code_falls_back_to_plan(tmp_path):
    # 00 没有编号，01 有
    _make_docx(tmp_path / "00-目录.docx", "归档目录，无编号")
    _make_docx(tmp_path / "01-计划书.docx", "项目编号CDYJR2026008-C-1111002")
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-C-1111002"


def test_read_activity_code_prefers_catalog(tmp_path):
    _make_docx(tmp_path / "00-目录.docx", "编号CDYJR2026008-C-1110002")
    _make_docx(tmp_path / "01-计划书.docx", "编号CDYJR2026008-C-9999999")
    assert ingest.read_activity_code(tmp_path) == "CDYJR2026008-C-1110002"


def test_read_activity_code_none(tmp_path):
    _make_docx(tmp_path / "00-目录.docx", "没有编号")
    _make_docx(tmp_path / "01-计划书.docx", "也没有")
    assert ingest.read_activity_code(tmp_path) is None
    assert ingest.read_activity_code(tmp_path / "不存在") is None


def test_find_activity_dir_exact(tmp_path):
    act = tmp_path / "活动"
    act.mkdir()
    (act / "06-爱邻议事会-第三期").mkdir()
    (act / "11-共治行动-第一期").mkdir()
    found = activities.find_activity_dir(tmp_path, "爱邻议事会", "第三期")
    assert found == (act / "06-爱邻议事会-第三期").resolve()


def test_find_activity_dir_ordinal_optional(tmp_path):
    act = tmp_path / "活动"
    act.mkdir()
    (act / "11-共治行动-第一期").mkdir()
    found = activities.find_activity_dir(tmp_path, "共治行动")
    assert found is not None and found.name == "11-共治行动-第一期"


def test_find_activity_dir_multiple_returns_none(tmp_path):
    act = tmp_path / "活动"
    act.mkdir()
    (act / "04-爱邻议事会-第一期").mkdir()
    (act / "05-爱邻议事会-第二期").mkdir()
    # 关键词命中两个、没给期数 → 多匹配
    assert activities.find_activity_dir(tmp_path, "爱邻议事会") is None
    # 关键词 + 期数仍命中两个（同名副本）→ 多匹配也返回 None
    (act / "04-爱邻议事会-第一期-副本").mkdir()
    assert activities.find_activity_dir(tmp_path, "爱邻议事会", "第一期") is None


def test_find_activity_dir_no_match(tmp_path):
    assert activities.find_activity_dir(tmp_path, "共治行动", "第一期") is None
    assert activities.find_activity_dir(tmp_path, "", "第一期") is None


def test_import_activity_tree_copies_and_skips(tmp_path):
    src = tmp_path / "src"
    (src / "03-签到表").mkdir(parents=True)
    (src / "00-目录.docx").write_text("a", encoding="utf-8")
    (src / "03-签到表" / "签到.png").write_text("b", encoding="utf-8")

    dst = tmp_path / "dst"
    first = ingest.import_activity_tree(src, dst)
    assert sorted(first["copied"]) == ["00-目录.docx", "03-签到表/签到.png"]
    assert first["skipped"] == [] and first["errors"] == []
    assert (dst / "03-签到表" / "签到.png").read_text(encoding="utf-8") == "b"

    # 第二次全部已存在：不覆盖，只记 skipped
    (dst / "00-目录.docx").write_text("改过", encoding="utf-8")
    second = ingest.import_activity_tree(src, dst)
    assert second["copied"] == []
    assert sorted(second["skipped"]) == ["00-目录.docx", "03-签到表/签到.png"]
    assert (dst / "00-目录.docx").read_text(encoding="utf-8") == "改过"


def test_import_activity_tree_missing_source(tmp_path):
    result = ingest.import_activity_tree(tmp_path / "没有", tmp_path / "dst")
    assert result["copied"] == [] and result["skipped"] == []
    assert result["errors"] and "源目录不存在" in result["errors"][0]


def test_write_activity_yaml_idempotent(tmp_path):
    act = tmp_path / "活动" / "11-共治行动-第一期"
    kwargs = dict(code="CDYJR2026008-C-1110002",
                  source="/tmp/act4/共治行动第一期",
                  imported_at="2026-11-18T16:30:00+08:00")
    path = ingest.write_activity_yaml(act, **kwargs)
    first = path.read_bytes()
    ingest.write_activity_yaml(act, **kwargs)
    assert path.read_bytes() == first

    data = ingest.read_activity_yaml(act)
    assert data == {
        "code": "CDYJR2026008-C-1110002",
        "source": "/tmp/act4/共治行动第一期",
        "imported_at": "2026-11-18T16:30:00+08:00",
        "status": "imported",
    }


def test_write_activity_yaml_merges_existing_fields(tmp_path):
    act = tmp_path / "活动" / "06-爱邻议事会-第三期"
    act.mkdir(parents=True)
    (act / "activity.yaml").write_text(
        "code: 旧编号\n负责人: 张三\n", encoding="utf-8")
    ingest.write_activity_yaml(act, code="CDYJR2026008-C-1110001",
                               source="/tmp/act4/爱邻议事会第三期",
                               imported_at="2026-11-18T16:31:00+08:00")
    data = ingest.read_activity_yaml(act)
    assert data["code"] == "CDYJR2026008-C-1110001"
    assert data["负责人"] == "张三"          # 原有字段保留
    assert data["status"] == "imported"
    assert not (act / "activity.yaml.tmp").exists()


def test_read_activity_yaml_missing(tmp_path):
    assert ingest.read_activity_yaml(tmp_path) == {}


def test_projects_activity_dir():
    path = projects.activity_dir(HUIMIN, "11-共治行动-第一期")
    assert path.name == "11-共治行动-第一期"
    assert path.parent.name == "活动"
    assert path.parent.parent.name == HUIMIN


def test_projects_activity_dir_rejects_traversal():
    with pytest.raises(ValueError):
        projects.activity_dir("..", "11-共治行动-第一期")
    with pytest.raises(ValueError):
        projects.activity_dir(HUIMIN, "../逃逸")
    with pytest.raises(ValueError):
        projects.activity_dir(HUIMIN, "")
