#!/usr/bin/env python3
"""满意度汇总：先列照片录分数，再统计并填 05-满意度测评汇总表。

用法：
    # 1. 列照片清单，生成/更新 scores.json（分数留空等你手填）
    python3 scripts/satisfaction.py --activity 惠民社区保障资金项目/11-共治行动-第一期 --list

    # 2. 改完 scores.json 后统计并填汇总表
    python3 scripts/satisfaction.py --activity ... --fill
    python3 scripts/satisfaction.py --activity ... --fill --out /tmp/汇总.docx

为什么不做 OCR：问卷是手写体，识别不可靠，宁可让社工对着照片逐个录分。

scores.json 是数组，一条对应一张问卷照片：
    [{"file": "微信图片_xxx.jpg", "score": 92, "satisfied": true}, ...]
- score：问卷第 9 题「满意度（0-100 分）」的分数，必须填
- satisfied：可选，「主题/内容」「服务形式」「社工表现」三题是否都选了满意及以上；
  不填时按 score >= 80（良好及以上）视同三题均满意，统计时会说明口径
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from engine import activity as activity_engine  # noqa: E402
from engine import fill  # noqa: E402
from scripts import gen_brief as brief  # noqa: E402
from scripts import gen_summary as summary  # noqa: E402

# 问卷照片目录 / 分数文件 / 汇总表模板
SURVEY_SUBDIR = "04-满意度调查表资料"
SCORES_NAME = "scores.json"
SUMMARY_TEMPLATE = Path("/srv/social-work/templates/library/满意度测评汇总表.docx")
SUMMARY_NAME = "05-满意度测评汇总表_已填.docx"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
# 未单独录入 satisfied 时，这个分数及以上视同「三题均满意」
SATISFIED_SCORE = 80


# --------------------------------------------------------------------------
# 清单与分数
# --------------------------------------------------------------------------

def list_photos(act_dir: Path) -> list[Path]:
    """问卷照片：<活动>/04-满意度调查表资料/ 下的图片，按文件名排序。"""
    folder = Path(act_dir) / SURVEY_SUBDIR
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.rglob("*")
                  if path.is_file() and not path.name.startswith(".")
                  and path.suffix.lower() in IMAGE_EXTS)


def scores_path(act_dir: Path) -> Path:
    """分数文件固定在问卷资料目录，和照片放一起。"""
    return Path(act_dir) / SURVEY_SUBDIR / SCORES_NAME


def _read_scores(path: Path) -> list[dict]:
    """读已有的 scores.json；文件不存在或格式不对返回空列表。"""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def build_scores(act_dir: Path) -> list[dict]:
    """按当前照片生成清单，保留已录入的分数。

    已存在 scores.json 时以它为准：老条目的分数不覆盖，新照片补在末尾，
    照片已删除的条目也保留（避免误删手录数据，统计时按文件是否还在过滤）。
    """
    existing = {str(item.get("file")): item for item in _read_scores(scores_path(act_dir))}
    entries: list[dict] = []
    for photo in list_photos(act_dir):
        old = existing.pop(photo.name, None)
        entries.append(old or {"file": photo.name, "score": None, "satisfied": None})
    # 已不在目录里的老条目放回末尾，提示用户核对
    entries.extend(existing.values())
    return entries


def write_scores(act_dir: Path) -> tuple[list[dict], Path]:
    """写 scores.json 并返回清单与路径。"""
    entries = build_scores(act_dir)
    path = scores_path(act_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return entries, path


def load_scores(act_dir: Path) -> list[dict]:
    """读 scores.json；不存在返回空列表。"""
    return _read_scores(scores_path(act_dir))


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------

def grade(average: float | None) -> str:
    """评定等级。口径与模板脚注一致（五档）：
    优秀 90-100、良好 80-89、一般 70-79、合格 60-69、不合格 60 分以下。
    """
    if average is None:
        return ""
    if average >= 90:
        return "优秀"
    if average >= 80:
        return "良好"
    if average >= 70:
        return "一般"
    if average >= 60:
        return "合格"
    return "不合格"


def _score_of(entry: dict) -> float | None:
    """取一条的有效分数：非数字 / 布尔 / 空值都不算。"""
    value = entry.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _satisfied(entry: dict, score: float) -> bool:
    """三题是否均满意：显式给了 satisfied 就用它，否则按分数 ≥80 推定。"""
    flag = entry.get("satisfied")
    if isinstance(flag, bool):
        return flag
    return score >= SATISFIED_SCORE


def summarize(entries: list[dict]) -> dict:
    """统计总得分 / 份数 / 平均分 / 满意率 / 评定等级。

    只统计录了有效分数的条目；一份都没录时各值为 None 并给出 issues。
    """
    valid = [(entry, _score_of(entry)) for entry in entries]
    valid = [(entry, score) for entry, score in valid if score is not None]

    issues: list[str] = []
    skipped = len(entries) - len(valid)
    if skipped:
        issues.append(f"{skipped} 条未录分数，已跳过")
    if not valid:
        return {"份数": 0, "总得分": None, "平均分": None,
                "满意率": "", "评定等级": "", "issues": issues or ["没有可统计的分数"]}

    total = round(sum(score for _, score in valid), 2)
    count = len(valid)
    average = round(total / count, 2)
    hit = sum(1 for entry, score in valid if _satisfied(entry, score))
    rate = f"{hit / count * 100:.0f}%（{hit}/{count}）"

    return {
        "份数": count,
        "总得分": total,
        "平均分": average,
        "满意率": rate,
        "评定等级": grade(average),
        "issues": issues,
    }


# --------------------------------------------------------------------------
# 填汇总表
# --------------------------------------------------------------------------

def _facts(act_dir: Path) -> dict:
    """汇总表抬头：优先活动目录里的计划书，缺的用 activity.build_data 兜底。"""
    facts = summary.build_facts(act_dir)
    project_name = brief._project_name(act_dir)
    if project_name:
        try:
            base = activity_engine.build_data(project_name, str(act_dir))
        except (FileNotFoundError, ValueError):
            base = {}
        for key in ("项目名称", "活动名称", "活动时间", "活动地点",
                    "项目社工", "实际人数", "年月日"):
            if not facts.get(key) and base.get(key):
                facts[key] = base[key]
    return facts


def render_summary(stats: dict, facts: dict, dst: Path) -> dict:
    """把统计结果填进 05-满意度测评汇总表.docx。"""
    if not SUMMARY_TEMPLATE.is_file():
        raise FileNotFoundError(f"满意度汇总表模板不存在：{SUMMARY_TEMPLATE}")

    data = {
        "项目名称": facts.get("项目名称") or "",
        "活动名称": facts.get("活动名称") or "",
        "活动时间": facts.get("活动时间") or "",
        "活动地点": facts.get("活动地点") or "",
        "项目社工": facts.get("项目社工") or "",
        "实际人数": facts.get("实际人数") or "",
        "年月日": facts.get("年月日") or facts.get("活动时间") or "",
        "测评人数": stats["份数"],
        "总得分": f"{stats['总得分']:g} 分" if stats["总得分"] is not None else "",
        "平均分": f"{stats['平均分']:.2f}" if stats["平均分"] is not None else "",
        "满意率": stats["满意率"],
        "评定等级": stats["评定等级"],
    }
    return fill.render_docx(SUMMARY_TEMPLATE, dst, data)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="满意度分数录入与统计")
    parser.add_argument("--activity", required=True,
                        help="活动目录（可用 项目名/活动名 简写）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true",
                       help="列出问卷照片，生成/更新 scores.json")
    group.add_argument("--fill", action="store_true",
                       help="按 scores.json 统计并填汇总表")
    parser.add_argument("--out", help=f"汇总表输出路径，默认 <活动>/{SUMMARY_NAME}")
    args = parser.parse_args(argv)

    act_dir = brief._resolve_activity(args.activity)
    if act_dir is None:
        print(f"活动目录不存在：{args.activity}", file=sys.stderr)
        return 1

    if args.list:
        entries, path = write_scores(act_dir)
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        print(f"\n已写入 {path}（{len(entries)} 条），改完分数后加 --fill 统计",
              file=sys.stderr)
        return 0

    entries = load_scores(act_dir)
    if not entries:
        print(f"没有分数文件：{scores_path(act_dir)}；先跑 --list", file=sys.stderr)
        return 1

    stats = summarize(entries)
    for issue in stats["issues"]:
        print(f"提示：{issue}", file=sys.stderr)
    if not stats["份数"]:
        print("一份分数都没录，无法统计", file=sys.stderr)
        return 3

    dst = Path(args.out).expanduser() if args.out else act_dir / SUMMARY_NAME
    try:
        result = render_summary(stats, _facts(act_dir), dst)
    except FileNotFoundError as exc:
        print(f"填汇总表失败：{exc}", file=sys.stderr)
        return 2

    print(json.dumps({key: stats[key] for key in
                      ("份数", "总得分", "平均分", "满意率", "评定等级")},
                     ensure_ascii=False, indent=2))
    print(f"\n已写入 {dst}（填充 {result['filled']} 处，"
          f"未匹配 {len(result['missing'])} 个）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
