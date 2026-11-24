"""LLM 生成前的「冷化」：从项目资料与知识库取事实，注入 prompt。

问题：原来 LLM 只拿到 activity.yaml 的几个字段（活动名/时间/人数），
生成的活动通知、计划书、简报内容全靠模型自己编，跟项目实际对不上。
用户要求「根据我填写的关键信息，通过项目书和协议 + 知识库记录的活动信息
（项目级隔离）来冷化内容」。

数据源（严格本项目）：
1. 项目 yaml：project / parties（funder、executer）
2. 项目方案目录 `projects/<项目>/方案/*.docx|md`：项目申报书、协议
3. Cognee 知识库：按本项目的数据集检索，不跨项目

三个来源都失败时返回空字符串，生成照常进行（不因冷化失败挡住主流程）。
"""
from __future__ import annotations

from pathlib import Path

# 物料上限：prompt 太长会拖慢生成且稀释重点
MAX_PROJECT_DOC_CHARS = 2500
MAX_KB_CHARS = 2000


def _read_doc_text(path: Path, limit: int) -> str:
    """读 docx / md / txt 的正文，截断到 limit 字。失败返回空串。"""
    try:
        suffix = path.suffix.lower()
        if suffix == ".md" or suffix == ".txt":
            text = path.read_text(encoding="utf-8", errors="replace")
        elif suffix == ".docx":
            from docx import Document
            doc = Document(str(path))
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
            text = "\n".join(parts)
        else:
            return ""
        return text[:limit]
    except Exception:
        return ""


def project_documents(project_root: Path,
                      max_chars: int = MAX_PROJECT_DOC_CHARS) -> list[dict]:
    """读项目级资料（方案 / 协议 / 报告），返回 [{name, text}]。

    只读 `方案/` `协议/` `报告/` 三个目录——那是项目书与协议的正规位置；
    不扫 `活动/`（那是单场活动的材料，冷化的输入不该混进产出）。
    """
    root = Path(project_root)
    if not root.is_dir():
        return []
    out: list[dict] = []
    budget = max_chars
    for sub in ("方案", "协议", "报告"):
        folder = root / sub
        if not folder.is_dir():
            continue
        # 同一份资料通常有 docx + md，优先 docx（正文更全）
        files = sorted(folder.glob("*.docx")) + sorted(folder.glob("*.md"))
        seen_stems: set[str] = set()
        for f in files:
            if f.stem in seen_stems:
                continue
            if budget <= 0:
                break
            text = _read_doc_text(f, budget)
            if not text.strip():
                continue
            seen_stems.add(f.stem)
            out.append({"name": f"{sub}/{f.name}", "text": text})
            budget -= len(text)
    return out


def knowledge_base(project_name: str, query: str,
                   max_chars: int = MAX_KB_CHARS) -> str:
    """按项目检索知识库（Cognee），返回合并后的片段文本。

    项目级隔离：只查本项目的数据集，由 search_datasets 的 datasets 参数保证。
    任何异常都吞掉返回空串——冷化是增强项，不能挡住生成。
    """
    if not query.strip() or not project_name:
        return ""
    try:
        from api.routes.misc import search_datasets
        # 项目级隔离：数据集名就是项目名，只查自己那一个
        ok, text, _timeouts = search_datasets(query, [project_name], qtype="CHUNKS")
        if not ok:
            return ""
        return str(text or "")[:max_chars]
    except Exception:
        return ""


def build_context(project_root: Path, project_name: str, query: str) -> dict:
    """汇总冷化材料：{docs: [...], kb: str, summary: str}。"""
    docs = project_documents(project_root)
    kb = knowledge_base(project_name, query)
    return {"docs": docs, "kb": kb, "summary": summarize(docs, kb)}


def summarize(docs: list[dict], kb: str) -> str:
    """给界面看的一句话摘要。"""
    parts = []
    if docs:
        parts.append(f"项目资料 {len(docs)} 份")
    if kb.strip():
        parts.append("知识库命中")
    return "、".join(parts) if parts else "无可用冷化材料"


def render_prompt_block(ctx: dict) -> str:
    """把冷化材料拼成 prompt 片段。没有材料返回空串。"""
    docs = ctx.get("docs") or []
    kb = str(ctx.get("kb") or "").strip()
    if not docs and not kb:
        return ""
    lines = ["【项目事实（生成内容必须以此为据，不要编造）】"]
    for doc in docs:
        lines.append(f"--- {doc['name']} ---")
        lines.append(doc["text"].strip())
        lines.append("")
    if kb:
        lines.append("--- 知识库检索片段 ---")
        lines.append(kb)
        lines.append("")
    lines.append("要求：活动背景、目的、目标、宣传方式等必须能从上面找到依据；"
                 "上面没写的具体人名、金额、政策文号一律不要编。")
    return "\n".join(lines)
