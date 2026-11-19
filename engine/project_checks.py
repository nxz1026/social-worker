#!/usr/bin/env python3
"""项目级核对：依据《项目管理目录.docx》的 19 项，扫项目根下的四个资料目录。

项目资料按用途分放在 方案 / 协议 / 报告 / 其他 四个子目录里，
文件名不要求带编号，所以这里用关键词做子串匹配，递归扫描四棵目录树。

- 命中关键词 → ok
- 一个都没命中 → missing
- 命中文件的名字里含「不适用」→ na

返回结构与 engine.checks.check() 一致，方便前端复用同一套渲染。
"""
from __future__ import annotations

from pathlib import Path

PROJECT_CHECKLIST = [
    {"no": 1, "name": "项目协议",       "keywords": ["协议"]},
    {"no": 2, "name": "项目方案书",     "keywords": ["方案书", "申报书"]},
    {"no": 3, "name": "项目人员劳动合同", "keywords": ["劳动合同"]},
    {"no": 4, "name": "项目管理制度",   "keywords": ["管理制度"]},
    {"no": 5, "name": "项目立项通知",   "keywords": ["立项通知"]},
    {"no": 6, "name": "项目变更申请表", "keywords": ["变更申请"]},
    {"no": 7, "name": "项目进度管理表", "keywords": ["进度管理"]},
    {"no": 8, "name": "项目资源信息库", "keywords": ["资源信息库"]},
    {"no": 9, "name": "项目服务对象数据库", "keywords": ["服务对象数据库"]},
    {"no": 10, "name": "督导记录表",    "keywords": ["督导记录"]},
    {"no": 11, "name": "项目沟通记录表", "keywords": ["沟通记录"]},
    {"no": 12, "name": "活动物资采购协议书", "keywords": ["采购协议"]},
    {"no": 13, "name": "项目服务相关方评价表", "keywords": ["相关方评价"]},
    {"no": 14, "name": "媒体宣传信息清单", "keywords": ["媒体宣传", "宣传清单"]},
    {"no": 15, "name": "项目交流台账",   "keywords": ["交流台账"]},
    {"no": 16, "name": "项目案例",       "keywords": ["案例"]},
    {"no": 17, "name": "项目中期（结项）自评报告", "keywords": ["自评报告"]},
    {"no": 18, "name": "保障资金项目季度汇报", "keywords": ["季度汇报"]},
    {"no": 19, "name": "项目结项申请",   "keywords": ["结项申请"]},
]

# 项目资料只在这四个子目录里找；活动目录不参与项目级核对
PROJECT_SUBDIRS = ("方案", "协议", "报告", "其他")

NA_MARK = "不适用"

STATUS_TEXT = {"ok": "齐", "partial": "待补", "missing": "缺", "na": "不适用"}


def _project_files(project_dir: Path) -> list[Path]:
    """递归收集四个资料子目录里的文件，跳过隐藏目录（如 .trash）。"""
    out: list[Path] = []
    for name in PROJECT_SUBDIRS:
        base = project_dir / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(project_dir).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            out.append(path)
    return sorted(out, key=lambda p: p.as_posix())


def check_project(project_dir: Path) -> dict:
    """核对一个项目的 19 项资料，返回逐项结果 + 计数 + 是否完整。

    只扫 方案 / 协议 / 报告 / 其他 四个子目录（递归）；
    文件名包含 keywords 里任一关键词即算命中，文件名含「不适用」则判 na。
    """
    root = Path(project_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"项目目录不存在：{root}")

    files = _project_files(root)
    items: list[dict] = []

    for item in PROJECT_CHECKLIST:
        hits = [p for p in files
                if any(keyword in p.name for keyword in item["keywords"])]
        if any(NA_MARK in p.name for p in hits):
            status, detail = "na", "文件名标注不适用"
        elif hits:
            status = "ok"
            detail = f"命中 {hits[0].relative_to(root).as_posix()}"
        else:
            status = "missing"
            detail = f"未找到关键词：{'/'.join(item['keywords'])}"
        items.append({"no": item["no"], "name": item["name"],
                      "status": status, "detail": detail})

    ok_count = sum(1 for it in items if it["status"] == "ok")
    partial_count = sum(1 for it in items if it["status"] == "partial")
    missing_count = sum(1 for it in items if it["status"] == "missing")
    na_count = sum(1 for it in items if it["status"] == "na")
    return {
        "items": items,
        "ok_count": ok_count,
        "partial_count": partial_count,
        "missing_count": missing_count,
        "na_count": na_count,
        "complete": partial_count == 0 and missing_count == 0,
    }


def _cell(text: str) -> str:
    """表格单元格转义：竖线会把 Markdown 表格撑破。"""
    return str(text).replace("|", "\\|")


def to_markdown(result: dict) -> str:
    """项目级核对结果转 Markdown，方便直接贴进报告。"""
    lines = [
        "| 序号 | 材料 | 状态 | 说明 |",
        "|---|---|---|---|",
    ]
    for item in result["items"]:
        lines.append("| {} | {} | {} | {} |".format(
            item["no"], _cell(item["name"]),
            STATUS_TEXT.get(item["status"], item["status"]),
            _cell(item["detail"]),
        ))
    verdict = "完整" if result["complete"] else "未完整"
    lines.append("")
    lines.append(f"**汇总**：齐 {result['ok_count']} 项、"
                 f"缺 {result['missing_count']} 项、"
                 f"不适用 {result['na_count']} 项；{verdict}")
    return "\n".join(lines) + "\n"
