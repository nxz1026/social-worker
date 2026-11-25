#!/usr/bin/env python3
"""项目书解析：docx / pdf / md → 项目 yaml 结构（project / parties / staff / 预算 / 指标）。

设计原则（用户定的硬规矩）：
**解析不到就留空 + 返回 warnings，绝不编造数据。**

- 字段正则抓不到 → 该字段空串 / 空 dict / 空列表，并在 warnings 里说明
- 指标只有「活动名称 + 精确期数」能确认时才写 count；
  「120人次」「6次」这种服务量不是期数，不猜（见 engine/activities.py 同款教训）
- 预算按「费用类别 / 金额」能对上才统计，对不上就给空 dict

只做文本 → 结构，不碰磁盘、不写 yaml；写盘由 api/routes/projects.py 负责。
"""
from __future__ import annotations

import re
from pathlib import Path

# ---------- 文本小工具 ----------

# 全角 / 各种空白统一成半角空格，正则里就能只用 " "
_SPACE_CHARS = "\u3000\u00a0\t\r\n"
# 表单里标签与值之间的填空：「项目类别：     小区、院落」里的那些空格
_LABEL_GAP = r"[ \u3000\u00a0]*"


def _norm(text: str) -> str:
    """把表格里的 <br>、全角空格、各种空白压成单个半角空格。"""
    raw = str(text or "")
    raw = raw.replace("<br>", " ").replace("\\|", "|")
    for ch in _SPACE_CHARS:
        raw = raw.replace(ch, " ")
    return re.sub(r" {2,}", " ", raw).strip()


def _clean_value(value: str) -> str:
    """取到的值再洗一遍：去首尾空格、去掉结尾的空白填充与分隔用的竖线。

    还要压掉**中文之间的多余空格**：docx 表格里一行文字换行时，
    转 Markdown 后会在词中间留下空格，实测解析出
    「成都农村商业银行股份 有限公司温江柳城支行」——一字之差，
    开票信息就是错的。英文单词之间的空格保留（如 Credit Code）。
    """
    text = _norm(value)
    text = text.strip(" |")
    # 两个中文字符（含全角标点）之间的空白压掉；英文/数字之间的不动
    text = re.sub(r"(?<=[\u4e00-\u9fa5，。、；：（）%])\s+(?=[\u4e00-\u9fa5，。、；：（）%])",
                  "", text)
    return text.strip()


def _cell_after_label(line: str, label: str) -> str:
    """从「标签：值」一行里取「值」的部分（行内第一个标签命中的那个）。

    表单里常见 `项目类别：             小区、院落（网格）营建` 这种长空白，
    也可能一行里有多个「标签：值」对（`直接服务人数 138 间接服务人数 5000`），
    此时只取 label 之后到下一个「XX：」之前的部分。
    """
    pattern = re.compile(rf"{re.escape(label)}\s*[:：]\s*")
    m = pattern.search(line)
    if not m:
        return ""
    rest = line[m.end():]
    # 下一个「标签：」= 一段以冒号结尾的短文本，后面跟着空格或另一个标签
    nxt = re.search(r"\s[\u4e00-\u9fa5]{2,8}\s*[:：]\s", rest)
    if nxt:
        rest = rest[:nxt.start()]
    return _clean_value(rest)


def _label_cell_value(md: str, label: str) -> str:
    """按标签取值：表格里就是「左边格是标签」的右邻格，正文里就是「标签：」之后。

    表单类项目书（申报书）几乎全是两列横表：`| 项目周期 | 2026年7月-2027年2月 | …`。
    只看「标签：」的正文写法会漏掉整张表，所以两种写法都要认：
      1. 表格行：某一格正好等于 label → 取它右边一格
      2. 正文行：`label：值` → 取冒号后到下一个「XX：」之前
    """
    if not label:
        return ""
    # 1) 表格行
    for line in md.splitlines():
        cells = [_clean_value(c) for c in _row_cells(line)]
        if not cells:
            continue
        for k, cell in enumerate(cells[:-1]):
            # 标签格可能带括注，如「立项社区保障资金（发包金额）」
            base = re.sub(r"[（(][^）)]*[）)]\s*$", "", cell).strip()
            if base == label and cells[k + 1]:
                return cells[k + 1]
    # 2) 正文行
    line = _line_with_label(md, label)
    if line:
        return _cell_after_label(line, label)
    return ""


def _line_with_label(md: str, label: str) -> str:
    """返回正文里第一条含该标签的段落（整段，已归一化）。找不到返回空串。"""
    needle = re.escape(label)
    for line in md.splitlines():
        if re.search(needle + r"\s*[:：]", line):
            return _norm(line)
    return ""


# ---------- 各字段抽取 ----------

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 「负责人：赖敏 18121960238」/「联系方式 18121960238」/「电话：028-1111」
_TEL_RE = re.compile(r"(?<!\d)(0\d{2,3}[-\s]?\d{7,8}|1[3-9]\d{9})(?!\d)")
# 负责人姓名：标签后紧跟 2~4 个汉字（排除「情况」这类表格字段名）
_NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")
# 表单里「负责人」后面常跟的字段名，不是人名
_NAME_STOPWORDS = {
    "情况", "姓名", "电话", "签字", "盖章", "签名", "简介", "职责", "分工",
    "联系方式", "成员", "及电话", "本人", "意见",
}
_UNIT_SUFFIX = ("委员会", "服务中心", "基金会", "服务社", "事务所", "有限公司",
                "社会组织", "研究院", "办事处", "人民政府", "党支部")


def _extract_leader(md: str) -> tuple[str, str]:
    """项目负责人：(姓名, 电话)。抓不到返回空串，调用方自己记 warning。"""
    # 候选标签：先找「项目负责人」，再退到「负责人」「联系人」
    for label in ("项目负责人", "负责人", "联系人", "联系人及电话"):
        value = _label_cell_value(md, label)
        if not value:
            continue
        # 姓名：值开头 2~4 个汉字，且不是表头词
        name = ""
        for token in re.split(r"[\s，,、;；/]+", value):
            token = token.strip()
            if not token:
                continue
            if _PHONE_RE.fullmatch(token) or _TEL_RE.fullmatch(token):
                continue
            if _NAME_RE.match(token) and token not in _NAME_STOPWORDS:
                name = token
                break
            break                      # 第一个 token 不是人名就放弃，不往后猜
        # 电话：优先手机号，其次座机
        phone = ""
        m = _PHONE_RE.search(value) or _TEL_RE.search(value)
        if m:
            phone = m.group(1)
        if name or phone:
            return name, phone
    # 「三、项目执行负责人情况」这类表里单独有「姓名」「联系方式」两行
    return "", ""


def _extract_leader_table(md: str) -> tuple[str, str]:
    """负责人表兜底：表里「姓名」格 + 「联系方式」格。

    申报书的「项目执行负责人情况」是 表头/值 交替的横表，
    形如 `| 姓名 | 赖敏 | 性别 | 男 |`，这里按关键字成对取相邻格。
    """
    name = phone = ""
    for line in md.splitlines():
        cells = _row_cells(line)
        if not cells:
            continue
        for i, cell in enumerate(cells[:-1]):
            label = _clean_value(cell)
            value = _clean_value(cells[i + 1])
            if not value:
                continue
            if not name and label == "姓名" and _NAME_RE.match(value) \
                    and re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", value):
                name = value
            if not phone and label in ("联系方式", "联系电话", "手机"):
                m = _PHONE_RE.search(value) or _TEL_RE.search(value)
                if m:
                    phone = m.group(1)
    return name, phone


def _row_cells(line: str) -> list[str]:
    """Markdown 表格行 → 单元格列表；不是表格行返回 []。"""
    body = line.strip()
    if not body.startswith("|"):
        return []
    body = body.strip("|")
    # 反转义竖线后再切，避免把 \| 当分隔符
    parts = re.split(r"(?<!\\)\|", body)
    return [part for part in parts]


def _extract_unit(md: str, label: str) -> str:
    """取单位名：表格格 / 「标签：」正文两种写法都认。"""
    value = _label_cell_value(md, label)
    if not value:
        return ""
    # 单位名到第一个空白或下一个标签为止；去掉末尾圆括注
    token = re.split(r"\s", value)[0]
    token = re.sub(r"[（(][^）)]*[）)]$", "", token)
    return token.strip()


def _split_period(value: str) -> tuple[str, str]:
    """「2026年7月-2027年2月」→ ('2026-07', '2027-02')；解析不了返回 ('','')。"""
    text = _clean_value(value)
    # 统一成 年-月 的数字对
    pairs = re.findall(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*月?", text)
    if len(pairs) < 2:
        return "", ""
    (y1, m1), (y2, m2) = pairs[0], pairs[1]
    try:
        n1, n2 = int(m1), int(m2)
    except ValueError:
        return "", ""
    if not (1 <= n1 <= 12 and 1 <= n2 <= 12):
        return "", ""
    return f"{y1}-{n1:02d}", f"{y2}-{n2:02d}"


def _month_end(year: int, month: int) -> int:
    """该月最后一天（不引 calendar，够用）。"""
    if month == 12:
        return 31
    days = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        days = 29
    return days


def _period_range(value: str) -> tuple[str, str]:
    """「2026年7月-2027年2月」→ ('2026-07-01', '2027-02-28')；解析不了返回 ('','')。"""
    start, end = _split_period(value)
    if not start or not end:
        return "", ""
    try:
        y2, m2 = int(end[:4]), int(end[5:7])
    except ValueError:
        return "", ""
    return f"{start}-01", f"{end}-{_month_end(y2, m2):02d}"


def _period_months(start: str, end: str) -> int:
    """项目周期月数（含头含尾）。解析不了返回 0。"""
    try:
        y1, m1 = int(start[:4]), int(start[5:7])
        y2, m2 = int(end[:4]), int(end[5:7])
    except (ValueError, IndexError):
        return 0
    months = (y2 - y1) * 12 + (m2 - m1) + 1
    return months if months > 0 else 0


def _extract_checked_option(md: str, labels: tuple[str, ...]) -> str:
    """单选行里取打勾的那一项：「□社区级  ☑小区（院落）、网格级」→ 勾中的那项。

    两种排版都认：
      1. 表格行：`| 项目实施级别 | □社区级   ☑小区（院落）、网格级 |`
      2. 正文：`项目实施级别：` 单独一行，勾选框在下一行
    勾中一项都没有（或不只一项）时返回空串，不猜。
    """
    lines = md.splitlines()
    for label in labels:
        for i, line in enumerate(lines):
            cells = [_clean_value(c) for c in _row_cells(line)]
            hit: list[str] = []
            if cells:
                # 表格行：左格是标签 → 只处理它右边那一格，避免扫到整行的别的选项
                for k, cell in enumerate(cells[:-1]):
                    if re.sub(r"[（(][^）)]*[）)]\s*$", "", cell).strip() == label:
                        hit = [cells[k + 1]]
                        break
            if not hit:
                if not str(line.strip()).startswith(label):
                    continue
                # 正文写法：标签行本身 + 后两行都可能有勾选框
                hit = [_norm(x) for x in lines[i: i + 3]]
            picked = [opt for opt in _checked_options(" ".join(hit)) if opt]
            # 只认恰好勾一项；勾多项说明是复选题，交给人工
            if len(picked) == 1:
                return picked[0]
    return ""


def _checked_options(block: str) -> list[str]:
    """从一段文字里取出所有被勾中的选项名（☑/√/✓ 后面紧跟的那一项）。"""
    out: list[str] = []
    # 选项之间用 □ / ☑ 分隔：先按这两种符号切开，保留符号本身
    for piece in re.split(r"(?=[□☑✓√])", block):
        piece = piece.strip()
        if not piece or piece[0] not in "\u2611\u2713\u221a":
            continue
        name = piece[1:].strip()
        name = re.split(r"[\u25a1\u2611\u2713\u221a]", name)[0].strip()
        # 去掉尾部的填空 / 提示语
        name = re.split(r"\s{2,}", name)[0].strip()
        name = name.split("（城市社区与涉农社区")[0].strip()
        if name:
            out.append(name)
    return out


def _extract_budget(md: str) -> tuple[dict, int, int, list[str]]:
    """预算：发包金额 / 报价金额 / 管理费 / 税费。

    返回 (budget dict, management_fee, tax, warnings)。
    只在「标签 + 数字」同时抓到时才写，抓不到一律给 0 + warning。
    """
    warns: list[str] = []
    budget: dict = {}

    def _amount(label: str) -> int | None:
        value = _label_cell_value(md, label)
        if not value:
            return None
        m = re.search(r"(\d[\d,]*(?:\.\d+)?)", value)
        if not m:
            return None
        try:
            return int(float(m.group(1).replace(",", "")))
        except ValueError:
            return None

    upper = _amount("立项社区保障资金")
    total = _amount("实际社区保障资金")
    if total is None:
        total = _amount("预算合计")
    self_funded = _amount("自筹资金")

    if upper is not None:
        budget["upper"] = upper
    if total is not None:
        budget["total"] = total
    if self_funded is not None:
        budget["self_funded"] = self_funded

    # 管理费 / 税费：预算表里「管理费 | / | -- | -- | 7200」这种行
    management_fee = _budget_row_amount(md, ("管理费",))
    tax = _budget_row_amount(md, ("税费", "税金"))
    if not budget:
        warns.append("预算：未识别到经费来源，budget 留空；请在项目 yaml 里手工补 budget")
    return budget, management_fee, tax, warns


def _budget_row_amount(md: str, names: tuple[str, ...]) -> int:
    """预算表里以 names 打头的行，取行内最后一个整数金额（合计列通常在最右）。"""
    for line in md.splitlines():
        cells = [_clean_value(c) for c in _row_cells(line)]
        if not cells or cells[0] not in names:
            continue
        amounts = []
        for cell in cells[1:]:
            m = re.fullmatch(r"(\d[\d,]*(?:\.\d+)?)\s*元?", cell)
            if m:
                amounts.append(m.group(1))
        if not amounts:
            continue
        try:
            return int(float(amounts[-1].replace(",", "")))
        except ValueError:
            continue
    return 0


_STRATEGY_RE = re.compile(r"(策略[一二三四五六七八九十]+)")


def _extract_indicators(md: str) -> tuple[list[dict], dict, list[str]]:
    """量化指标表 → (indicators, code_rules, warnings)。

    只认两种来源，其余一律留空：
      1. 表头含「策略 / 指标明细 / 预期量化指标」的量化指标表
      2. 预算表里的「活动」列 + 同名指标（只用来对齐名称，不造数字）

    count 只在「expected 列的数值部分 == 预算数量列」这种精确一致时才写，
    否则该指标不拆期（plan_activities 默认 count=1）。
    从 target 文本推期数是错的（engine/activities.py 有实测记录），这里不做。
    """
    warns: list[str] = []
    rows = _indicator_table_rows(md)
    if not rows:
        warns.append("指标：未识别到量化指标表，indicators 留空；"
                     "活动目录需要手工按项目书补 indicators 后才能「按指标建目录」")
        return [], {}, warns

    indicators: list[dict] = []
    # 活动类型号：按出现顺序给 1..9；超过 9 个类型时后面的不编号（编号只有一位）
    act_types: dict[str, int] = {}
    used_numbers: set[int] = set()
    overflow = False

    for strategy, name, target in rows:
        if not name:
            continue
        item: dict = {}
        if strategy:
            item["strategy"] = strategy
        item["name"] = name
        if target:
            item["target"] = target
        indicators.append(item)
        if name not in act_types:
            if len(used_numbers) < 9:
                # 找最小的未用 1..9
                for n in range(1, 10):
                    if n not in used_numbers:
                        act_types[name] = n
                        used_numbers.add(n)
                        break
            else:
                overflow = True

    if overflow:
        warns.append("code_rules.act_types：活动类型超过 9 种，超出部分未分配类型号"
                     "（编号里的类型号只有一位），请人工确认后手工补")

    return indicators, {"act_types": act_types}, warns


# 量化指标表表头：策略 / 指标明细 / 预期量化指标（三者都要有）
_INDICATOR_HEADERS = ("策略", "指标明细")
_TARGET_HEADERS = ("预期量化指标", "量化指标", "预期指标")
# 预算表表头（用来确定「表在哪结束」，避免把预算表当指标表读）
_BUDGET_HEADERS = ("活动", "费用类别")


def _header_index(cells: list[str]) -> list[str] | None:
    """表格行归一化后的表头；不是表头行返回 None。"""
    headers = [_clean_value(c).replace(" ", "") for c in cells]
    return headers or None


def _find_col(headers: list[str], names: tuple[str, ...]) -> int:
    for k, h in enumerate(headers):
        if h in names:
            return k
    return -1


def _is_separator(row: list[str]) -> bool:
    """Markdown 分隔行（| --- | --- |）：每一格都必须是 -/: 组成的。"""
    return bool(row) and all(
        re.fullmatch(r"-{2,}|-{2,}:|:-{2,}|:{1,2}-{1,}:?", (cell or "").strip())
        for cell in row)


def _table_span(lines: list[str], start: int) -> tuple[int, int]:
    """从表头行 start 起，返回这张表的 [start, end) 行区间。

    声明式切表：表到「第一张别的表头」或「连续空行」为止。
    早版本逐行读、遇到空表头行才停，结果一张「｜活动｜费用类别｜…｜」的预算表
    紧接着就被当成指标表继续读，31 条预算行全进了 indicators（2026-11-25 实测）。
    """
    end = start + 1
    blank = 0
    while end < len(lines):
        row = _row_cells(lines[end])
        if not row:
            blank += 1
            if blank >= 2:
                break
            end += 1
            continue
        blank = 0
        norm = [c.replace(" ", "") for c in _clean_value("|".join(row)).split("|")]
        # 新表头：同时含策略+指标明细（下一张指标表）或活动+费用类别（预算表）
        if (_find_col(norm, _INDICATOR_HEADERS) >= 0
                and _find_col(norm, _TARGET_HEADERS) >= 0):
            break
        if _find_col(norm, _BUDGET_HEADERS) >= 0:
            break
        end += 1
    return start, end


def _indicator_table_rows(md: str) -> list[tuple[str, str, str]]:
    """找量化指标表，返回 [(策略, 指标名, 预期量化指标)]。

    表头定位：同一张表里同时出现「策略」「指标明细」「预期量化指标」三列。
    值里的 <br> 已归一化成空格。
    """
    lines = md.splitlines()
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        cells = _row_cells(lines[i])
        headers = _header_index(cells) if cells else None
        if not headers:
            i += 1
            continue
        col_strategy = _find_col(headers, _INDICATOR_HEADERS[:1])
        col_name = _find_col(headers, ("指标明细",))
        col_target = _find_col(headers, _TARGET_HEADERS)
        if min(col_strategy, col_name, col_target) < 0:
            i += 1
            continue

        start, end = _table_span(lines, i)
        for j in range(start + 1, end):
            row = [_clean_value(c) for c in _row_cells(lines[j])]
            if not row or _is_separator(row):
                continue

            def _at(k: int) -> str:
                return row[k] if 0 <= k < len(row) else ""

            strategy_raw = _at(col_strategy)
            m = _STRATEGY_RE.search(strategy_raw)
            strategy = m.group(1) if m else ""
            # 指标名去掉全角/半角引号，便于和预算表「活动」列对齐
            name = _at(col_name).strip("“”\"'‘’「」 ")
            target = _at(col_target)
            # 策略 / 名称 / 预期指标 三列全空的是版面占位行，不是指标
            if not (strategy or name or target):
                continue
            out.append((strategy, name, target))
        i = end
    return out


# ---------- 顶层解析 ----------

# 关键字段缺一不可（缺了就不建项目，让调用方补 query 参数重试）
REQUIRED_FIELDS = ("project_name", "funder", "executer")


def markdown_to_project(md: str) -> tuple[dict, list[str]]:
    """项目书 Markdown → (项目 yaml dict, warnings)。

    yaml 结构与现有三个项目对齐：顶层 project / parties / staff / budget /
    indicators / personnel / management_fee / tax。
    返回的 dict 里 `_parsed` 放原始解析记录（写 yaml 时剔除）。
    """
    warnings: list[str] = []
    text = str(md or "")

    # ---- 项目名称 ----
    project_name = _label_cell_value(text, "项目名称")
    # 行内标签写法可能把后续标签一起吞了，再截一次
    project_name = project_name.split(" 项目类别")[0].strip()

    # ---- 项目类别 / 立项级别 ----
    category = _label_cell_value(text, "项目类别")
    category = category.split(" 立项单位")[0].strip()
    if not category:
        # 申报书里类别是勾选框，不是「项目类别：」标签
        category = _extract_checked_option(text, ("申请类别", "项目类别"))
    level = _extract_checked_option(text, ("项目实施级别", "项目级别", "实施级别"))

    # ---- 单位 ----
    funder = _extract_unit(text, "立项单位") or _extract_unit(text, "立项社区")
    executer = _extract_unit(text, "申报单位")

    # ---- 负责人 ----
    leader_name, leader_phone = _extract_leader(text)
    if not leader_name or not leader_phone:
        t_name, t_phone = _extract_leader_table(text)
        leader_name = leader_name or t_name
        leader_phone = leader_phone or t_phone

    # ---- 周期 ----
    period_start = period_end = ""
    raw_period = _label_cell_value(text, "项目周期")
    if raw_period:
        period_start, period_end = _period_range(raw_period)
    period_months = _period_months(period_start, period_end)

    # ---- 预算 ----
    budget, management_fee, tax, budget_warns = _extract_budget(text)
    warnings.extend(budget_warns)

    # ---- 机构信息（申报单位表） ----
    executer_info = _extract_executer_info(text)
    leader_extra = _extract_leader_extra(text)

    # ---- 指标 ----
    indicators, rules, indicator_warns = _extract_indicators(text)
    warnings.extend(indicator_warns)

    # ---- 立项月份：从项目周期 start 取，取不到留空 ----
    establish_month: int | None = None
    if period_start:
        try:
            establish_month = int(period_start[5:7])
        except ValueError:
            establish_month = None
    if establish_month is None:
        warnings.append("code_rules.establish_month：未从项目周期里取到立项月份，"
                        "留空；补上后才能生成活动编号")

    # ---- 组装 yaml ----
    project: dict = {
        "name": project_name,
        # short 由调用方给（目录名 / 数据集名），解析阶段先留空
        "short": "",
        "code_rules": {
            "establish_month": establish_month,
            "act_types": rules.get("act_types", {}),
        },
        "category": category,
        "level": level,
        "period": {
            "start": period_start,
            "end": period_end,
            "months": period_months,
        },
    }
    parties = {
        "funder": {"name": funder},
        "executer": {"name": executer, **executer_info},
    }
    leader: dict = {"name": leader_name, "phone": leader_phone, **leader_extra}
    data = {
        "project": project,
        "parties": parties,
        "staff": {"leader": leader},
        "budget": budget,
        "indicators": indicators,
        "personnel": [],
        "tax": tax,
        "management_fee": management_fee,
    }

    # ---- 缺失字段提示（不编造，只提示） ----
    if not project_name:
        warnings.append("项目名称：未从项目书识别到（表单里的「项目名称：」标签没找到或值为空）")
    if not category:
        warnings.append("项目类别：未识别到（既没有「项目类别：」标签，也没有勾选框）")
    if not level:
        warnings.append("级别：未识别到「项目实施级别」勾选项，level 留空")
    if not funder:
        warnings.append("立项单位：未识别到，parties.funder.name 留空")
    if not executer:
        warnings.append("申报单位：未识别到，parties.executer.name 留空")
    if not leader_name:
        warnings.append("项目负责人：未识别到姓名，staff.leader.name 留空")
    if not leader_phone:
        warnings.append("项目负责人电话：未识别到，staff.leader.phone 留空")
    if not period_start or not period_end:
        warnings.append("项目周期：未能解析出起止月份，period 留空")

    parsed = {
        "project_name": project_name,
        "category": category,
        "level": level,
        "funder": funder,
        "executer": executer,
        "leader_name": leader_name,
        "leader_phone": leader_phone,
        "period_start": period_start,
        "period_end": period_end,
        "indicator_count": len(indicators),
        "act_type_count": len(rules.get("act_types", {})),
    }
    data["_parsed"] = parsed
    return data, warnings


def _extract_executer_info(md: str) -> dict:
    """申报单位表：社会信用代码 / 法人 / 开户行 / 账号。取不到给空串。"""
    out = {
        "credit_code": "",
        "legal_person": "",
        "bank": "",
        "account": "",
    }
    # 统一社会信用代码：18 位大写字母数字，排除纯数字
    for line in md.splitlines():
        cells = _row_cells(line)
        if len(cells) < 2:
            continue
        pairs = [(_clean_value(cells[k]), _clean_value(cells[k + 1]))
                 for k in range(len(cells) - 1)]
        for label, value in pairs:
            if not value:
                continue
            if label == "社会信用代码" and not out["credit_code"]:
                m = re.search(r"[0-9A-HJ-NP-RTUWXY]{18}", value.upper())
                if m:
                    out["credit_code"] = m.group(0)
            elif label in ("机构法人代表", "法定代表人", "法人代表") \
                    and not out["legal_person"]:
                if re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", value):
                    out["legal_person"] = value
            elif label == "开户银行" and not out["bank"]:
                out["bank"] = value
            elif label == "银行账号" and not out["account"]:
                out["account"] = value.replace(" ", "")
    return out


def _extract_leader_extra(md: str) -> dict:
    """负责人表里的职务 / 职称 / 邮箱。取不到就不写这些键。"""
    extra: dict = {}
    for line in md.splitlines():
        cells = _row_cells(line)
        if len(cells) < 2:
            continue
        pairs = [(_clean_value(cells[k]), _clean_value(cells[k + 1]))
                 for k in range(len(cells) - 1)]
        for label, value in pairs:
            if not value:
                continue
            if label in ("职称/职务", "职务") and "role" not in extra:
                extra["role"] = value
            elif label == "电子信箱" and "email" not in extra:
                if "@" in value:
                    extra["email"] = value
    return extra


def convert(path: Path, to_markdown) -> str:
    """按扩展名把项目书转成 Markdown。

    to_markdown(file) 由调用方注入（api 层用 engine.markdown.docx_to_md 等），
    这样本模块不反向依赖具体转换实现，测试也只需给一个假函数。
    不支持的扩展名抛 ValueError，由调用方转 400。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in (".docx", ".pdf", ".md"):
        raise ValueError(f"项目书只支持 .docx / .pdf / .md，收到 {suffix or '（无扩展名）'}")
    return to_markdown(p)
