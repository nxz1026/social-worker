"""engine.doc_mirror：docx/xlsx/pdf 同名 md 骨架 + .stignore 规则。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from docx import Document

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import doc_mirror, projects  # noqa: E402

PROJECT = "测试项目"
ACT = "11-共治行动-第一期"


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """项目根指到 tmp，避免碰真实资料。"""
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path)
    project = tmp_path / PROJECT
    (project / "活动" / ACT).mkdir(parents=True)
    (project / f"{PROJECT}.yaml").write_text(
        "project:\n  short: 测试项目\n", encoding="utf-8")
    (project / "活动" / ACT / "activity.yaml").write_text(
        "活动名称: 共治行动（第一期）\n", encoding="utf-8")
    return tmp_path


def _docx(path: Path, text: str = "这是共治行动的活动计划正文。") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))
    return path


def test_mirror_path_and_should_mirror(tmp_path):
    doc = _docx(tmp_path / "01-计划书.docx")
    assert doc_mirror.mirror_path(doc) == tmp_path / "01-计划书.md"
    assert doc_mirror.should_mirror(doc) is True

    (tmp_path / "01-计划书.md").write_text("已有", encoding="utf-8")
    assert doc_mirror.should_mirror(doc) is False

    hidden = _docx(tmp_path / ".隐藏.docx")
    index = _docx(tmp_path / "_索引.docx")
    other = tmp_path / "a.txt"
    other.write_text("x", encoding="utf-8")
    assert doc_mirror.should_mirror(hidden) is False
    assert doc_mirror.should_mirror(index) is False
    assert doc_mirror.should_mirror(other) is False


def test_render_mirror_contains_links_and_summary(fake_root):
    doc = _docx(fake_root / PROJECT / "活动" / ACT / "01-计划书-共治行动.docx",
                "共治行动第一期计划：开展议事协商，形成治理公约。")

    text = doc_mirror.render_mirror(doc, PROJECT, ACT)

    assert "# 01-计划书-共治行动" in text
    assert "> 类型：Word 文档" in text
    assert "> 原件：[[01-计划书-共治行动.docx]]" in text
    assert "共治行动第一期计划" in text
    assert "所属活动：[[_11-共治行动-第一期-资料索引|共治行动（第一期）]]" in text
    assert "所属项目：[[_测试项目-项目资料索引|测试项目]]" in text


def test_mirror_project_writes_once_and_adds_stignore(fake_root):
    act = fake_root / PROJECT / "活动" / ACT
    _docx(act / "01-计划书.docx")
    # 已有规则：新规则必须插在段落注释和规则之前
    (fake_root / PROJECT / ".stignore").write_text(
        "// 头注释\n\n// ---- 图片 ----\n**/*.jpg\n", encoding="utf-8")

    first = doc_mirror.mirror_project(PROJECT)
    assert first["errors"] == []
    assert first["written"] == [f"{PROJECT}/活动/{ACT}/01-计划书.md"]
    mirror = act / "01-计划书.md"
    assert mirror.is_file()
    assert "所属活动" in mirror.read_text(encoding="utf-8")

    second = doc_mirror.mirror_project(PROJECT)
    assert second["written"] == []
    assert second["skipped"] == 1

    ignore = (fake_root / PROJECT / ".stignore").read_text(encoding="utf-8")
    assert ignore.index("**/*.docx") < ignore.index("// ---- 图片 ----")
    assert ignore.index("// ---- 图片 ----") < ignore.index("**/*.jpg")
    assert "**/*.pdf" in ignore
    # 幂等：再跑不重复插块
    assert ignore.count(doc_mirror.STIGNORE_MARKER) == 1


def test_summary_failure_still_writes(fake_root, monkeypatch):
    doc = _docx(fake_root / PROJECT / "活动" / ACT / "01-计划书.docx")

    def boom(path):
        raise RuntimeError("转换失败")

    monkeypatch.setattr(doc_mirror._markdown, "file_to_md", boom)
    assert doc_mirror.write_mirror(doc, PROJECT, ACT) is True
    text = doc.with_suffix(".md").read_text(encoding="utf-8")
    assert "（无法提取正文）" in text


def test_missing_stignore_is_not_created(fake_root):
    """没有 .stignore 的项目不新建半套规则，避免照片等原有排除丢失。"""
    assert doc_mirror.ensure_stignore(fake_root / PROJECT) is False
    assert not (fake_root / PROJECT / ".stignore").exists()


def test_project_root_doc_has_no_activity_link(fake_root):
    doc = _docx(fake_root / PROJECT / "方案" / "申报书.docx", "项目申报书正文。")
    text = doc_mirror.render_mirror(doc, PROJECT)
    assert "所属活动" not in text
    assert "所属项目：[[_测试项目-项目资料索引|测试项目]]" in text
