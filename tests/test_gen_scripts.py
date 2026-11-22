"""生成脚本纯逻辑补测：gen_brief / gen_plan / fill_plan_from_xlsx。

约束（工单 A）：
  - 只测纯逻辑，不连真实 LLM、不读真实活动目录，一律用 tmp_path 造数据
  - LLM 相关调用全部 monkeypatch 掉
  - 不碰 /srv/social-work：模板也现造一份临时 docx
  - 发现源码问题只记录在测试注释与报告里，不改源码
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from docx import Document
from openpyxl import Workbook

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import fill_plan_from_xlsx  # noqa: E402
import gen_brief  # noqa: E402
import gen_plan  # noqa: E402
from engine import plan_sheet, projects  # noqa: E402


# --------------------------------------------------------------------------
# 造数工具
# --------------------------------------------------------------------------

def _write_docx(path: Path, paragraphs=(), tables=()) -> Path:
    """按给定顺序写段落与表格；表格用二维元组列表表示。"""
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    for rows in tables:
        table = doc.add_table(rows=len(rows), cols=len(rows[0]))
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                table.cell(row_index, col_index).text = str(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _docx_text(path: Path) -> str:
    """读回 docx 的正文段落与表格文本，用于断言填充结果。"""
    doc = Document(str(path))
    parts = [para.text for para in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _write_project(project_dir: Path, name: str = '"邻里共治"街区活力营建项目') -> Path:
    """写一份项目 yaml（含指标、预算、staff），用于项目名/背景/预算上限。"""
    project_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "project": {
            "name": name,
            "category": "社区保障资金",
            "level": "市级",
            "period": {"start": "2026-01", "end": "2026-12", "months": 12},
        },
        "budget": {"total": 120000},
        "staff": {
            "leader": {"name": "李四"},
            "social_worker": {"name": "张三"},
        },
        "indicators": [
            {"name": "共治行动", "target": "2场", "budget": 20000, "count": 2},
        ],
    }
    path = project_dir / f"{project_dir.name}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def _write_plan_docx(path: Path, *, name: str = "邻里共治行动",
                     time: str = "2026-11-10（上午）", place: str = "民乐北巷",
                     target: str = "社区居民", expected: str = "20",
                     purpose: str = "清理通道，改善环境") -> Path:
    """造 01-活动计划书：标签字段 + 目的/目标段落 + 环节表。"""
    return _write_docx(
        path,
        paragraphs=[
            f"活动名称：{name}",
            f"活动时间：{time}",
            f"活动地点：{place}",
            f"服务对象：{target}",
            f"预计参与人数：{expected}",
            "问题现状：通道堆放杂物",
            "需求来源：居民反映",
            "回应思路：协商共治",
            "活动目的",
            purpose,
            "活动目标",
            "①提升居民参与意识；",
            "②形成长效机制。",
        ],
        tables=[[
            ("阶段", "时间", "内容", "所需物资"),
            ("准备阶段", "9:00-9:30", "签到、宣读公约", "签到表"),
            ("执行阶段", "9:30-11:00", "分组清理通道", "手套、垃圾袋"),
        ]],
    )


def _write_report_docx(path: Path, count: str = "19") -> Path:
    """造 06-总结报告：表格里的「人数 | 19人」。"""
    return _write_docx(path, tables=[[("人数", f"{count}人")]])


def _write_budget_docx(path: Path, total: str = "1662") -> Path:
    """造 08-决算表：表格里的「合计 | 1662（元）」。"""
    return _write_docx(path, tables=[[("项目", "金额"), ("合计", f"{total}（元）")]])


def _write_plan_template(path: Path) -> Path:
    """造一份只含计划书占位符的迷你模板（替代 /srv 的真实模板）。"""
    return _write_docx(path, paragraphs=[
        "{{项目名称}}", "{{活动名称}}",
        "{{活动背景内容}}", "{{环节内容_1}}", "{{环节物资_1}}", "{{环节人员_1}}",
        "{{预计困难_1}}", "{{解决方案_1}}",
        "{{物资名称_1}}", "{{物资规格_1}}", "{{物资单价_1}}", "{{物资小计_1}}",
        "{{合计金额}}",
    ])


def _make_xlsx(path: Path, *, steps=(), difficulties=(), materials=()) -> Path:
    """按 plan_sheet 约定的三张表造一份填写表。"""
    workbook = Workbook()
    step = workbook.active
    step.title = plan_sheet.SHEET_STEP
    step.append(list(plan_sheet.STEP_FIELDS))
    for row in steps:
        step.append(list(row))

    difficulty = workbook.create_sheet(plan_sheet.SHEET_DIFFICULTY)
    difficulty.append(list(plan_sheet.DIFFICULTY_FIELDS))
    for row in difficulties:
        difficulty.append(list(row))

    material = workbook.create_sheet(plan_sheet.SHEET_MATERIAL)
    material.append(list(plan_sheet.MATERIAL_FIELDS))
    for row in materials:
        material.append(list(row))

    workbook.save(str(path))
    workbook.close()
    return path


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch) -> Path:
    """把 projects.PROJECTS_ROOT 指到临时目录，避免读 /srv。"""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(projects, "PROJECTS_ROOT", root)
    return root


@pytest.fixture
def activity_dir(project_root: Path) -> Path:
    """标准活动目录：<项目>/活动/<活动>，保证 _project_name 能反推出项目名。"""
    act = project_root / "测试项目" / "活动" / "01-共治行动-第一期"
    act.mkdir(parents=True)
    return act


# ==========================================================================
# 一、gen_brief.py：纯解析函数
# ==========================================================================

@pytest.mark.parametrize("md,expected", [
    ("活动名称：邻里共治行动", "邻里共治行动"),
    ("活动名称: 带半角冒号", "带半角冒号"),
    ("  活动地点 ： 民乐北巷  ", "民乐北巷"),
    ("预计人数：20", ""),                 # 标签不匹配
    ("", ""),
])
def test_grab_label_variants(md, expected):
    """_grab 只认「标签：值」整行，半角/全角冒号与前后空白都要兼容。"""
    assert gen_brief._grab(md, "活动名称", "活动地点") == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026-11-10（上午）", "2026年11月10日（上午）"),
    ("2026-1-5", "2026年1月5日"),
    ("2026/11/10", "2026/11/10"),          # 非 ISO 格式原样返回
    ("待定", "待定"),
    ("", ""),
])
def test_cn_date(raw, expected):
    """_cn_date 只转 ISO 日期，其余原样返回。"""
    assert gen_brief._cn_date(raw) == expected


def test_line_after_returns_first_non_empty_line():
    """活动目的这类独占一段的字段取标题后的第一个非空行。"""
    md = "前言\n\n活动目的\n\n\n清理通道，改善环境\n后续文字"
    assert gen_brief._line_after(md, "活动目的") == "清理通道，改善环境"
    assert gen_brief._line_after(md, "不存在的标题") == ""


def test_parse_goals_stops_at_non_numbered_line():
    """活动目标只收连续的 ①② 列表项，遇到普通段落即结束。"""
    md = "活动目标\n\n①提升意识；\n②形成机制。\n\n下一步：继续跟踪"
    assert gen_brief._parse_goals(md) == ["提升意识", "形成机制"]
    assert gen_brief._parse_goals("没有这个标题") == []


def test_parse_steps_reads_third_column():
    """环节取表格第三列，跳过表头与 markdown 分隔行。"""
    md = ("| 阶段 | 时间 | 内容 | 所需物资 |\n"
          "| --- | --- | --- | --- |\n"
          "| 准备 | 9:00 | 签到、宣读公约 | 签到表 |\n"
          "| 执行 | 9:30 | 分组清理通道 | 手套 |")
    assert gen_brief._parse_steps(md) == ["签到、宣读公约", "分组清理通道"]
    assert gen_brief._parse_steps("没有表格") == []


def test_parse_steps_drops_br_without_separator():
    """现状记录：单元格里的 <br> 被直接删掉，两段文字会粘在一起。

    源码 `re.sub(r"<br\\s*/?>", "", cells[2])` 没有补分隔符，
    docx 单元格两段文字会变成「分组清理通道」而不是「分组清理 通道」。
    这里固定当前行为，若以后修成加分隔符，本用例会失败提醒同步。
    """
    md = "| 阶段 | 时间 | 内容 |\n| --- | --- | --- |\n| 执行 | 9:30 | 分组清理<br>通道 |"
    assert gen_brief._parse_steps(md) == ["分组清理通道"]


def test_parse_actual_count_and_total_amount():
    """总结报告的人数、决算表的合计都从 markdown 表格里取。"""
    assert gen_brief._parse_actual_count("| 人数 | 19人 |") == 19
    assert gen_brief._parse_actual_count("| 人数 | 19 |") == 19
    assert gen_brief._parse_actual_count("人数：19人") is None

    assert gen_brief._parse_total_amount("| 合计 | 1662（元） |") == 1662.0
    assert gen_brief._parse_total_amount("| 合计 | 1662.50 |") == 1662.5
    assert gen_brief._parse_total_amount("| 合计 | 待定 |") is None


def test_clean_item_strips_trailing_punctuation():
    """列表项末尾的；;。要去掉，避免拼出「A；、B；」。"""
    assert gen_brief._clean_item("提升意识；") == "提升意识"
    assert gen_brief._clean_item(" 形成机制。 ") == "形成机制"
    assert gen_brief._clean_item("带分号;") == "带分号"


# ==========================================================================
# 二、gen_brief.py：build_facts / render_brief / main
# ==========================================================================

def test_build_facts_full_activity(activity_dir: Path, project_root: Path):
    """活动目录里有 01/06/08 三份 docx 时，facts 的每个字段都能解析出来。"""
    _write_project(project_root / "测试项目")
    _write_plan_docx(activity_dir / "01-活动计划书.docx")
    _write_report_docx(activity_dir / "06-总结报告.docx")
    _write_budget_docx(activity_dir / "08-决算表.docx")

    facts = gen_brief.build_facts(activity_dir)

    assert list(facts) == list(gen_brief.FACT_KEYS)
    assert facts["活动名称"] == "邻里共治行动"
    assert facts["活动时间"] == "2026年11月10日（上午）"
    assert facts["活动地点"] == "民乐北巷"
    assert facts["服务对象"] == "社区居民"
    assert facts["预计人数"] == 20                 # 数字串转 int
    assert facts["实际人数"] == 19
    assert facts["问题现状"] == "通道堆放杂物"
    assert facts["需求来源"] == "居民反映"
    assert facts["回应思路"] == "协商共治"
    assert facts["活动目的"] == "清理通道，改善环境"
    assert facts["活动目标"] == ["提升居民参与意识", "形成长效机制"]
    assert facts["环节"] == ["签到、宣读公约", "分组清理通道"]
    assert facts["决算金额"] == 1662.0
    assert facts["项目名称"] == '"邻里共治"街区活力营建项目'


def test_build_facts_empty_activity_dir(activity_dir: Path):
    """空活动目录不崩：key 齐全，缺失字段留空串 / None / 空列表。"""
    facts = gen_brief.build_facts(activity_dir)

    assert list(facts) == list(gen_brief.FACT_KEYS)
    assert facts["活动名称"] == ""
    assert facts["活动时间"] == ""
    assert facts["预计人数"] == ""
    assert facts["实际人数"] is None
    assert facts["决算金额"] is None
    assert facts["活动目标"] == []
    assert facts["环节"] == []
    # 项目 yaml 不存在时退回目录名
    assert facts["项目名称"] == "测试项目"


def test_build_facts_warns_missing_fields(activity_dir: Path, capsys):
    """缺字段只告警不中断，告警写到 stderr。"""
    gen_brief.build_facts(activity_dir)
    captured = capsys.readouterr()
    assert "警告：未解析到" in captured.err
    assert "活动名称" in captured.err


def test_resolve_activity_accepts_paths_and_shorthand(activity_dir: Path, project_root: Path):
    """绝对路径、项目名/活动名、项目名/活动/活动名 三种写法都能定位。"""
    assert gen_brief._resolve_activity(str(activity_dir)) == activity_dir
    assert gen_brief._resolve_activity("测试项目/01-共治行动-第一期") == activity_dir
    assert gen_brief._resolve_activity("测试项目/活动/01-共治行动-第一期") == activity_dir
    assert gen_brief._resolve_activity("不存在的项目/不存在") is None


def test_project_title_strips_outer_quotes_only(project_root: Path):
    """项目 yaml 的 name 只剥掉整串包住的引号，不动中间的引号。"""
    _write_project(project_root / "测试项目", name='"整串引号项目"')
    assert gen_brief._project_title("测试项目") == "整串引号项目"
    # 目录不存在时退回目录名
    assert gen_brief._project_title("没有这个项目") == "没有这个项目"
    assert gen_brief._project_title("") == ""


def test_render_brief_fills_all_slots(tmp_path: Path, monkeypatch):
    """render_brief 把 facts 与 result 填进模板的 6 个位置。"""
    template = _write_docx(tmp_path / "简报.docx", paragraphs=[
        "{{项目名称}}", "{{年月日}}", "{{活动名称}}",
        "{{简报导语}}", "{{诉求描述}}", "{{达到目标}}",
    ])
    monkeypatch.setattr(gen_brief, "BRIEF_TEMPLATE", template)

    facts = {"项目名称": "测试项目", "活动时间": "2026年11月10日",
             "活动名称": "邻里共治行动"}
    result = {"导语": "导语内容", "目的": "目的内容",
              "过程": "过程内容", "成效": "成效内容"}
    out = tmp_path / "输出" / "简报-已填.docx"

    gen_brief.render_brief(facts, result, out)

    assert out.is_file()
    text = _docx_text(out)
    assert "测试项目" in text
    assert "2026年11月10日" in text
    assert "邻里共治行动" in text
    assert "导语内容" in text
    # 诉求描述 = 目的 + 换行 + 过程
    assert "目的内容" in text and "过程内容" in text
    assert "成效内容" in text


def test_render_brief_tolerates_minimal_facts(tmp_path: Path, monkeypatch):
    """最简 facts（空 dict）不抛异常，缺失位置填空串。"""
    template = _write_docx(tmp_path / "简报.docx", paragraphs=[
        "{{项目名称}}", "{{年月日}}", "{{活动名称}}",
        "{{简报导语}}", "{{诉求描述}}", "{{达到目标}}",
    ])
    monkeypatch.setattr(gen_brief, "BRIEF_TEMPLATE", template)

    out = tmp_path / "简报-已填.docx"
    result = {"导语": "只有导语", "目的": "", "过程": "", "成效": ""}
    gen_brief.render_brief({}, result, out)

    assert out.is_file()
    assert "只有导语" in _docx_text(out)


def test_render_brief_missing_template_raises(tmp_path: Path, monkeypatch):
    """模板不存在时明确抛 FileNotFoundError，而不是静默产出空文件。"""
    monkeypatch.setattr(gen_brief, "BRIEF_TEMPLATE", tmp_path / "不存在.docx")
    with pytest.raises(FileNotFoundError, match="简报模板不存在"):
        gen_brief.render_brief({}, {"导语": "", "目的": "", "过程": "", "成效": ""},
                               tmp_path / "out.docx")


def test_gen_brief_main_dry_run_prints_prompt(activity_dir: Path, monkeypatch, capsys):
    """--dry-run 只打印 prompt，不调 LLM。"""
    monkeypatch.setattr(gen_brief.llm, "resolve_style", lambda *a, **k: "务实")
    monkeypatch.setattr(gen_brief.llm, "build_prompt", lambda *a, **k: "假PROMPT")

    code = gen_brief.main(["--activity", str(activity_dir), "--dry-run"])

    assert code == 0
    assert "假PROMPT" in capsys.readouterr().out


def test_gen_brief_main_writes_docx_with_fake_llm(activity_dir: Path, tmp_path: Path,
                                                  monkeypatch, capsys):
    """用假 LLM 结果跑完整 main：--out 产出 docx，内部元信息不进 JSON。"""
    template = _write_docx(tmp_path / "简报.docx", paragraphs=[
        "{{项目名称}}", "{{年月日}}", "{{活动名称}}",
        "{{简报导语}}", "{{诉求描述}}", "{{达到目标}}",
    ])
    monkeypatch.setattr(gen_brief, "BRIEF_TEMPLATE", template)
    monkeypatch.setattr(gen_brief.llm, "resolve_style", lambda *a, **k: "务实")
    monkeypatch.setattr(gen_brief.llm, "write", lambda *a, **k: {
        "导语": "导语内容", "目的": "目的内容", "过程": "过程内容", "成效": "成效内容",
        "_check": {"score": 90, "ok": True, "words": 600, "issues": []},
        "_rewrites": 1,
    })

    out = tmp_path / "简报-已填.docx"
    code = gen_brief.main(["--activity", str(activity_dir), "--out", str(out)])

    assert code == 0
    assert out.is_file()
    assert "导语内容" in _docx_text(out)
    stdout = capsys.readouterr().out
    assert "导语内容" in stdout
    assert "_check" not in stdout              # 内部元信息被过滤


def test_gen_brief_main_llm_error_returns_2(activity_dir: Path, monkeypatch, capsys):
    """LLM 调用失败时返回 2，错误信息写 stderr。"""
    monkeypatch.setattr(gen_brief.llm, "resolve_style", lambda *a, **k: "务实")

    def _boom(*args, **kwargs):
        raise gen_brief.llm.LLMError("上游失败")

    monkeypatch.setattr(gen_brief.llm, "write", _boom)

    code = gen_brief.main(["--activity", str(activity_dir)])

    assert code == 2
    assert "生成失败" in capsys.readouterr().err


def test_gen_brief_main_write_failure_returns_3(activity_dir: Path, tmp_path: Path,
                                                monkeypatch):
    """--out 写回失败（模板缺失）时返回 3，不影响已生成的文字。"""
    monkeypatch.setattr(gen_brief, "BRIEF_TEMPLATE", tmp_path / "不存在.docx")
    monkeypatch.setattr(gen_brief.llm, "resolve_style", lambda *a, **k: "务实")
    monkeypatch.setattr(gen_brief.llm, "write", lambda *a, **k: {
        "导语": "导语", "目的": "目的", "过程": "过程", "成效": "成效",
    })

    code = gen_brief.main(["--activity", str(activity_dir),
                           "--out", str(tmp_path / "out.docx")])

    assert code == 3


# ==========================================================================
# 三、gen_plan.py：build_facts / render_plan / main
# ==========================================================================

def test_plan_build_facts_prefers_activity_yaml(activity_dir: Path, project_root: Path):
    """activity.yaml 优先于 01-docx：名称/时间/人数都取 yaml 的值。"""
    _write_project(project_root / "测试项目")
    _write_plan_docx(activity_dir / "01-活动计划书.docx",
                     name="计划书里的名字", expected="99")
    (activity_dir / "activity.yaml").write_text(
        yaml.safe_dump({
            "活动名称": "邻里共治行动",
            "活动时间": "2026-11-10",
            "活动地点": "民乐北巷",
            "服务对象": "社区居民",
            "预计人数": "20",
            "项目社工": "张三",
            "志愿者人数": "5",
        }, allow_unicode=True),
        encoding="utf-8",
    )

    facts = gen_plan.build_facts(activity_dir)

    assert list(facts) == list(gen_plan.FACT_KEYS)
    assert facts["活动名称"] == "邻里共治行动"      # 不是 docx 里的名字
    assert facts["活动时间"] == "2026-11-10"
    assert facts["活动地点"] == "民乐北巷"
    assert facts["服务对象"] == "社区居民"
    assert facts["预计人数"] == 20                 # 不是 docx 里的 99
    assert facts["项目社工"] == "张三"
    assert facts["志愿者人数"] == "5"
    assert facts["活动类型"] == "共治行动"
    # indicators: 20000 元 / 2 期 → 每期 10000
    assert facts["预算上限"] == 10000.0
    # 项目背景包含项目名、类别、执行期、立项资金与同类指标
    assert "邻里共治" in facts["项目背景"]
    assert "社区保障资金（市级）" in facts["项目背景"]
    assert "执行期 2026-01 至 2026-12" in facts["项目背景"]
    assert "立项资金 120000 元" in facts["项目背景"]
    assert "「共治行动」指标 2场" in facts["项目背景"]


def test_plan_build_facts_falls_back_to_plan_docx(activity_dir: Path, project_root: Path):
    """没有 activity.yaml 时退回 01-docx，时间转成中文格式。"""
    _write_project(project_root / "测试项目")
    _write_plan_docx(activity_dir / "01-活动计划书.docx")

    facts = gen_plan.build_facts(activity_dir)

    assert facts["活动名称"] == "邻里共治行动"
    assert facts["活动时间"] == "2026年11月10日（上午）"
    assert facts["预计人数"] == 20
    assert facts["活动地点"] == "民乐北巷"


def test_plan_build_facts_empty_activity_dir(activity_dir: Path):
    """空活动目录不崩：key 齐全，活动名从目录名反推。"""
    facts = gen_plan.build_facts(activity_dir)

    assert list(facts) == list(gen_plan.FACT_KEYS)
    assert facts["活动名称"] == "共治行动（第一期）"   # activity._name_from_dir
    assert facts["预计人数"] == ""
    assert facts["项目社工"] == ""
    assert facts["项目名称"] == "测试项目"
    assert facts["活动类型"] == "共治行动"
    assert facts["项目背景"] == ""
    assert facts["预算上限"] is None


def test_plan_build_facts_without_project_parent_raises(tmp_path: Path, project_root: Path):
    """现状记录：活动目录不在 <项目>/活动/ 下时 gen_plan.build_facts 直接抛 ValueError。

    _project_name 返回空串 → activity.build_data → projects.load_project("")
    → _safe_name 抛「项目名不能为空」。这是与 gen_brief 行为不一致的地方，
    源码未改，这里固定现状。
    """
    orphan = tmp_path / "孤立活动"
    orphan.mkdir()
    with pytest.raises(ValueError, match="项目名不能为空"):
        gen_plan.build_facts(orphan)


def test_project_background_bits_and_missing_pieces():
    """_project_background 拼装项目名/类别/执行期/资金/指标，缺项跳过。"""
    data = {
        "project": {
            "name": '"带引号"项目',
            "category": "社区保障资金",
            "level": "市级",
            "period": {"start": "2026-01", "end": "2026-12", "months": 12},
        },
        "budget": {"total": 120000},
        "indicators": [{"name": "共治行动", "target": "2场", "budget": 20000}],
    }
    text = gen_plan._project_background(data, "共治行动")
    assert "项目「\"带引号\"项目」" in text
    assert "属社区保障资金（市级）" in text
    assert "执行期 2026-01 至 2026-12" in text
    assert "共 12 个月" in text
    assert "立项资金 120000 元" in text
    assert "「共治行动」指标 2场、该项预算 20000 元" in text
    assert gen_plan._project_background({}, "共治行动") == ""


def test_activity_budget_matches_dirname_and_missing():
    """预算上限按 indicators 拆期后与活动目录名对齐。"""
    data = {"indicators": [{"name": "共治行动", "budget": 20000, "count": 2}]}
    assert gen_plan._activity_budget(data, "01-共治行动-第一期") == 10000.0
    assert gen_plan._activity_budget(data, "02-共治行动-第二期") == 10000.0
    assert gen_plan._activity_budget(data, "99-不存在-第一期") is None
    assert gen_plan._activity_budget({}, "01-共治行动-第一期") is None


def test_render_plan_writes_docx_with_project_name(activity_dir: Path, project_root: Path,
                                                   tmp_path: Path, monkeypatch):
    """render_plan 产出 docx，内容包含项目名、活动名与 LLM 给的环节/困难。"""
    _write_project(project_root / "测试项目")
    template = _write_plan_template(tmp_path / "计划书.docx")
    monkeypatch.setattr(gen_plan, "PLAN_TEMPLATE", template)

    facts = gen_plan.build_facts(activity_dir)
    result = {
        "活动背景内容": "背景文字",
        "环节": [{"内容": "签到", "物资": "签到表", "人员": "社工"}],
        "预计困难": [{"困难": "下雨", "方案": "改到室内"}],
        "物资": [{"名称": "横幅"}],
    }
    out = tmp_path / "输出" / "计划书-已填.docx"

    notes = gen_plan.render_plan("测试项目", activity_dir, facts, result, out)

    assert notes == []
    assert out.is_file()
    text = _docx_text(out)
    # 项目名取项目 yaml 的 project.name；活动名无 yaml/docx 时从目录名反推
    assert '"邻里共治"街区活力营建项目' in text
    assert "共治行动（第一期）" in text
    assert "背景文字" in text
    assert "签到" in text
    assert "下雨" in text and "改到室内" in text
    # 物资预算表本工单不填，占位符被显式清空而不是残留
    assert "{{物资名称_1}}" not in text


def test_render_plan_returns_truncation_notes(activity_dir: Path, project_root: Path,
                                              tmp_path: Path, monkeypatch):
    """环节超过模板 5 行时，render_plan 把截断说明作为 notes 返回。"""
    _write_project(project_root / "测试项目")
    template = _write_plan_template(tmp_path / "计划书.docx")
    monkeypatch.setattr(gen_plan, "PLAN_TEMPLATE", template)

    facts = gen_plan.build_facts(activity_dir)
    result = {"环节": [{"内容": f"环节{i}", "物资": "", "人员": ""}
                       for i in range(1, 8)]}
    notes = gen_plan.render_plan("测试项目", activity_dir, facts, result,
                                 tmp_path / "out.docx")

    assert any("共 7 条" in note for note in notes)
    assert any("5 行" in note for note in notes)


def test_render_plan_missing_template_raises(tmp_path: Path, monkeypatch):
    """计划书模板不存在时抛 FileNotFoundError。"""
    monkeypatch.setattr(gen_plan, "PLAN_TEMPLATE", tmp_path / "不存在.docx")
    with pytest.raises(FileNotFoundError, match="计划书模板不存在"):
        gen_plan.render_plan("测试项目", tmp_path, {}, {}, tmp_path / "out.docx")


def test_gen_plan_main_dry_run_prints_prompt(activity_dir: Path, monkeypatch, capsys):
    """--dry-run 只打印 prompt，不调 LLM。"""
    monkeypatch.setattr(gen_plan.llm, "resolve_style", lambda *a, **k: "务实")
    monkeypatch.setattr(gen_plan.llm, "build_prompt", lambda *a, **k: "假PLANPROMPT")

    code = gen_plan.main(["--activity", str(activity_dir), "--dry-run"])

    assert code == 0
    assert "假PLANPROMPT" in capsys.readouterr().out


def test_gen_plan_main_writes_docx_with_fake_llm(activity_dir: Path, project_root: Path,
                                                 tmp_path: Path, monkeypatch):
    """用假 LLM 结果跑完整 main：--out 产出 docx 且内容正确。"""
    _write_project(project_root / "测试项目")
    template = _write_plan_template(tmp_path / "计划书.docx")
    monkeypatch.setattr(gen_plan, "PLAN_TEMPLATE", template)
    monkeypatch.setattr(gen_plan.llm, "resolve_style", lambda *a, **k: "务实")
    monkeypatch.setattr(gen_plan.llm, "write", lambda *a, **k: {
        "活动背景内容": "假背景",
        "环节": [{"内容": "假环节", "物资": "假物资", "人员": "假人员"}],
        "预计困难": [{"困难": "假困难", "方案": "假方案"}],
    })

    out = tmp_path / "计划书-已填.docx"
    code = gen_plan.main(["--activity", str(activity_dir), "--out", str(out)])

    assert code == 0
    assert out.is_file()
    text = _docx_text(out)
    assert "假背景" in text
    assert "假环节" in text
    assert "假困难" in text and "假方案" in text


def test_gen_plan_main_llm_error_returns_2(activity_dir: Path, monkeypatch, capsys):
    """LLM 调用失败时返回 2。"""
    monkeypatch.setattr(gen_plan.llm, "resolve_style", lambda *a, **k: "务实")

    def _boom(*args, **kwargs):
        raise gen_plan.llm.LLMError("上游失败")

    monkeypatch.setattr(gen_plan.llm, "write", _boom)

    code = gen_plan.main(["--activity", str(activity_dir)])

    assert code == 2
    assert "生成失败" in capsys.readouterr().err


def test_gen_plan_main_write_failure_returns_3(activity_dir: Path, tmp_path: Path,
                                               monkeypatch):
    """--out 写回失败（模板缺失）时返回 3。"""
    monkeypatch.setattr(gen_plan, "PLAN_TEMPLATE", tmp_path / "不存在.docx")
    monkeypatch.setattr(gen_plan.llm, "resolve_style", lambda *a, **k: "务实")
    monkeypatch.setattr(gen_plan.llm, "write", lambda *a, **k: {"活动背景内容": "背景"})

    code = gen_plan.main(["--activity", str(activity_dir),
                          "--out", str(tmp_path / "out.docx")])

    assert code == 3


# ==========================================================================
# 四、fill_plan_from_xlsx.py：main 全流程
# ==========================================================================

def test_fill_plan_main_writes_docx_from_xlsx(activity_dir: Path, project_root: Path,
                                              tmp_path: Path, monkeypatch, capsys):
    """最小 xlsx → main → docx：环节/困难/物资都填进模板，模板本身不被改。"""
    _write_project(project_root / "测试项目", name="测试项目")
    template = _write_plan_template(tmp_path / "计划书.docx")
    monkeypatch.setattr(fill_plan_from_xlsx, "PLAN_TEMPLATE", template)
    template_before = template.read_bytes()

    sheet = _make_xlsx(
        tmp_path / "计划书-填写表.xlsx",
        steps=[("准备阶段", "9:00-9:30", "签到、宣读公约", "签到表", "社工", "")],
        difficulties=[("车主不配合", "提前张贴告知")],
        materials=[
            ("宣传", "横幅", "2条", 50, None),        # 小计故意不填
            ("饮用水", "矿泉水", "3箱", 30, None),
        ],
    )
    out = tmp_path / "计划书-已填.docx"

    code = fill_plan_from_xlsx.main([
        "--activity", str(activity_dir),
        "--sheet", str(sheet),
        "--out", str(out),
    ])

    assert code == 0
    assert out.is_file()
    assert template.read_bytes() == template_before      # 模板只读

    text = _docx_text(out)
    assert "测试项目" in text
    assert "签到、宣读公约" in text
    assert "车主不配合" in text and "提前张贴告知" in text
    assert "横幅" in text
    assert "100" in text                                  # 2×50 重算小计
    assert "190" in text                                  # 合计 100+90

    printed = capsys.readouterr().out
    assert "环节 1 条" in printed
    assert "困难 1 条" in printed
    assert "物资 2 条" in printed
    assert "物资合计 190.00 元" in printed
    assert "输出:" in printed


def test_fill_plan_main_missing_activity(tmp_path: Path, project_root: Path):
    """活动目录不存在时返回 1，不继续读表。"""
    code = fill_plan_from_xlsx.main([
        "--activity", str(tmp_path / "不存在"),
        "--sheet", str(tmp_path / "无.xlsx"),
    ])
    assert code == 1


def test_fill_plan_main_missing_sheet(activity_dir: Path, tmp_path: Path, monkeypatch):
    """填写表不存在时返回 2。"""
    monkeypatch.setattr(fill_plan_from_xlsx, "PLAN_TEMPLATE",
                        _write_plan_template(tmp_path / "计划书.docx"))
    code = fill_plan_from_xlsx.main([
        "--activity", str(activity_dir),
        "--sheet", str(tmp_path / "不存在.xlsx"),
    ])
    assert code == 2


def test_fill_plan_main_missing_template(activity_dir: Path, tmp_path: Path, monkeypatch):
    """计划书模板不存在时返回 1。"""
    sheet = _make_xlsx(tmp_path / "表.xlsx")
    monkeypatch.setattr(fill_plan_from_xlsx, "PLAN_TEMPLATE",
                        tmp_path / "不存在.docx")
    code = fill_plan_from_xlsx.main([
        "--activity", str(activity_dir),
        "--sheet", str(sheet),
    ])
    assert code == 1


def test_fill_plan_main_permission_error_returns_3(activity_dir: Path, tmp_path: Path,
                                                   monkeypatch):
    """写 docx 无权限时返回 3。"""
    monkeypatch.setattr(fill_plan_from_xlsx, "PLAN_TEMPLATE",
                        _write_plan_template(tmp_path / "计划书.docx"))
    sheet = _make_xlsx(tmp_path / "表.xlsx")

    def _denied(*args, **kwargs):
        raise PermissionError("只读")

    monkeypatch.setattr(fill_plan_from_xlsx.fill, "render_docx", _denied)

    code = fill_plan_from_xlsx.main([
        "--activity", str(activity_dir),
        "--sheet", str(sheet),
    ])
    assert code == 3


def test_fill_plan_main_bad_workbook_returns_2(activity_dir: Path, tmp_path: Path,
                                               monkeypatch):
    """xlsx 缺少约定工作表时返回 2（plan_sheet.read_workbook 抛 ValueError）。"""
    monkeypatch.setattr(fill_plan_from_xlsx, "PLAN_TEMPLATE",
                        _write_plan_template(tmp_path / "计划书.docx"))
    workbook = Workbook()
    workbook.active.title = "别的表"
    bad = tmp_path / "坏表.xlsx"
    workbook.save(str(bad))
    workbook.close()

    code = fill_plan_from_xlsx.main([
        "--activity", str(activity_dir),
        "--sheet", str(bad),
    ])
    assert code == 2
