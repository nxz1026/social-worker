"""scripts/satisfaction.py：分数录入清单、统计口径与汇总表填充。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts import satisfaction  # noqa: E402


def _make_photos(act_dir: Path, count: int = 10) -> Path:
    """造问卷照片目录。"""
    folder = act_dir / satisfaction.SURVEY_SUBDIR
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (folder / f"微信图片_202611151932{index:02d}_15{index}_107.jpg").write_bytes(b"")
    return folder


def _entries(scores: list, satisfied: list | None = None) -> list[dict]:
    """按分数造 scores.json 内容。"""
    out: list[dict] = []
    for index, score in enumerate(scores, start=1):
        entry = {"file": f"p{index}.jpg", "score": score}
        if satisfied is not None:
            entry["satisfied"] = satisfied[index - 1]
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# 评定等级
# --------------------------------------------------------------------------

def test_grade_boundaries():
    """口径照模板脚注：优秀 90-100、良好 80-89、一般 70-79、合格 60-69、不合格 <60。"""
    assert satisfaction.grade(100) == "优秀"
    assert satisfaction.grade(90) == "优秀"
    assert satisfaction.grade(89.99) == "良好"
    assert satisfaction.grade(80) == "良好"
    assert satisfaction.grade(79.99) == "一般"
    assert satisfaction.grade(70) == "一般"
    assert satisfaction.grade(69.99) == "合格"
    assert satisfaction.grade(60) == "合格"
    assert satisfaction.grade(59.99) == "不合格"
    assert satisfaction.grade(None) == ""


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------

def test_summarize_matches_real_data():
    """真实数据：10 份、合计 914、平均 91.40、满意率 100%、优秀。"""
    scores = [91, 92, 90, 93, 91, 90, 92, 91, 92, 92]

    stats = satisfaction.summarize(_entries(scores))

    assert stats["份数"] == 10
    assert stats["总得分"] == 914
    assert stats["平均分"] == 91.4
    assert stats["满意率"] == "100%（10/10）"
    assert stats["评定等级"] == "优秀"
    assert stats["issues"] == []


def test_summarize_skips_empty_scores():
    stats = satisfaction.summarize(_entries([90, None, 80]))

    assert stats["份数"] == 2
    assert stats["总得分"] == 170
    assert stats["平均分"] == 85
    assert any("1 条未录分数" in issue for issue in stats["issues"])


def test_summarize_explicit_satisfied_wins():
    """显式 satisfied=False 的份数不计入满意率，即使分数很高。"""
    stats = satisfaction.summarize(_entries([95, 95, 95, 95],
                                            satisfied=[True, True, True, False]))

    assert stats["满意率"] == "75%（3/4）"
    assert stats["平均分"] == 95
    assert stats["评定等级"] == "优秀"


def test_summarize_default_satisfied_uses_score_band():
    """未录 satisfied 时按 ≥80 推定三题均满意。"""
    stats = satisfaction.summarize(_entries([90, 85, 79]))

    assert stats["满意率"] == "67%（2/3）"


def test_summarize_no_scores():
    stats = satisfaction.summarize(_entries([None, None]))

    assert stats["份数"] == 0
    assert stats["总得分"] is None
    assert stats["评定等级"] == ""


# --------------------------------------------------------------------------
# scores.json
# --------------------------------------------------------------------------

def test_list_photos_only_images(tmp_path: Path):
    act_dir = tmp_path / "活动"
    folder = _make_photos(act_dir, count=2)
    (folder / "04-满意度调查表回收清单.docx").write_bytes(b"")

    photos = satisfaction.list_photos(act_dir)

    assert [path.name for path in photos] == [
        "微信图片_20261115193201_151_107.jpg",
        "微信图片_20261115193202_152_107.jpg",
    ]


def test_build_scores_keeps_recorded_scores(tmp_path: Path):
    """重跑 --list 不覆盖已录分数，新照片补在末尾。"""
    act_dir = tmp_path / "活动"
    folder = _make_photos(act_dir, count=2)
    path = satisfaction.scores_path(act_dir)
    path.write_text(json.dumps(
        [{"file": "微信图片_20261115193201_151_107.jpg", "score": 92, "satisfied": True}],
        ensure_ascii=False), encoding="utf-8")

    (folder / "微信图片_20261115193203_153_107.jpg").write_bytes(b"")
    entries, _ = satisfaction.write_scores(act_dir)

    assert entries[0]["score"] == 92
    assert entries[0]["satisfied"] is True
    assert entries[-1] == {"file": "微信图片_20261115193203_153_107.jpg",
                           "score": None, "satisfied": None}


def test_write_scores_creates_json(tmp_path: Path):
    act_dir = tmp_path / "活动"
    _make_photos(act_dir, count=3)

    entries, path = satisfaction.write_scores(act_dir)

    assert path.is_file()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) == 3
    assert all(item["score"] is None for item in saved)


# --------------------------------------------------------------------------
# 填汇总表
# --------------------------------------------------------------------------

def _template(path: Path) -> Path:
    """造一份带占位符的汇总表模板。"""
    doc = Document()
    doc.add_paragraph("{{项目名称}}{{活动名称}}")
    doc.add_paragraph("{{活动时间}}｜{{活动地点}}｜{{项目社工}}")
    table = doc.add_table(rows=2, cols=4)
    for index, text in enumerate(("参与人数", "接受测评人数", "总得分", "满意率")):
        table.cell(0, index).text = text
    for index, text in enumerate(("{{实际人数}}", "{{测评人数}}",
                                  "{{总得分}}", "{{满意率}}")):
        table.cell(1, index).text = text
    doc.add_paragraph("结论：本次活动满意度测评得分为{{平均分}}分，测评结果为{{评定等级}}")
    doc.add_paragraph("{{年月日}}")
    doc.save(str(path))
    return path


def test_fill_writes_summary_docx(tmp_path: Path, monkeypatch):
    act_dir = tmp_path / "项目" / "活动" / "11-共治行动-第一期"
    _make_photos(act_dir, count=3)
    satisfaction.write_scores(act_dir)
    path = satisfaction.scores_path(act_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry, score in zip(data, (90, 92, 94)):
        entry["score"] = score
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    template = _template(tmp_path / "汇总模板.docx")
    monkeypatch.setattr(satisfaction, "SUMMARY_TEMPLATE", template)
    monkeypatch.setattr(satisfaction, "_facts", lambda act: {
        "项目名称": "某项目", "活动名称": "共治行动（第一期）",
        "活动时间": "2026年11月10日", "活动地点": "民乐北巷",
        "项目社工": "聂聃", "实际人数": 19, "年月日": "2026年11月19日",
    })

    out = tmp_path / "汇总.docx"
    code = satisfaction.main(["--activity", str(act_dir), "--fill", "--out", str(out)])

    assert code == 0
    text = "\n".join(par.text for par in Document(str(out)).paragraphs)
    assert "某项目共治行动（第一期）" in text
    assert "结论：本次活动满意度测评得分为92.00分，测评结果为优秀" in text
    table = Document(str(out)).tables[0]
    assert table.cell(1, 1).text == "3"
    assert table.cell(1, 2).text == "276 分"
    assert table.cell(1, 3).text == "100%（3/3）"


def test_fill_without_scores_file(tmp_path: Path, capsys):
    act_dir = tmp_path / "活动"
    _make_photos(act_dir, count=1)

    code = satisfaction.main(["--activity", str(act_dir), "--fill"])

    assert code == 1
    assert "先跑 --list" in capsys.readouterr().err


def test_list_cli_prints_and_writes(tmp_path: Path, capsys):
    act_dir = tmp_path / "活动"
    _make_photos(act_dir, count=2)

    code = satisfaction.main(["--activity", str(act_dir), "--list"])

    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == [
        {"file": "微信图片_20261115193201_151_107.jpg", "score": None, "satisfied": None},
        {"file": "微信图片_20261115193202_152_107.jpg", "score": None, "satisfied": None},
    ]
    assert satisfaction.scores_path(act_dir).is_file()
