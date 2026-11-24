#!/usr/bin/env python3
"""模板 ↔ LLM 字段覆盖回归测试。

工单背景：18 个模板里只有少数几个有「需要 LLM 写长文字」的段落，其余是纯数据
表（金额/日期/名单）。这个文件同时守住两件事：

1. **防回归（关键）**：`llm.TASKS[task]["lists"][字段]["item"]` 是子字段白名单，
   不在这里的子字段会被 normalize 阶段静默丢弃 —— 之前 schema 加了「阶段/时间」，
   白名单没加，模型生成了也填不进模板。所以白名单必须是 schema 声明子字段的**超集**。
2. **字段落点**：task 的每个 field 在模板里要么有 `{{字段}}` 占位符，要么是
   lists 里的结构化字段（展开成 `{{字段_1}}`…）。没有落点的字段等于白生成。

3. **前端路由**：`web/app.js` 的 `LLM_TASK_BY_TPL` 必须覆盖所有有 LLM 段的模板。
   漏一个，前端就 `|| 'plan'` 兜底成 plan，生成一堆计划书字段，模板整篇留空。

跑法：
    python3 -m pytest tests/test_template_llm_coverage.py -q

依赖真实模板目录，缺目录时整文件跳过（与 test_template_layout.py 同规）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = Path(os.environ.get("SOCIAL_WORK_TEMPLATES",
                                "/srv/social-work/templates/library"))
APP_JS = REPO / "web" / "app.js"

sys.path.insert(0, str(REPO))
from engine import fill, llm  # noqa: E402

# 整个文件依赖真实模板资产；缺目录时全部跳过，不能 fail。
pytestmark = pytest.mark.skipif(
    not TEMPLATES.is_dir(), reason=f"缺少真实模板目录 {TEMPLATES}")


# 模板名 → (task, 该模板的 LLM 字段是否必填)
# 判定依据见工单报告 A 步的 18 行判定表：
#   需要 LLM 的 5 个模板全部有 task 了。活动通知的正文段现在由 task `notice`
#   生成「活动简介」一句（2026-11-24），它是**可选** LLM：正文段之外的
#   时间/地点/对象/联系人都是纯数据，不勾 LLM 也能出文。
COVERED = {
    "计划书": ("plan", True),
    "简报": ("brief", True),
    "总结报告": ("summary", True),
    "监督记录": ("supervision", True),
    "活动通知": ("notice", False),
}


# 字段已在模板里以「派生占位符」形式出现，不需要 {{{{字段}}}} 本身。
# 活动通知：正文段写的是 `{{{{活动简介_suffix}}}}`，它是 build_data 里由
# 「活动简介」派生的（空值不补「，」），LLM 只产出「活动简介」。
_DERIVED_PLACEHOLDERS = {
    "活动简介": "活动简介_suffix",
}

# 由 activity.yaml / calc / 项目数据提供的字段，不归 LLM 生成
_ACTIVITY_DATA_FIELDS = {
    "图注", "年月日", "项目名称", "项目社工", "项目负责人", "项目编号",
    "活动名称", "活动时间", "活动时间_cn", "活动地点", "活动编号",
    "实际人数", "人手分配", "服务对象", "活动简介_suffix", "社区名称",
    "联系电话", "志愿者人数", "预计人数",
}


def _template(name: str) -> Path:
    path = TEMPLATES / f"{name}.docx"
    if not path.is_file():
        pytest.skip(f"缺少模板 {path}")
    return path


def _placeholders(name: str) -> set[str]:
    """模板占位符，去掉花括号只留字段名。"""
    return {p.strip("{}") for p in fill.scan_placeholders(_template(name))}


def _flatten_schema_fields(schema) -> list[str]:
    """取出 schema 里声明的子字段名。

    schema 有两种形态：
    - 字符串（普通文字段）：不是列表，没有子字段
    - 列表（结构化清单）：元素是 dict，dict 的 key 即子字段；有的元素还会嵌套
      list（如环节里的「子项」），递归收全。
    """
    if isinstance(schema, str) or not isinstance(schema, list):
        return []
    keys: list[str] = []
    for item in schema:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            keys.append(key)
            keys.extend(_flatten_schema_fields(value))
    # 去重保序
    return list(dict.fromkeys(keys))


def _list_fields(task: str) -> dict[str, dict]:
    return llm.TASKS[task].get("lists") or {}


def _row_specs() -> dict[str, dict]:
    """engine.activity.PLAN_ROW_SPECS：结构化字段 → 模板占位符前缀。

    环节的子字段「阶段」在模板里是 {{环节阶段_N}}，不是 {{环节_N}}；
    这层映射就是「子字段 → 占位符前缀」的对照表。
    """
    from engine import activity
    return activity.PLAN_ROW_SPECS


# --------------------------------------------------------------------------
# 1. 占位符能被 scan_placeholders 扫到
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(COVERED))
def test_template_placeholders_scanned(name):
    """模板里该有的 LLM 占位符确实扫得到（模板被覆写/回退时立刻失败）。"""
    task = COVERED[name][0]
    spec = llm.TASKS[task]
    found = _placeholders(name)
    specs = _row_specs()

    assert found, f"{name}.docx 一个占位符都没扫到"

    for field in spec["fields"]:
        if field in _list_fields(task):
            # 结构化字段按 PLAN_ROW_SPECS 展开成 {{前缀_N}}
            columns = specs[field]["columns"]
            for sub, prefix in columns.items():
                hit = {p for p in found if re.fullmatch(rf"{re.escape(prefix)}_\d+", p)}
                expect = "{{" + prefix + "_1}}"
                assert hit, (
                    f"{name}.docx 缺少「{field}.{sub}」的占位符"
                    f"（应为 {expect}）")
        elif field in _DERIVED_PLACEHOLDERS:
            derived = _DERIVED_PLACEHOLDERS[field]
            assert derived in found, (
                f"{name}.docx 既没有 {{{{{field}}}}}，也没有派生占位符 "
                f"{{{{{derived}}}}} —— task {task!r} 生成了也填不进去")
        else:
            assert field in found, (
                f"{name}.docx 缺少占位符 {{{{{field}}}}} —— "
                f"task {task!r} 生成了也填不进去")


# --------------------------------------------------------------------------
# 2. task.fields 里确有该字段
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(COVERED))
def test_task_declares_template_fields(name):
    """模板的每个 LLM 占位符都能在对应 task 的 fields 里找到。

    注意按「占位符前缀 → 结构化字段」反查：{{环节阶段_1}} 的 base 是「环节阶段」，
    它对应 lists 里的「环节」字段的子字段「阶段」，不是 {{环节_N}}。
    """
    task = COVERED[name][0]
    spec = llm.TASKS[task]
    fields = set(spec["fields"])
    list_keys = set(_list_fields(task))

    # 前缀 → (结构化字段, 子字段)
    prefix_to_list: dict[str, tuple[str, str]] = {}
    for key, row in _row_specs().items():
        for sub, prefix in row["columns"].items():
            prefix_to_list[prefix] = (key, sub)

    # 派生列（物资小计 = 单价 × 数量）由代码算，不归模型
    computed = {row.get("total") for row in _row_specs().values() if row.get("total")}
    for row in _row_specs().values():
        computed.update((row.get("computed") or {}).values())
    computed.discard(None)

    for ph in _placeholders(name):
        base = re.sub(r"_\d+$", "", ph)
        if base in _ACTIVITY_DATA_FIELDS or base in computed:
            continue  # 来自 activity.yaml / calc 的数据字段，不归 LLM
        if base in prefix_to_list:
            key, sub = prefix_to_list[base]
            assert key in list_keys, (
                f"{name}.docx 占位符 {{{{{ph}}}}} 对应结构化字段 {key!r}，"
                f"但 task {task!r} 的 lists 里没有它")
            assert sub in _list_fields(task)[key]["item"], (
                f"{name}.docx 占位符 {{{{{ph}}}}} 对应 {key}.{sub}，"
                f"但 {key}.item 白名单里没有 {sub!r}")
            continue
        assert base in fields, (
            f"{name}.docx 的占位符 {{{{{ph}}}}} 在 task {task!r} 的 fields 里找不到")


# --------------------------------------------------------------------------
# 3. lists.<字段>.item 白名单 ⊇ schema 子字段（防回归关键）
# --------------------------------------------------------------------------

def test_plan_lists_whitelist_covers_schema():
    """plan 的环节/预计困难/物资：item 白名单必须收全 schema 子字段。

    这就是 2026-11-23 那个坑：schema 加了「阶段/时间/备注」，item 白名单没加，
    模型生成了也被 normalize 丢掉，模板里 {{环节阶段_N}} 全是空的。
    """
    spec = llm.TASKS["plan"]
    lists = _list_fields("plan")
    assert lists, "plan 没有 lists 声明"

    for key, conf in lists.items():
        schema_keys = _flatten_schema_fields(spec["schema"][key])
        assert schema_keys, f"plan.schema[{key!r}] 没声明子字段"
        whitelist = tuple(conf["item"])
        missing = [k for k in schema_keys if k not in whitelist]
        assert not missing, (
            f"plan.lists[{key!r}].item 白名单漏了 {missing}；"
            f"schema 声明了但白名单没有 → 模型生成了也会被静默丢弃。"
            f"白名单={whitelist}，schema={schema_keys}")
        # 反过来：白名单里的项也必须在 schema 里，否则是死字段
        extra = [k for k in whitelist if k not in schema_keys]
        assert not extra, (
            f"plan.lists[{key!r}].item 白名单含 schema 未声明的 {extra}")


@pytest.mark.parametrize("name", sorted(COVERED))
def test_lists_whitelist_covers_schema_all_tasks(name):
    """同上，推广到所有有 lists 的 task（总结报告/简报目前没有 lists，跳过）。"""
    task = COVERED[name][0]
    spec = llm.TASKS[task]
    for key, conf in _list_fields(task).items():
        schema_keys = _flatten_schema_fields(spec["schema"][key])
        whitelist = tuple(conf["item"])
        missing = [k for k in schema_keys if k not in whitelist]
        assert not missing, (
            f"{task}.lists[{key!r}].item 白名单漏了 {missing}。"
            f"白名单={whitelist}，schema={schema_keys}")


# --------------------------------------------------------------------------
# 4. 用假数据 render 一次，断言无残留 {{...}}
# --------------------------------------------------------------------------

def _fake_value(field: str, schema) -> str:
    if isinstance(schema, str):
        return f"【{field}】这是用于测试的占位文字，长度足够。"
    return f"【{field}】"


def _fake_facts(name: str, task: str) -> dict:
    """构造能覆盖模板全部占位符的假数据（LLM 字段 + 活动数据字段）。"""
    spec = llm.TASKS[task]
    data: dict[str, str] = {}

    # LLM 字段
    for field in spec["fields"]:
        data[field] = _fake_value(field, spec["schema"][field])
    # 结构化字段：给 5 条（模板环节/物资都是 5 行，领取表 16 行另算）
    for key, conf in _list_fields(task).items():
        data[key] = [
            {sub: f"【{key}{idx}·{sub}】" for sub in conf["item"]}
            for idx in range(1, 6)
        ]

    # 非 LLM 的数据字段：按模板实际占位符补齐，值统一给一段文字
    for ph in _placeholders(name):
        base = re.sub(r"_\d+$", "", ph)
        data.setdefault(base, f"【{base}】")
    return data


@pytest.mark.parametrize("name", sorted(COVERED))
def test_render_leaves_no_placeholder(name, tmp_path):
    """假数据 render 一遍，成品里不能再有 {{...}} 残骸。"""
    task = COVERED[name][0]
    src = _template(name)
    dst = tmp_path / f"{name}-filled.docx"

    result = fill.render(src, dst, _fake_facts(name, task))

    # 渲染结果里不许有残留占位符
    leftover = fill.scan_placeholders(dst)
    assert leftover == [], f"{name} 渲染后仍残留占位符：{leftover}"
    assert result["missing"] == [], f"{name} 渲染缺字段：{result['missing']}"

    # 模板本身不能被就地修改
    assert fill.scan_placeholders(src) == fill.scan_placeholders(_template(name))


# --------------------------------------------------------------------------
# 5. 前端路由：LLM_TASK_BY_TPL 必须覆盖所有有 LLM 段的模板
# --------------------------------------------------------------------------

def _parse_llm_task_map() -> dict[str, str]:
    """从 web/app.js 里抠出 LLM_TASK_BY_TPL 的对象字面量。"""
    text = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"const LLM_TASK_BY_TPL = \{(.*?)\};", text, re.S)
    assert match, "web/app.js 里找不到 LLM_TASK_BY_TPL"
    body = match.group(1)
    return dict(re.findall(r"'([^']+)':\s*'([^']+)'", body))


def _parse_required_llm() -> list[str]:
    text = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"const REQUIRED_LLM = \[(.*?)\];", text, re.S)
    assert match, "web/app.js 里找不到 REQUIRED_LLM"
    return re.findall(r"'([^']+)'", match.group(1))


def test_frontend_map_covers_every_llm_template():
    """前端映射缺一个 template，那个模板就会被 `|| 'plan'` 兜底成计划书任务。

    总结报告曾是受害者：后端 TASKS 有 summary，前端没映射 → 走 plan →
    生成 13 个计划书字段 → 总结报告 19 个格子全空。
    """
    mapping = _parse_llm_task_map()
    for name, (task, _required) in COVERED.items():
        assert name in mapping, (
            f"web/app.js LLM_TASK_BY_TPL 缺 '{name}' → 会兜底成 plan，"
            f"生成结果一个都填不进模板")
        assert mapping[name] == task, (
            f"web/app.js 把 '{name}' 映射到 {mapping[name]!r}，应为 {task!r}")


def test_frontend_map_only_points_at_real_tasks():
    """前端映射的 task 名必须是 llm.TASKS 里真实存在的任务。"""
    for name, task in _parse_llm_task_map().items():
        assert task in llm.TASKS, (
            f"web/app.js 把 '{name}' 映射到不存在的 task {task!r}；"
            f"可选：{sorted(llm.TASKS)}")


def test_required_llm_contains_mapping_keys():
    """REQUIRED_LLM 必须是 LLM_TASK_BY_TPL 的 key 的子集，否则勾选框行为错乱。

    原来要求两者完全相等；活动通知加入后两者不再相等 —— 它是**可选** LLM
    （2026-11-24 工单 B3 明确要求「REQUIRED_LLM 不要加活动通知」）。
    真正要守的是：REQUIRED_LLM 里的模板必须有映射（否则强制勾选却走错任务），
    且活动通知不能进 REQUIRED_LLM（否则用户没法关掉 LLM）。
    """
    mapping = set(_parse_llm_task_map())
    required = set(_parse_required_llm())
    assert required <= mapping, (
        f"REQUIRED_LLM 里有模板没有 LLM 映射：{sorted(required - mapping)}")
    assert "活动通知" in mapping, "活动通知必须有 notice 映射，否则兜底成 plan"
    assert "活动通知" not in required, "活动通知是可选 LLM，不该被强制勾选"


def test_covered_table_matches_frontend():
    """本文件 COVERED 的必填声明要与前端 REQUIRED_LLM 双向对齐。"""
    required = set(_parse_required_llm())
    for name, (_task, is_required) in COVERED.items():
        if is_required:
            assert name in required, f"{name} 在本文件声明必填，前端 REQUIRED_LLM 却没有"
        else:
            assert name not in required, f"{name} 在本文件声明可选，前端却强制勾选"


# ---------- 计划书表格合并缺陷（2026-11-24 修） ----------

def test_plan_table_columns_not_vertically_merged():
    """计划书表0/表2 的第 0 列必须是 5 个独立单元格。

    原模板把「阶段」「类别」列做了纵向合并（行1+2、行4+5、行2+3+4），
    导致 {{环节阶段_2}} {{物资类别_3}} 等占位符根本不存在，5 行只能填出
    2~3 个不同的值。修法是重建单元格、去掉 vMerge。
    """
    from docx import Document
    from docx.oxml.ns import qn
    from pathlib import Path

    tpl = Path("/srv/social-work/templates/library/计划书.docx")
    if not tpl.is_file():
        pytest.skip("模板库无计划书.docx")
    doc = Document(str(tpl))
    for ti in (0, 2):
        table = doc.tables[ti]
        seen = []
        for row in table.rows:
            tc = row.cells[0]._tc
            tcPr = tc.tcPr
            vm = tcPr.find(qn("w:vMerge")) if tcPr is not None else None
            assert vm is None, f"表{ti} 第 0 列仍有纵向合并"
            seen.append(id(tc))
        assert len(set(seen)) == len(seen), f"表{ti} 第 0 列有行共用单元格"


def test_plan_placeholders_complete_for_all_rows():
    """计划书每张表的每一行都要有自己的序号占位符，不能缺号。"""
    from pathlib import Path
    from engine import fill

    tpl = Path("/srv/social-work/templates/library/计划书.docx")
    if not tpl.is_file():
        pytest.skip("模板库无计划书.docx")
    ph = {p.strip("{}") for p in fill.scan_placeholders(tpl)}
    for prefix in ("环节阶段", "环节时间", "环节内容",
                   "物资类别", "物资名称", "物资规格", "物资单价", "物资小计"):
        for n in range(1, 6):
            assert f"{prefix}_{n}" in ph, f"缺占位符 {prefix}_{n}"
