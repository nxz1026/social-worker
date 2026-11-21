"""C1：B2 三项（报销校验 / 图注 / 满意度）接进 Web 的 8 个端点。

沿用 test_api_web.py 的做法：直接调用端点函数，不依赖 httpx / TestClient；
LLM、真实模板库、真实资料目录全部用 tmp_path + monkeypatch 替换。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import openpyxl
import pytest
import yaml
from docx import Document

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402

ADMIN = {"id": 1, "username": "admin", "role": "admin",
         "projects": {"huimin": "write", "huadu": "write"}}
VIEWER = {"id": 3, "username": "viewer", "role": "viewer", "projects": {"huimin": "readonly"}}

ACT = "11-共治行动-第一期"


@pytest.fixture(autouse=True)
def _silent_audit(monkeypatch):
    """本工单的端点不写审计，这里只是兜底，防止别的依赖落库。"""
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """一个项目 + 活动目录，模板库也落在同一个 tmp ROOT 下。"""
    root = tmp_path / "projects"
    project = root / "惠民项目"
    (project / "活动").mkdir(parents=True)
    (project / "惠民项目.yaml").write_text(
        yaml.safe_dump({"project": {"short": "huimin", "code_rules": {}}},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    monkeypatch.setattr(app._projects, "PROJECTS_ROOT", root)
    monkeypatch.setattr(app, "ROOT", tmp_path)
    monkeypatch.setattr("api.state.ROOT", tmp_path)
    (tmp_path / "templates" / "library").mkdir(parents=True)
    return root


def _activity(projects_root: Path, dirname: str = ACT) -> Path:
    """活动目录：activity.yaml + 发票 / 问卷 / 照片三个子目录。"""
    act = projects_root / "惠民项目" / "活动" / dirname
    (act / "12-发票").mkdir(parents=True)
    (act / "04-满意度调查表资料").mkdir()
    (act / "11-活动照片资料").mkdir()
    (act / "activity.yaml").write_text(
        yaml.safe_dump({"code": "CDYJR2026008-C-1110002", "status": "imported"},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return act


def _invoice_set(act: Path) -> None:
    """造三张规范命名的发票 + 决算 1662 + 报销 1662。"""
    for name in (
        "2026-11-13_增值税发票_300元_刘佳_讲课费_26512000005080500076.pdf",
        "2026-11-13_电子发票_1190元_温江三圆广告制作部_26512000005078121196.pdf",
        "2026-11-13_电子发票_172元_美诗韵商贸部_劳保手套_26512000005086188106.pdf",
    ):
        (act / "12-发票" / name).write_bytes(b"")   # 文件名解析，不读正文

    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "物品名称"
    table.cell(0, 1).text = "金额（元）"
    table.cell(1, 0).text = "合计"
    table.cell(1, 1).text = "1662（元）"
    doc.save(str(act / "08-决算表.docx"))

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["A1"] = "费用报销单"
    sheet["A2"] = "合计金额"
    sheet["C2"] = 1662
    workbook.save(str(act / "21-报销单.xlsx"))


def _report_template(projects_root: Path) -> Path:
    """发票明细表模板：表头 + 20 行数据行（与线上模板同结构）。"""
    path = projects_root.parent / "templates" / "library" / "发票明细表.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for col, title in enumerate(
            ("序号", "发票日期", "发票号码", "商户名称", "项目内容", "金额（元）", "备注"),
            start=1):
        sheet.cell(row=1, column=col, value=title)
    workbook.save(str(path))
    return path


def _survey_photos(act: Path, count: int = 3) -> Path:
    folder = act / "04-满意度调查表资料"
    for index in range(1, count + 1):
        (folder / f"微信图片_202611151932{index:02d}_15{index}_107.jpg").write_bytes(b"")
    return folder


# ---------------- 一、报销校验 ----------------

def test_invoice_scan_returns_three_way_check(projects_root):
    act = _activity(projects_root)
    _invoice_set(act)

    out = app.api_invoice_scan("惠民项目", ACT, ADMIN)

    assert out["决算"]["value"] == 1662
    assert out["报销"]["value"] == 1662
    assert out["发票"]["value"] == 1662
    assert out["发票"]["count"] == 3
    assert out["consistent"] is True
    assert out["diff"] == {"决算-报销": 0, "报销-发票": 0}
    assert out["issues"] == []


def test_invoice_scan_viewer_403(projects_root):
    _activity(projects_root)
    with pytest.raises(app.HTTPException) as exc:
        app.api_invoice_scan("惠民项目", ACT, VIEWER)
    assert exc.value.status_code == 403


def test_invoice_report_writes_detail_sheet(projects_root):
    act = _activity(projects_root)
    _invoice_set(act)
    _report_template(projects_root)

    out = app.api_invoice_report("惠民项目", ACT, {}, ADMIN)

    assert out["count"] == 3
    assert out["total"] == 1662
    assert out["path"] == "12-发票/发票明细表.xlsx"
    sheet = openpyxl.load_workbook(str(act / out["path"])).active
    assert sheet.cell(row=2, column=2).value == "2026-11-13"
    assert sheet.cell(row=2, column=4).value == "刘佳"
    assert sheet.cell(row=2, column=6).value == 300


def test_invoice_report_custom_output_and_traversal(projects_root):
    act = _activity(projects_root)
    _invoice_set(act)
    _report_template(projects_root)

    out = app.api_invoice_report(
        "惠民项目", ACT, {"output": "12-发票/明细-自定义.xlsx"}, ADMIN)
    assert (act / out["path"]).is_file()

    with pytest.raises(app.HTTPException) as exc:
        app.api_invoice_report("惠民项目", ACT, {"output": "../逃逸.xlsx"}, ADMIN)
    assert exc.value.status_code == 400


# ---------------- 二、图注 ----------------

def test_photos_lists_images_and_marks_default(projects_root):
    act = _activity(projects_root)
    folder = act / "11-活动照片资料"
    (folder / "全景-01.jpg").write_bytes(b"img")
    (folder / "微信图片_001.jpg").write_bytes(b"img")
    (folder / "说明.txt").write_text("不是图片", encoding="utf-8")

    out = app.api_activity_photos("惠民项目", ACT, user=ADMIN)

    assert [item["name"] for item in out] == ["全景-01.jpg", "微信图片_001.jpg"]
    by_name = {item["name"]: item for item in out}
    assert by_name["全景-01.jpg"]["picked"] is True     # 命中「全景」
    assert by_name["微信图片_001.jpg"]["picked"] is True  # 不足 8 张，全部默认选中
    assert by_name["全景-01.jpg"]["size"] == 3
    assert "mtime" in by_name["全景-01.jpg"]
    # 空文件不是合法图片，缩略图降级为空串而不是 500
    assert by_name["全景-01.jpg"]["thumb"] == ""


def test_photos_missing_subdir_returns_empty(projects_root):
    _activity(projects_root)
    assert app.api_activity_photos("惠民项目", ACT, "99-不存在", ADMIN) == []


def test_captions_use_picked_files(projects_root, monkeypatch):
    act = _activity(projects_root)
    folder = act / "11-活动照片资料"
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        (folder / name).write_bytes(b"img")
    seen = {}

    def fake_write(photos, facts, *, style=""):
        seen["photos"] = [p.name for p in photos]
        seen["style"] = style
        seen["facts"] = facts
        return [{"file": p.name, "caption": f"图{i} 现场"}
                for i, p in enumerate(photos, start=1)]

    monkeypatch.setattr(app._caption, "write_captions", fake_write)

    out = app.api_activity_captions(
        "惠民项目", ACT, {"sub": "11-活动照片资料", "style": "务实",
                          "picked": ["c.jpg", "a.jpg"]}, ADMIN)

    assert [item["file"] for item in out] == ["c.jpg", "a.jpg"]
    assert seen["photos"] == ["c.jpg", "a.jpg"]
    assert seen["style"] == "务实"
    assert seen["facts"]["项目编号"] == "CDYJR2026008-C-1110002"


def test_captions_empty_picked_falls_back_to_program(projects_root, monkeypatch):
    act = _activity(projects_root)
    folder = act / "11-活动照片资料"
    (folder / "微信图片_001.jpg").write_bytes(b"img")
    monkeypatch.setattr(app._caption, "write_captions",
                        lambda photos, facts, *, style="": [
                            {"file": p.name, "caption": "图1 活动现场"} for p in photos])

    out = app.api_activity_captions("惠民项目", ACT, {"picked": []}, ADMIN)

    assert out == [{"file": "微信图片_001.jpg", "caption": "图1 活动现场"}]


def test_captions_rejects_unknown_picked(projects_root, monkeypatch):
    _activity(projects_root)
    monkeypatch.setattr(app._caption, "write_captions",
                        lambda *a, **k: pytest.fail("不该调 LLM"))
    with pytest.raises(app.HTTPException) as exc:
        app.api_activity_captions("惠民项目", ACT, {"picked": ["没有.jpg"]}, ADMIN)
    assert exc.value.status_code == 400


def test_captions_fill_writes_template(projects_root):
    act = _activity(projects_root)
    lib = projects_root.parent / "templates" / "library"
    doc = Document()
    doc.add_paragraph("{{图注_1}}")
    doc.add_paragraph("{{图注_2}}")
    doc.save(str(lib / "简报.docx"))

    out = app.api_captions_fill(
        "惠民项目", ACT,
        {"template": "简报",
         "captions": [{"file": "a.jpg", "caption": "图1 活动现场"},
                      {"file": "b.jpg", "caption": "图2 居民签到"}]},
        ADMIN)

    assert out["path"] == "简报.docx"
    assert out["filled"] == 2
    text = "\n".join(p.text for p in Document(str(act / "简报.docx")).paragraphs)
    assert "图1 活动现场" in text and "图2 居民签到" in text


def test_captions_fill_empty_400(projects_root):
    _activity(projects_root)
    with pytest.raises(app.HTTPException) as exc:
        app.api_captions_fill("惠民项目", ACT, {"template": "简报", "captions": []},
                              ADMIN)
    assert exc.value.status_code == 400


# ---------------- 三、满意度 ----------------

def test_sat_list_creates_scores_and_keeps_existing(projects_root):
    act = _activity(projects_root)
    folder = _survey_photos(act, count=2)
    (folder / "scores.json").write_text(json.dumps(
        [{"file": "微信图片_20261115193201_151_107.jpg", "score": 92,
          "satisfied": True}], ensure_ascii=False), encoding="utf-8")

    (folder / "微信图片_20261115193203_153_107.jpg").write_bytes(b"")
    out = app.api_sat_list("惠民项目", ACT, ADMIN)

    assert out[0]["score"] == 92 and out[0]["satisfied"] is True
    assert out[-1] == {"file": "微信图片_20261115193203_153_107.jpg",
                       "score": None, "satisfied": None}
    saved = json.loads((folder / "scores.json").read_text(encoding="utf-8"))
    assert len(saved) == 3


def test_sat_get_returns_saved_scores(projects_root):
    act = _activity(projects_root)
    _survey_photos(act, count=1)
    app.api_sat_list("惠民项目", ACT, ADMIN)

    out = app.api_sat_get("惠民项目", ACT, ADMIN)
    assert out == [{"file": "微信图片_20261115193201_151_107.jpg",
                    "score": None, "satisfied": None}]


def test_sat_get_without_file_returns_empty(projects_root):
    _activity(projects_root)
    assert app.api_sat_get("惠民项目", ACT, ADMIN) == []


def test_sat_put_writes_scores(projects_root):
    act = _activity(projects_root)
    _survey_photos(act, count=2)

    out = app.api_sat_put("惠民项目", ACT, {"scores": [
        {"file": "微信图片_20261115193201_151_107.jpg", "score": 91,
         "satisfied": True},
        {"file": "微信图片_20261115193202_152_107.jpg", "score": 88,
         "satisfied": False},
    ]}, ADMIN)

    assert out["count"] == 2
    saved = json.loads((act / out["path"]).read_text(encoding="utf-8"))
    assert saved[1] == {"file": "微信图片_20261115193202_152_107.jpg",
                        "score": 88, "satisfied": False}


def test_sat_put_rejects_non_numeric_score(projects_root):
    _activity(projects_root)
    with pytest.raises(app.HTTPException) as exc:
        app.api_sat_put("惠民项目", ACT, {"scores": [
            {"file": "a.jpg", "score": "九十"}]}, ADMIN)
    assert exc.value.status_code == 400


def test_sat_fill_writes_summary(projects_root, monkeypatch):
    act = _activity(projects_root)
    _survey_photos(act, count=3)
    app.api_sat_put("惠民项目", ACT, {"scores": [
        {"file": "p1.jpg", "score": 90, "satisfied": True},
        {"file": "p2.jpg", "score": 92, "satisfied": True},
        {"file": "p3.jpg", "score": 94, "satisfied": True},
    ]}, ADMIN)

    lib = projects_root.parent / "templates" / "library"
    doc = Document()
    doc.add_paragraph("{{项目名称}}{{活动名称}}")
    doc.add_paragraph("测评结果{{评定等级}}，平均{{平均分}}分")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "测评人数"
    table.cell(0, 1).text = "总得分"
    table.cell(1, 0).text = "{{测评人数}}"
    table.cell(1, 1).text = "{{总得分}}"
    doc.save(str(lib / "满意度测评汇总表.docx"))

    from api.shared.domain import script
    sat = script("satisfaction")
    monkeypatch.setattr(sat, "_facts", lambda act_dir: {
        "项目名称": "惠民项目", "活动名称": "共治行动（第一期）",
        "活动时间": "2026年11月10日", "活动地点": "民乐北巷",
        "项目社工": "社工甲", "实际人数": 19, "年月日": "2026年11月19日",
    })

    out = app.api_sat_fill("惠民项目", ACT, ADMIN)

    assert out["份数"] == 3
    assert out["总得分"] == 276
    assert out["平均分"] == 92
    assert out["满意率"] == "100%（3/3）"
    assert out["评定等级"] == "优秀"
    assert out["path"] == sat.SUMMARY_NAME
    text = "\n".join(p.text for p in Document(str(act / out["path"])).paragraphs)
    assert "惠民项目共治行动（第一期）" in text
    assert "优秀" in text


def test_sat_fill_without_scores_400(projects_root):
    _activity(projects_root)
    with pytest.raises(app.HTTPException) as exc:
        app.api_sat_fill("惠民项目", ACT, ADMIN)
    assert exc.value.status_code == 400
    assert "先点" in exc.value.detail


def test_sat_fill_without_recorded_scores_400(projects_root):
    act = _activity(projects_root)
    _survey_photos(act, count=2)
    app.api_sat_list("惠民项目", ACT, ADMIN)

    with pytest.raises(app.HTTPException) as exc:
        app.api_sat_fill("惠民项目", ACT, ADMIN)
    assert exc.value.status_code == 400
