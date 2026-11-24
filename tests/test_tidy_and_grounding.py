#!/usr/bin/env python3
"""自动整理（tidy）与冷化（grounding）的回归测试。"""
from __future__ import annotations

from pathlib import Path

from engine import checks, grounding, tidy


def _act(tmp_path: Path) -> Path:
    d = tmp_path / "01-测试活动"
    d.mkdir(parents=True)
    return d


def test_tidy_moves_misplaced_signed_photo(tmp_path):
    """签到表放错在 11- 目录里 → 建议移到根目录并改名 03-签到表.pdf。"""
    d = _act(tmp_path)
    (d / "11-活动照片资料").mkdir()
    (d / "11-活动照片资料" / "签到表.pdf").write_bytes(b"x")
    (d / "11-活动照片资料" / "a.jpg").write_bytes(b"x")

    plan = tidy.plan(d)
    acts = [a for a in plan["actions"] if a["item"] == 3]
    assert len(acts) == 1
    assert acts[0]["from"] == "11-活动照片资料/签到表.pdf"
    assert acts[0]["to"] == "03-签到表.pdf"


def test_tidy_apply_moves_file(tmp_path):
    d = _act(tmp_path)
    (d / "11-活动照片资料").mkdir()
    (d / "11-活动照片资料" / "签到表.pdf").write_bytes(b"pdf")
    plan = tidy.plan(d)
    result = tidy.apply(d, plan["actions"])
    assert result["moved"] >= 1 and not result["errors"]
    assert (d / "03-签到表.pdf").is_file()
    assert not (d / "11-活动照片资料" / "签到表.pdf").exists()


def test_tidy_skips_extra_sequence_numbers(tmp_path):
    """25- 这类补充编号已经规范，不该被改成 10-。

    2026-11-24 起「物资照片」只认照片类关键词（原来含「领取表」会误认领
    25-物资领取表 为物资照片），所以这里改成直接验证跳过逻辑本身。
    """
    d = _act(tmp_path)
    (d / "25-物资领取表-测试.docx").write_bytes(b"x")
    plan = tidy.plan(d)
    moved = [a for a in plan["actions"] if a["from"].startswith("25-")]
    assert not moved, "补充编号的文件不该被整理"


def test_tidy_does_not_split_photo_set(tmp_path):
    """成套扫描件不拆：同目录多份同类图片时整体不动。"""
    d = _act(tmp_path)
    folder = d / "04-满意度调查表资料"
    folder.mkdir()
    for i in range(3):
        (folder / f"问卷{i}.jpg").write_bytes(b"x")
    plan = tidy.plan(d)
    moved = [a for a in plan["actions"] if "问卷" in a["from"]]
    assert not moved, "成套问卷不该被单独搬走"


def test_tidy_keeps_files_in_numbered_dir(tmp_path):
    """已经在 03-签到表/ 这样的编号目录里，文件不需要再带序号。"""
    d = _act(tmp_path)
    folder = d / "03-签到表"
    folder.mkdir()
    (folder / "签到表.jpg").write_bytes(b"x")
    plan = tidy.plan(d)
    moved = [a for a in plan["actions"] if "签到表" in a["from"]]
    assert not moved, "编号目录内的文件不该被搬到根目录"


def test_tidy_leaves_canonical_alone(tmp_path):
    d = _act(tmp_path)
    (d / "03-签到表.pdf").write_bytes(b"x")
    plan = tidy.plan(d)
    assert not [a for a in plan["actions"] if a["item"] == 3]


def test_check_exposes_path(tmp_path):
    """check() 要带回命中路径，整理与排查都靠它。"""
    d = _act(tmp_path)
    (d / "06-总结报告.docx").write_bytes(b"x")
    item = next(i for i in checks.check(d)["items"] if i["no"] == 5)
    assert item["path"] == "06-总结报告.docx"


def test_grounding_reads_project_docs(tmp_path):
    """只读 方案/协议/报告，不读活动目录。"""
    proj = tmp_path / "项目"
    (proj / "方案").mkdir(parents=True)
    (proj / "活动").mkdir()
    (proj / "方案" / "申报书.md").write_text("项目背景：微网格治理", encoding="utf-8")
    (proj / "活动" / "x.md").write_text("活动材料不该进冷化", encoding="utf-8")

    docs = grounding.project_documents(proj)
    assert len(docs) == 1
    assert "微网格治理" in docs[0]["text"]
    assert "活动材料" not in docs[0]["text"]


def test_grounding_render_empty_when_no_material():
    assert grounding.render_prompt_block({"docs": [], "kb": ""}) == ""


def test_grounding_render_includes_docs():
    block = grounding.render_prompt_block(
        {"docs": [{"name": "方案/协议.md", "text": "服务对象为社区老人"}], "kb": ""})
    assert "方案/协议.md" in block
    assert "服务对象为社区老人" in block
    assert "不要编造" in block


def test_grounding_summarize():
    assert grounding.summarize([], "") == "无可用冷化材料"
    assert "1 份" in grounding.summarize([{"name": "a", "text": "x"}], "")
