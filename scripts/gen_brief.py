#!/usr/bin/env python3
"""命令行：从活动计划书解析 facts，调 LLM 生成活动简报。

用法：
    python3 scripts/gen_brief.py --activity 惠民社区保障资金项目/11-共治行动-第一期 --style 务实
    python3 scripts/gen_brief.py --activity ... --dry-run    # 只打印 prompt
    python3 scripts/gen_brief.py --activity ... --out /tmp/简报.docx

facts 来源：活动目录 01-*.docx（计划书）正文；
实际人数取 06-*.docx（总结报告）的「人数」，决算金额取 08-*.docx（决算表）的合计。
解析不到的字段留空并打印警告，不中断。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from engine import llm, markdown, projects  # noqa: E402

# 简报模板（占位符：项目名称 / 年月日 / 活动名称 / 简报导语 / 诉求描述 / 达到目标）
BRIEF_TEMPLATE = Path("/srv/social-work/templates/library/简报.docx")

# facts 的字段顺序，也是 prompt 里「活动基本信息」的顺序
FACT_KEYS = ("活动名称", "活动时间", "活动地点", "服务对象", "预计人数",
             "实际人数", "问题现状", "需求来源", "回应思路", "活动目的",
             "活动目标", "环节", "决算金额", "项目名称")


# --------------------------------------------------------------------------
# 活动目录定位
# --------------------------------------------------------------------------

def _resolve_activity(raw: str) -> Path | None:
    """把命令行参数解析成活动目录：绝对/相对路径，或「项目名/活动名」简写。"""
    path = Path(raw)
    if path.is_dir():
        return path

    root = Path(projects.PROJECTS_ROOT)
    parts = list(path.parts)
    candidates = [root / path]
    if len(parts) == 2:
        candidates.append(root / parts[0] / "活动" / parts[1])
    elif len(parts) == 3 and parts[1] == "活动":
        candidates.append(root / parts[0] / "活动" / parts[2])
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _project_name(act_dir: Path) -> str:
    """活动目录 → 项目目录名（<项目>/活动/<活动> 的上两级）。"""
    return act_dir.parent.parent.name if act_dir.parent.name == "活动" else ""


# --------------------------------------------------------------------------
# 计划书解析
# --------------------------------------------------------------------------

def _first_docx(act_dir: Path, item_name: str) -> Path | None:
    """按清单项取第一个 docx，兼容有/无序号前缀的命名。

    原来只 glob「01-*.docx」，而 Web 按模板原名生成的是「计划书.docx」，
    真实活动目录里两种并存（见 engine.checks.find_first_file）。
    """
    from engine import checks as _checks
    return _checks.find_first_file(act_dir, item_name)


def _to_md(path: Path | None) -> str:
    """docx → markdown；文件缺失或转换失败返回空串。"""
    if path is None:
        return ""
    try:
        return markdown.docx_to_md(path)
    except RuntimeError as exc:
        print(f"警告：{path.name} 转文本失败：{exc}", file=sys.stderr)
        return ""


def _grab(md: str, *labels: str) -> str:
    """抓「标签：值」这类单行字段，多个候选标签按顺序尝试。"""
    for label in labels:
        match = re.search(rf"^[ \t]*{re.escape(label)}[ \t]*[：:][ \t]*(.+)$",
                          md, re.M)
        if match:
            return match.group(1).strip()
    return ""


def _line_after(md: str, title: str) -> str:
    """标题行之后的第一个非空行（活动目的这类独占一段的字段）。"""
    lines = md.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == title:
            for following in lines[index + 1:]:
                if following.strip():
                    return following.strip()
            return ""
    return ""


def _clean_item(text: str) -> str:
    """列表项去掉末尾的；。，避免「A；、B；」这种拼接。"""
    return re.sub(r"[；;。]+$", "", text.strip()).strip()


def _parse_goals(md: str) -> list[str]:
    """活动目标：标题后连续的 ①②③ 列表项。"""
    lines = md.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip() == "活动目标"), None)
    if start is None:
        return []
    goals: list[str] = []
    for line in lines[start + 1:]:
        text = line.strip()
        if not text:
            continue
        match = re.match(r"^[①-⑳]\s*(.+)$", text)
        if match:
            goals.append(_clean_item(match.group(1)))
        else:
            break                      # 序号列表结束
    return goals


def _parse_steps(md: str) -> list[str]:
    """环节：「五、活动具体安排及内容」表格的内容列，一行一个环节。"""
    lines = md.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("| 阶段")), None)
    if start is None:
        return []
    steps: list[str] = []
    for line in lines[start + 1:]:
        if not line.startswith("|"):
            break                      # 表格结束
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        if all(set(cell) <= {"-", " "} for cell in cells):
            continue                   # markdown 分隔行
        content = re.sub(r"<br\s*/?>", "", cells[2]).strip()
        if content:
            steps.append(_clean_item(content))
    return steps


def _parse_actual_count(md: str) -> int | None:
    """总结报告表格里的「人数 | 19人」。"""
    match = re.search(r"\|\s*人\s*数\s*\|\s*(\d+)\s*人?\s*\|", md)
    return int(match.group(1)) if match else None


def _parse_total_amount(md: str) -> float | None:
    """决算表「合计 | 1662（元）」。"""
    match = re.search(r"\|\s*合计\s*\|\s*([\d.]+)", md)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _project_title(project_name: str) -> str:
    """项目 yaml 里的 project.name；读不到就用目录名。

    yaml 里的 name 常写成 '"亲邻惠里…"街区活力营建项目'（内含引号），
    只剥掉成对包住整串的引号，不动中间的引号。
    """
    if not project_name:
        return ""
    try:
        data = projects.load_project(project_name)
    except (FileNotFoundError, ValueError):
        return project_name
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    title = str(project.get("name") or "").strip()
    if len(title) >= 2 and title[0] == title[-1] and title[0] in "\"'":
        title = title[1:-1]
    return title or project_name


def _cn_date(text: str) -> str:
    """2026-11-10（上午）→ 2026年11月10日（上午）；不是这个格式就原样返回。"""
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s*(.*)$", str(text).strip())
    if not match:
        return str(text).strip()
    year, month, day, suffix = match.groups()
    return f"{int(year)}年{int(month)}月{int(day)}日{suffix.strip()}"


def build_facts(act_dir: Path) -> dict:
    """活动目录 → brief 的 facts。缺的字段留空串 / None，由调用方决定是否告警。"""
    plan_md = _to_md(_first_docx(act_dir, "活动计划"))
    report_md = _to_md(_first_docx(act_dir, "总结报告"))
    budget_md = _to_md(_first_docx(act_dir, "决算表"))

    expected = _grab(plan_md, "预计参与人数", "预计人数", "参与人数")
    facts = {
        "活动名称": _grab(plan_md, "活动名称"),
        "活动时间": _cn_date(_grab(plan_md, "活动时间")),
        "活动地点": _grab(plan_md, "活动地点"),
        "服务对象": _grab(plan_md, "服务对象"),
        "预计人数": int(expected) if expected.isdigit() else expected,
        "实际人数": _parse_actual_count(report_md),
        "问题现状": _grab(plan_md, "问题现状"),
        "需求来源": _grab(plan_md, "需求来源"),
        "回应思路": _grab(plan_md, "回应思路"),
        "活动目的": _line_after(plan_md, "活动目的"),
        "活动目标": _parse_goals(plan_md),
        "环节": _parse_steps(plan_md),
        "决算金额": _parse_total_amount(budget_md),
        "项目名称": _project_title(_project_name(act_dir)),
    }
    missing = [key for key in FACT_KEYS if facts.get(key) in (None, "", [])]
    if missing:
        print(f"警告：未解析到 {len(missing)} 个字段：{'、'.join(missing)}",
              file=sys.stderr)
    return {key: facts[key] for key in FACT_KEYS}


# --------------------------------------------------------------------------
# 写回 docx
# --------------------------------------------------------------------------

def render_brief(facts: dict, result: dict, out: Path) -> None:
    """把 4 段填进 简报.docx。

    模板只有 3 个正文位：简报导语 / 诉求描述 / 达到目标。
    映射：简报导语←导语，诉求描述←目的 + 过程（模板上这格在「具体过程」标题下），
    达到目标←成效。
    """
    from engine import fill                       # 延迟导入，--dry-run 不必装 python-docx

    if not BRIEF_TEMPLATE.is_file():
        raise FileNotFoundError(f"简报模板不存在：{BRIEF_TEMPLATE}")
    data = {
        "项目名称": facts.get("项目名称") or "",
        "年月日": facts.get("活动时间") or "",
        "活动名称": facts.get("活动名称") or "",
        "简报导语": result["导语"],
        "诉求描述": f"{result['目的']}\n{result['过程']}",
        "达到目标": result["成效"],
    }
    stats = fill.render_docx(BRIEF_TEMPLATE, out, data)
    print(f"已写入 {out}（填充 {stats['filled']} 处，"
          f"未匹配 {len(stats['missing'])} 个）", file=sys.stderr)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成活动简报（LLM）")
    parser.add_argument("--activity", required=True,
                        help="活动目录（可用 项目名/活动名 简写）")
    parser.add_argument("--style", default="",
                        help="文风：务实 / 平实 / 党建；不填按项目与活动类型推断")
    parser.add_argument("--dry-run", action="store_true", help="只打印 prompt")
    parser.add_argument("--out", help="填进 简报.docx 模板后输出的路径")
    args = parser.parse_args(argv)

    act_dir = _resolve_activity(args.activity)
    if act_dir is None:
        print(f"活动目录不存在：{args.activity}", file=sys.stderr)
        return 1

    facts = build_facts(act_dir)
    style = llm.resolve_style(_project_name(act_dir), act_dir.name, args.style)
    print(f"文风：{style}", file=sys.stderr)

    if args.dry_run:
        print(llm.build_prompt("brief", facts, style))
        return 0

    try:
        result = llm.write("brief", facts, style=style)
    except llm.LLMError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 2

    check = result.get("_check") or {}
    rewrites = int(result.get("_rewrites") or 0)
    if check:
        print(f"文风自检：{check.get('score', 0)} 分（{check.get('words', 0)} 字，"
              f"{'通过' if check.get('ok') else '未通过'}，重写 {rewrites} 次）",
              file=sys.stderr)
        for issue in check.get("issues") or []:
            print(f"  未达标：{issue}", file=sys.stderr)

    # _check / _rewrites 是内部元信息，不进 JSON 正文
    print(json.dumps({key: value for key, value in result.items()
                      if not key.startswith("_")},
                     ensure_ascii=False, indent=2))

    if args.out:
        try:
            render_brief(facts, result, Path(args.out).expanduser())
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"写回 docx 失败：{exc}", file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
