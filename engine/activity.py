#!/usr/bin/env python3
"""活动数据装载层：把项目 yaml + activity.yaml + 用户输入合成填充数据。

产出的 dict 的 key 就是模板里的占位符名字（不带花括号），可直接交给
engine.fill.render_* 使用。缺的字段留 None，不抛错——让 fill 的 missing
列表告诉用户还缺什么，而不是在建数据阶段就中断。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import projects
from .ingest import read_activity_yaml

# 中国标准时间（UTC+8），服务器时区不可靠时也能拿到当天日期
CST = timezone(timedelta(hours=8))

# 必须出现在返回结果里的 key；顺序即文档顺序
REQUIRED_KEYS = (
    "项目名称", "活动名称", "活动时间", "活动地点", "服务对象", "预计人数",
    "实际人数", "人手分配", "项目社工", "项目负责人", "执行社工团队", "项目编号", "年月日",
    "填表日期", "执行社工", "出纳", "经办人", "收款单位", "帐号", "开户行",
    "费用归属部门", "领取人数", "测评人数", "总得分", "满意率", "平均分",
    "评定等级",
)

# 计划书里的多行表格：LLM 给的对象数组 → 模板里的 {{前缀_N}}。
# columns 是「元素子字段 → 占位符前缀」，rows 是模板固定行数（超出的截断）。
PLAN_ROW_SPECS = {
    "环节": {
        "columns": {"内容": "环节内容", "物资": "环节物资", "人员": "环节人员"},
        "rows": 5,
    },
    "预计困难": {
        "columns": {"困难": "预计困难", "方案": "解决方案"},
        "rows": 2,
    },
}


# 模板里人数类占位符自带单位（如「人    数：{{实际人数}} 人」），
# 如果 activity.yaml 写到「14人」，两边一拼就成「14人 人」（真实 E2E 实测）。
# 这里把值里**冗余的人/人次单位**剥掉，让模板的统一单位生效。
_UNIT_TAIL = re.compile(r"\s*(?:人次|人|名)\s*$")


def _count_text(value) -> str | None:
    """人数类字段：去掉尾部单位，只留数字/区间。

    只在**剥完仍有阿拉伯数字**时才剥：「14人」→「14」，
    但「十人」→ 保持「十人」（原来是「十」，把中文数字吃掉了）。
    「8-10人」这种区间保留「8-10」。
    """
    text = _text(value)
    if text is None:
        return None
    stripped = _UNIT_TAIL.sub("", text)
    if not stripped or not any(ch.isdigit() for ch in stripped):
        return text
    return stripped


def _text(value) -> str | None:
    """空值统一成 None，其余转字符串（编号这类字段 yaml 里可能写成数字）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _today_text() -> str:
    """当天 CST 日期，格式 2026年11月18日。"""
    now = datetime.now(CST)
    return f"{now.year}年{now.month}月{now.day}日"


def _name_from_dir(dirname: str) -> str | None:
    """从活动目录名反推活动名称。

    「11-共治行动-第一期」→「共治行动（第一期）」
    「01-走访调研-第一期」→「走访调研（第一期）」
    """
    raw = re.sub(r"^\d+[-_]", "", str(dirname or "").strip())
    if not raw:
        return None
    match = re.match(r"^(.*?)[-_]第(.+期)$", raw)
    if match:
        return f"{match.group(1)}（第{match.group(2)}）"
    return raw


def _resolve_activity_dir(project_name: str, activity_dir: str) -> Path:
    """活动目录定位：支持绝对路径、目录名，也容忍「项目名/目录名」写法。"""
    raw = str(activity_dir if activity_dir is not None else "").strip()
    if not raw:
        raise ValueError("活动目录不能为空")

    path = Path(raw)
    if path.is_absolute():
        return path

    parts = list(path.parts)
    if len(parts) > 1 and parts[0] == project_name:
        parts = parts[1:]
    if not parts:
        raise ValueError(f"活动目录不能只写项目名：{raw}")
    return projects.activity_dir(project_name, str(Path(*parts)))


def _load_project(project_name: str) -> dict:
    """读项目 yaml；项目目录或 yaml 不存在时返回空 dict（字段留 None）。"""
    try:
        return projects.load_project(project_name)
    except FileNotFoundError:
        return {}


def _project_code(project_name: str) -> str | None:
    """项目 yaml 里已写回的 project.code（通常是空串）。"""
    try:
        return _text(projects.project_meta(project_name).get("code"))
    except (FileNotFoundError, ValueError):
        return None


def _as_int(value) -> int | None:
    """把「3」「3人」这类值转成整数；转不了返回 None。"""
    match = re.match(r"^\s*(\d+)", str(value if value is not None else ""))
    return int(match.group(1)) if match else None


def _count_names(text) -> int | None:
    """从「①聂聃（…）；②刘佳（…）；③史佳红（…）」里数出人名个数。

    真正的执行团队常写在 activity.yaml 的「执行社工团队」里，
    比项目 staff 岗位数准（一项目多岗、单场活动只来部分人）。
    数不出来返回 None，让调用方退回下一级。
    """
    raw = _text(text)
    if not raw:
        return None
    # 圈码 ①②③… 是最可靠的段落标记
    circled = re.findall(r"[①-⑳]", raw)
    if circled:
        return len(circled)
    # 退而数「姓名（」的个数
    found = re.findall(r"([\u4e00-\u9fa5]{2,4})\s*[（(]", raw)
    return len(found) or None


def _staff_count(staff: dict) -> int:
    """数项目 yaml staff 段里有姓名的岗位数：leader / social_worker … 各算 1 人。

    兼容 staff 写成列表的形式；一个都数不出来时按 1 人兜底，避免填出「0名项目社工」。
    """
    if isinstance(staff, list):
        count = sum(1 for item in staff
                    if isinstance(item, dict) and _text(item.get("name")))
        return count or 1
    count = sum(1 for item in staff.values()
                if isinstance(item, dict) and _text(item.get("name")))
    return count or 1


def _staffing_text(staff: dict, data: dict) -> str:
    """推导 {{人手分配}}：「3名项目社工，2个社区网格、0个微网格」。

    社工人数优先取活动情况（extra / activity.yaml 的 社工人数），否则按项目
    yaml 的 staff 岗位数；网格数只有明确给了才拼上，没给就只写社工。
    """
    # 人数优先级：显式给的 > 执行社工团队里数出的姓名数 > 项目 staff 岗位数
    count = (_as_int(data.get("社工人数")) or _as_int(data.get("项目社工人数"))
             or _count_names(data.get("执行社工团队"))
             or _staff_count(staff))
    text = f"{count}名项目社工"
    grids = _as_int(data.get("社区网格数"))
    micro = _as_int(data.get("微网格数"))
    if grids is not None:
        text += f"，{grids}个社区网格"
    if micro is not None:
        text += f"、{micro}个微网格"
    return text


def expand_plan_rows(data: dict, plan: dict,
                     notes: list[str] | None = None) -> None:
    """把 plan 的 环节 / 预计困难 展开成 环节内容_1… 这类带序号的值。

    就地写入 data：模板每行都占位，所以行数固定——条目不足补空串（避免文档里
    留下 {{环节内容_4}} 这类原始占位符），超出模板行数的截断并把说明追加到 notes。
    """
    for key, spec in PLAN_ROW_SPECS.items():
        rows = plan.get(key)
        if not isinstance(rows, (list, tuple)):
            continue
        limit = spec["rows"]
        if len(rows) > limit:
            note = (f"{key}共 {len(rows)} 条，模板只有 {limit} 行，"
                    f"已截断后 {len(rows) - limit} 条")
            if notes is not None:
                notes.append(note)
            else:
                print(f"警告：{note}", file=sys.stderr)

        for seq in range(1, limit + 1):
            item = rows[seq - 1] if seq <= len(rows) else {}
            if not isinstance(item, dict):
                item = {}
            for src, prefix in spec["columns"].items():
                data[f"{prefix}_{seq}"] = _text(item.get(src)) or ""


def build_data(project_name: str, activity_dir: str,
               extra: dict | None = None, notes: list[str] | None = None,
               user: dict | None = None) -> dict:
    """合成填充数据，返回的 key 即占位符名字（不带 {{}}）。

    来源优先级：extra > activity.yaml > 项目 yaml > 目录名推断 > None。
    extra 里的「环节」「预计困难」按 PLAN_ROW_SPECS 展开；截断说明追加到 notes。
    user 传入时，用其 real_name 填「归档人」；脚本调用没有 user 就留空。
    """
    project = _load_project(project_name)
    act_dir = _resolve_activity_dir(project_name, activity_dir)
    activity = read_activity_yaml(act_dir) if act_dir.is_dir() else {}

    proj = project.get("project") if isinstance(project.get("project"), dict) else {}
    parties = project.get("parties") if isinstance(project.get("parties"), dict) else {}
    staff = project.get("staff") if isinstance(project.get("staff"), dict) else {}
    executer = parties.get("executer") if isinstance(parties.get("executer"), dict) else {}
    leader = staff.get("leader") if isinstance(staff.get("leader"), dict) else {}

    today = _today_text()
    data = {
        # 项目 yaml
        "项目名称": _text(proj.get("name")) or project_name,
        # 执行社工与项目负责人是两个角色：leader 是负责人，不是执行社工。
        # 执行社工优先取 activity.yaml（每场活动可能不同），没配才退回 leader 以外的社工，
        # 最后才用 leader 兜底（单人社工站的常见情形）。
        "项目社工": (_text(activity.get("执行社工"))
                     or _text((staff.get("social_worker") or {}).get("name"))
                     or _text(leader.get("name"))),
        "项目负责人": (_text(activity.get("项目负责人"))
                       or _text(leader.get("name"))),
        "帐号": _text(executer.get("account")),
        "开户行": _text(executer.get("bank")),
        "收款单位": _text(executer.get("name")),
        # activity.yaml
        "项目编号": (_text(activity.get("code")) or _text(activity.get("项目编号"))
                     or _project_code(project_name)),
        "活动名称": (_text(activity.get("活动名称")) or _text(activity.get("name"))
                     or _name_from_dir(act_dir.name)),
        "活动时间": (_text(activity.get("活动时间")) or _text(activity.get("time"))
                     or _text(activity.get("date"))),
        "活动地点": _text(activity.get("活动地点")) or _text(activity.get("place")),
        "服务对象": _text(activity.get("服务对象")),
        "预计人数": _count_text(activity.get("预计人数")),
        "实际人数": _count_text(activity.get("实际人数")),
        "执行社工": _text(activity.get("执行社工")),
        "执行社工团队": _text(activity.get("执行社工团队")),
        "出纳": _text(activity.get("出纳")),
        "经办人": _text(activity.get("经办人")),
        "费用归属部门": _text(activity.get("费用归属部门")),
        "领取人数": _count_text(activity.get("领取人数")),
        "测评人数": _count_text(activity.get("测评人数")),
        "总得分": _text(activity.get("总得分")),
        "满意率": _text(activity.get("满意率")),
        "平均分": _text(activity.get("平均分")),
        "评定等级": _text(activity.get("评定等级")),
        # 日期：当天
        "年月日": today,
        "填表日期": today,
        # 归档人：调用方传了登录用户就取真实姓名，脚本调用没有就留空
        "归档人": _text((user or {}).get("real_name")),
    }

    # 契约兜底：即使上面漏写，也保证 REQUIRED_KEYS 里的 key 都存在
    for key in REQUIRED_KEYS:
        data.setdefault(key, None)

    if extra:
        for key, value in extra.items():
            data[key] = value
            if key in PLAN_ROW_SPECS:
                # 结构化多行由 expand_plan_rows 展开成 环节内容_1…，不走通用列表展开
                continue
            # 列表值展开成「字段_1」「字段_2」……，供多行表格占位符按行取值；
            # 原列表 key 保留，兼容无序号占位符。显式传入的 X_1 不被覆盖。
            if isinstance(value, (list, tuple)):
                for seq, item in enumerate(value, start=1):
                    data.setdefault(f"{key}_{seq}", item)
        expand_plan_rows(data, extra, notes)

    # 人手分配要用到 extra / activity.yaml 里的社工人数与网格数，所以放在合并之后推导；
    # extra 显式给了就尊重显式值
    data["人手分配"] = data.get("人手分配") or _staffing_text(staff, data)
    return data
