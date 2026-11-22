"""web/util.js 纯函数单测。

用 Node 子进程 require util.js，再 eval 表达式；不启动服务、不碰 DOM。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UTIL = REPO / "web" / "util.js"
NODE = shutil.which("node")


def _node(expr: str) -> str:
    """在 node 里 eval 一段表达式，返回 stdout。"""
    if not NODE:
        pytest.skip("node 不可用")
    script = (
        "require(process.argv[1]);"
        "process.stdout.write(String(eval(process.argv[2])));"
    )
    proc = subprocess.run(
        [NODE, "-e", script, str(UTIL), expr],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"node 执行失败：{expr}\nstderr={proc.stderr}"
    return proc.stdout


def _node_json(expr: str):
    """eval 一段返回 JSON 的表达式，再反序列化成 Python 对象。"""
    return json.loads(_node(f"JSON.stringify({expr})"))


# ---------------- 挂载与加载 ----------------

def test_util_file_exists_and_mounts_functions():
    assert UTIL.is_file()
    names = [
        "esc", "fmtBytes", "fmtTime", "fmtMoney", "todayCn", "phKey",
        "projectShort", "statusBadge", "checkBadge", "projectCodeHint",
        "reviewText", "checkSummary", "reviewSummary", "todayIso",
        "activityNameFromDir", "pickTemplate", "SAT_GRADE",
        "parseSearchOutput", "permText", "maskSecret",
    ]
    assert _node("typeof globalThis.esc") == "function"
    for name in names:
        assert _node(f"typeof globalThis.{name}") in ("function", "object"), name


# ---------------- esc：HTML 转义 ----------------

def test_esc_escapes_all_specials():
    assert _node("""esc(`<>&"'`)""") == "&lt;&gt;&amp;&quot;&#39;"


@pytest.mark.parametrize("raw,expected", [
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("'", "&#39;"),
])
def test_esc_each_special_char(raw, expected):
    assert _node(f"esc({json.dumps(raw)})") == expected


@pytest.mark.parametrize("expr,expected", [
    ("esc('')", ""),
    ("esc(null)", ""),
    ("esc(undefined)", ""),
    ("esc()", ""),
    ("esc(0)", "0"),
    ("esc(false)", "false"),
])
def test_esc_empty_null_undefined(expr, expected):
    assert _node(expr) == expected


def test_esc_keeps_plain_text_and_chinese():
    assert _node("esc('惠民社区 2026')") == "惠民社区 2026"


# ---------------- fmtBytes：文件大小 ----------------

@pytest.mark.parametrize("n,expected", [
    (0, "0 B"),
    (1023, "1023 B"),
    (1024, "1.0 KB"),
    (1536, "1.5 KB"),
    (1024 * 1024, "1.0 MB"),
    (1024 * 1024 * 1024, "1.00 GB"),
    (5 * 1024 * 1024 * 1024, "5.00 GB"),
])
def test_fmt_bytes_units(n, expected):
    assert _node(f"fmtBytes({n})") == expected


@pytest.mark.parametrize("expr,expected", [
    ("fmtBytes(null)", "—"),
    ("fmtBytes(undefined)", "—"),
    ("fmtBytes('abc')", "—"),
    ("fmtBytes(NaN)", "—"),
])
def test_fmt_bytes_invalid(expr, expected):
    assert _node(expr) == expected


# ---------------- projectShort：项目简称 ----------------

def test_project_short_prefers_backend_short():
    assert _node(
        "projectShort({name:'惠民项目', data:{project:{short:'惠民'}}})"
    ) == "惠民"


def test_project_short_falls_back_to_dir_name():
    assert _node("projectShort({name:'花都项目', data:{}} )") == "花都项目"
    assert _node("projectShort({name:'激励项目'})") == "激励项目"


def test_project_short_does_not_strip_suffix():
    # 现状：裁剪只来自后端 data.project.short，函数本身不按「社区/项目」后缀处理。
    # 这里锁住实际行为，避免误以为前端有后缀裁剪逻辑。
    assert _node("projectShort({name:'惠民社区项目'})") == "惠民社区项目"


def test_project_short_empty():
    assert _node("projectShort({name:''})") == ""


# ---------------- statusBadge：活动状态 ----------------

@pytest.mark.parametrize("status,expected", [
    ("imported", '<span class="badge info">已入库</span>'),
    ("generated", '<span class="badge ok">已生成</span>'),
    ("edited", '<span class="badge warn">人工修改</span>'),
    ("reviewed", '<span class="badge ok">已核对</span>'),
    ("signed", '<span class="badge ok">已签字</span>'),
])
def test_status_badge_known(status, expected):
    assert _node(f"statusBadge({json.dumps(status)})") == expected


def test_status_badge_empty_is_new_dir():
    assert _node("statusBadge('')") == '<span class="badge muted">新目录</span>'
    assert _node("statusBadge(null)") == '<span class="badge muted">新目录</span>'


def test_status_badge_unknown_shows_raw_and_escapes():
    assert _node("statusBadge('weird<script>')") == (
        '<span class="badge muted">未知状态（weird&lt;script&gt;）</span>'
    )
    assert _node("statusBadge('  unknown  ')") == (
        '<span class="badge muted">未知状态（unknown）</span>'
    )


# ---------------- checkBadge / checkSummary ----------------

def test_check_badge_null():
    assert _node("checkBadge(null)") == '<span class="badge muted">—</span>'


@pytest.mark.parametrize("expr,expected", [
    ("checkBadge({complete:true})", '<span class="badge ok">完整</span>'),
    ("checkBadge({missing_count:2})", '<span class="badge warn">缺 2 项</span>'),
    ("checkBadge({partial_count:3})",
     '<span class="badge warn">部分不完整（3 项待补）</span>'),
    ("checkBadge({})", '<span class="badge warn">未完整</span>'),
    # 缺项优先于部分不完整
    ("checkBadge({missing_count:1, partial_count:2})",
     '<span class="badge warn">缺 1 项</span>'),
])
def test_check_badge_states(expr, expected):
    assert _node(expr) == expected


def test_check_summary_complete():
    out = _node(
        "checkSummary({complete:true, ok_count:15, partial_count:0,"
        " missing_count:0, na_count:0})"
    )
    assert "齐 15 项" in out
    assert "待补 0 项" in out
    assert "缺 0 项" in out
    assert "不适用 0 项" in out
    assert '<span class="badge ok">完整</span>' in out


def test_check_summary_incomplete_and_default_partial():
    out = _node(
        "checkSummary({complete:false, ok_count:10, missing_count:2, na_count:3})"
    )
    assert "齐 10 项" in out
    assert "待补 0 项" in out
    assert "缺 2 项" in out
    assert "不适用 3 项" in out
    assert '<span class="badge warn">未完整</span>' in out


# ---------------- reviewSummary ----------------

def test_review_summary_uses_backend_counts():
    out = _node(
        "reviewSummary({reviewed_required_count:3, required_count:5,"
        " items:[{signed:true},{signed:true},{signed:false}],"
        " deliverable:true})"
    )
    assert out == '已核对 3/5 · 已签字 2 <span class="badge ok">可交付</span>'


def test_review_summary_fallback_filters_na():
    # 没有后端计数时：必需项（status !== 'na'）里数 review。
    out = _node(
        "reviewSummary({items:["
        "{status:'ok', review:{}}, {status:'missing'},"
        "{status:'na', review:{}}, {status:'ok', review:{}, signed:true}],"
        " deliverable:false})"
    )
    assert out == '已核对 2/3 · 已签字 1 <span class="badge muted">未齐备</span>'


# ---------------- 日期 / 金额格式化 ----------------

@pytest.mark.parametrize("iso,expected", [
    ("2026-11-22T13:45:09", "2026-11-22 13:45"),
    ("2026-11-22 13:45:09", "2026-11-22 13:45"),
    ("2026-11-22", "2026-11-22"),
])
def test_fmt_time(iso, expected):
    assert _node(f"fmtTime({json.dumps(iso)})") == expected


@pytest.mark.parametrize("expr,expected", [
    ("fmtTime('')", "—"),
    ("fmtTime(null)", "—"),
    ("fmtTime(undefined)", "—"),
])
def test_fmt_time_empty(expr, expected):
    assert _node(expr) == expected


@pytest.mark.parametrize("expr,expected", [
    ("fmtMoney(0)", "0.00"),
    ("fmtMoney(1.5)", "1.50"),
    ("fmtMoney('12.345')", "12.35"),
])
def test_fmt_money(expr, expected):
    assert _node(expr) == expected


@pytest.mark.parametrize("expr,expected", [
    ("fmtMoney(null)", "—"),
    ("fmtMoney(undefined)", "—"),
    ("fmtMoney(NaN)", "—"),
    ("fmtMoney('abc')", "—"),
])
def test_fmt_money_invalid(expr, expected):
    assert _node(expr) == expected


def test_today_cn_format():
    out = _node("todayCn()")
    assert re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日", out), out
    assert str(date.today().year) in out


def test_today_iso_format():
    out = _node("todayIso()")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", out), out
    assert "T" not in out
    assert out == _node("todayIso()")


# ---------------- phKey：模板占位符 ----------------

@pytest.mark.parametrize("raw,expected", [
    ("{{活动名称}}", "活动名称"),
    ("{{ 活动名称 }}", "活动名称"),
    ("活动名称", "活动名称"),
    ("", ""),
])
def test_ph_key(raw, expected):
    assert _node(f"phKey({json.dumps(raw)})") == expected


def test_ph_key_null():
    assert _node("phKey(null)") == ""


# ---------------- activityNameFromDir ----------------

@pytest.mark.parametrize("dirname,expected", [
    ("01-惠民社区活动", "惠民社区活动"),
    ("02_敬老活动_第3期", "敬老活动（第3期）"),
    ("03-走访_第12期", "走访（第12期）"),
    ("", ""),
])
def test_activity_name_from_dir(dirname, expected):
    assert _node(f"activityNameFromDir({json.dumps(dirname)})") == expected


def test_activity_name_from_dir_null():
    assert _node("activityNameFromDir(null)") == ""


# ---------------- pickTemplate ----------------

def test_pick_template_by_task():
    tpls = (
        "[{name:'活动简报模板'},{name:'活动计划书模板'},"
        "{name:'总结报告模板'}]"
    )
    assert _node(f"pickTemplate({tpls}, 'brief').name") == "活动简报模板"
    assert _node(f"pickTemplate({tpls}, 'plan').name") == "活动计划书模板"
    assert _node(f"pickTemplate({tpls}, 'summary').name") == "总结报告模板"


def test_pick_template_falls_back_to_first():
    assert _node("pickTemplate([{name:'其他模板'}], 'brief').name") == "其他模板"


def test_pick_template_empty_returns_null():
    assert _node("pickTemplate([], 'brief') === null") == "true"


# ---------------- SAT_GRADE ----------------

@pytest.mark.parametrize("avg,expected", [
    (95, "优秀"),
    (90, "优秀"),
    (89.9, "良好"),
    (80, "良好"),
    (79, "一般"),
    (70, "一般"),
    (69, "合格"),
    (60, "合格"),
    (59.9, "不合格"),
])
def test_sat_grade(avg, expected):
    assert _node(f"SAT_GRADE({avg})") == expected


@pytest.mark.parametrize("expr", ["SAT_GRADE(null)", "SAT_GRADE(NaN)"])
def test_sat_grade_empty(expr):
    assert _node(expr) == ""


# ---------------- parseSearchOutput ----------------

def test_parse_search_output_blocks_and_score():
    text = (
        "检索结果\n\n"
        "### 第一篇（score: 0.87）\n"
        "内容 A\n第二行\n\n"
        "### 第二篇 (score：0.5)\n"
        "内容 B\n"
    )
    parsed = _node_json(f"parseSearchOutput({json.dumps(text)})")
    assert parsed["head"] == "检索结果"
    assert [b["title"] for b in parsed["blocks"]] == ["第一篇", "第二篇"]
    assert [b["score"] for b in parsed["blocks"]] == ["0.87", "0.5"]
    assert parsed["blocks"][0]["content"] == "内容 A\n第二行"
    assert parsed["blocks"][1]["content"] == "内容 B"


def test_parse_search_output_title_without_score():
    parsed = _node_json("parseSearchOutput('### 无分数标题\\n正文')")
    assert len(parsed["blocks"]) == 1
    assert parsed["blocks"][0]["title"] == "无分数标题"
    assert parsed["blocks"][0]["score"] is None
    assert parsed["blocks"][0]["content"] == "正文"


def test_parse_search_output_no_blocks_keeps_head():
    parsed = _node_json("parseSearchOutput('纯文本\\n第二行')")
    assert parsed["blocks"] == []
    assert parsed["head"] == "纯文本\n第二行"


def test_parse_search_output_empty():
    parsed = _node_json("parseSearchOutput(null)")
    assert parsed == {"head": "", "blocks": [], "raw": ""}


# ---------------- reviewText ----------------

def test_review_text_without_review():
    assert _node("reviewText({})") == ""


def test_review_text_unsigned_and_signed():
    assert _node(
        "reviewText({review:{reviewed_at:'2026-11-22T10:00:00',"
        " reviewed_by:'张三'}})"
    ) == "✓ 张三 2026-11-22"
    assert _node(
        "reviewText({signed:true, review:{reviewed_at:'2026-11-22T10:00:00',"
        " reviewed_by:'张三'}})"
    ) == "✓✓ 已签字（张三 2026-11-22）"


def test_review_text_escapes_user_input():
    assert _node(
        "reviewText({review:{reviewed_at:'2026-11-22', reviewed_by:'<b>'}})"
    ) == "✓ &lt;b&gt; 2026-11-22"


# ---------------- permText / maskSecret / projectCodeHint ----------------

def test_perm_text_maps_access():
    assert _node("permText({huimin:'write', huadu:'readonly'})") == (
        "huimin：读写；huadu：只读"
    )
    # 非 write 一律按只读显示（沿用原逻辑）
    assert _node("permText({huimin:'other'})") == "huimin：只读"


def test_perm_text_empty():
    assert _node("permText({})") == "—"
    assert _node("permText(null)") == "—"


def test_mask_secret_keeps_first_12_chars():
    assert _node("maskSecret('abcdefghijklmnop')") == "abcdefghijkl" + "••••••••"
    assert _node("maskSecret('short')") == "short" + "••••••••"


def test_mask_secret_empty():
    assert _node("maskSecret('')") == "••••••••"
    assert _node("maskSecret(null)") == "••••••••"


def test_project_code_hint():
    assert _node("projectCodeHint({})") == "未配置立项月"
    assert _node("projectCodeHint({code_rules:{establish_month:5}})") == (
        "CDYJR<立项年>005-C-…（活动级编号）"
    )
    assert _node("projectCodeHint({code_rules:{establish_month:12}})") == (
        "CDYJR<立项年>012-C-…（活动级编号）"
    )


def test_project_code_hint_null_and_zero_month():
    assert _node("projectCodeHint(null)") == "未配置立项月"
    assert _node("projectCodeHint({code_rules:{establish_month:0}})") == "未配置立项月"
