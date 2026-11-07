#!/usr/bin/env python3
"""M10 图片归档端到端：文件名 → 文件命名 + 档案条目；读不出日期与单位 → _待确认。

沙箱里跑真的 `scan-inbox.py`（真配置、真规则层、真台账），只把飞书与 LLM 打桩 ——
archive_only 图片本来就不 OCR、不读正文，所以测试图用任意字节即可（不需要 PIL）。

要验的三条规则：
  1. 文件名里有日期 + 单位 → 按 `<日期>-<单位>-<内容>` 命名，产档案条目 md（喂 Cognee）；
     类别判不出来不叫 LLM（archive_only 没有正文可看，叫了也是重猜文件名）。
  2. 有单位、类别关键词不命中 → 项目照文件名定，类别给默认「日常工作佐证」。
  3. 既没日期也没单位 → 未分类落 `sources/_待确认/`，保留原名，22:00 汇总里带原因。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# 打桩 LLM 一律返回花都 —— 用它当探针：项目若变成 huimin，说明这条路径没走 LLM
STUB_LLM = {"project": "huadu", "category": "日常工作佐证", "confidence": 0.9,
            "reason": "stub", "title": ""}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"sw_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sandbox(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """造一个最小部署根：inbox + 飞书流程配置 + 假 .env。"""
    root = tmp_path / "root"
    (root / "inbox").mkdir(parents=True)
    shutil.copy(REPO / "config" / "feishu-flow.yaml.example", root / "feishu-flow.yaml")
    (root / ".env").write_text("LLM_API_KEY=sk-test\n", encoding="utf-8")
    for rel, data in files.items():
        p = root / "inbox" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def _scan(root: Path, monkeypatch, llm: dict | None = None):
    """跑一遍 scan-inbox.py（--no-cognify），返回 (模块, 返回码, LLM 被调用的文件名)。"""
    monkeypatch.setenv("SOCIAL_WORK_ROOT", str(root))
    monkeypatch.setattr(sys, "argv", ["scan-inbox.py", "--no-cognify"])
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    m = _load("scan-inbox")
    assert m.ROOT == root, "沙箱 root 没生效，跑到真目录去了"

    called: list[str] = []

    def fake_classify(*_args, **_kw):
        called.append(_args[0] if _args else "")
        return dict(llm or STUB_LLM)

    m.pull_inbox = lambda _token: []
    m.upload_to_archive = lambda rel, folder: ""
    m.classify = fake_classify
    rc = m.main()
    return m, rc, called


def _entries(root: Path) -> dict:
    data = yaml.safe_load((root / "state" / "manifest.yaml").read_text(encoding="utf-8"))
    return data["files"]


def test_image_archived_and_indexed_by_filename(tmp_path, monkeypatch):
    """文件名齐活 → 直接归档 + 产档案条目，全程不问 LLM。"""
    root = _sandbox(tmp_path, {"花都-2026年11月5日-织围巾登记-名单.jpg": b"fake-jpeg-a"})
    _, rc, called = _scan(root, monkeypatch)
    assert rc == 0
    assert called == [], "文件名判得出项目，不该再调 LLM"

    entry = next(iter(_entries(root).values()))
    assert entry["name"] == "2026-11-05-花都-织围巾登记-名单.jpg"
    assert (entry["project"], entry["category"], entry["status"]) == \
        ("huadu", "日常工作佐证", "done")
    assert entry["original"] == f"sources/projects/huadu/日常工作佐证/{entry['name']}"
    assert (root / entry["original"]).is_file()
    assert entry["media"] == f"vault-huadu/media/日常工作佐证/{entry['name']}"
    assert (root / entry["media"]).is_file()

    md = (root / entry["md"]).read_text(encoding="utf-8")
    for frag in ("# 档案条目：2026-11-05-花都-织围巾登记-名单.jpg",
                 "- 日期：2026-11-05",
                 "- 单位：花都",
                 "- 原文件名：花都-2026年11月5日-织围巾登记-名单.jpg",
                 f"- 源文件：{entry['original']}",
                 "手写资料",
                 "找原件"):
        assert frag in md, f"档案条目缺：{frag}"


def test_photo_without_category_keyword_stays_rule_based(tmp_path, monkeypatch):
    """有单位、类别关键词不命中 → 项目仍按文件名定，类别给默认值，不叫 LLM。"""
    root = _sandbox(tmp_path, {"惠民-2026年11月6日-隔离桩.jpg": b"fake-jpeg-b"})
    _, rc, called = _scan(root, monkeypatch)
    assert rc == 0
    assert called == [], "只判出项目就该收工，别为类别去调 LLM"

    entry = next(iter(_entries(root).values()))
    assert entry["project"] == "huimin", "打桩 LLM 返回 huadu，说明这里走了 LLM"
    assert entry["category"] == "日常工作佐证"
    assert entry["md"], "只存档图片也要产档案条目 md（这是喂 Cognee 的关系来源）"


def test_unreadable_filename_goes_to_pending(tmp_path, monkeypatch):
    """既没日期也没单位 → 未分类，保留原名，不产 md，不留空目录。"""
    root = _sandbox(tmp_path, {"乱七八糟.jpg": b"fake-jpeg-c"})
    _, rc, _ = _scan(root, monkeypatch)
    assert rc == 0

    entry = next(iter(_entries(root).values()))
    assert entry["project"] == "", "文件名没有日期也没有单位 → 必须未分类，不受 LLM 影响"
    assert entry["status"] == "pending_review"
    assert entry["note"] == "文件名无日期/单位"
    assert "md" not in entry
    assert entry["name"] == "乱七八糟.jpg", "未分类件保留原名，人工在 _待确认/ 里好对"
    assert entry["original"] == "sources/_待确认/日常工作佐证/乱七八糟.jpg"
    assert (root / entry["original"]).is_file()
    # 不产 md 就别在 _待确认/ 里留下空壳目录
    assert not (root / "sources" / "_待确认" / "_md" / "日常工作佐证" / "乱七八糟").exists()


def test_pending_item_shows_up_in_daily_reminder(tmp_path, monkeypatch):
    """未分类件要能被当天 22:00 的兜底清单捞到，并说明原因。"""
    root = _sandbox(tmp_path, {"乱七八糟.jpg": b"fake-jpeg-d"})
    _scan(root, monkeypatch)

    fb = _load("fallback")
    day = sorted(p.stem for p in (root / "state" / "audit").glob("*.jsonl"))[-1]
    item = next(i for i in fb.collect(root, day) if i["file"] == "乱七八糟.jpg")
    assert item["problem"] == "文件名无日期/单位", "汇总要能说出为什么落这儿"
    assert item["where"].endswith("乱七八糟.jpg"), "得给出原件在哪"
