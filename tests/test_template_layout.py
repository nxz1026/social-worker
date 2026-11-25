"""模板页眉/档案编号折行回归测试。

跑法：
    python3 -m pytest tests/test_template_layout.py -q

依赖真实模板目录 /srv/social-work/templates/library/。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest
from lxml import etree

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TEMPLATES = Path(os.environ.get("SOCIAL_WORK_TEMPLATES",
                                "/srv/social-work/templates/library"))

# 整个文件都依赖真实模板资产；缺目录时全部跳过，不能 fail。
pytestmark = pytest.mark.skipif(
    not TEMPLATES.is_dir(), reason=f"缺少真实模板目录 {TEMPLATES}")

sys.path.insert(0, str(SCRIPTS))
import fix_template_layout as fix  # noqa: E402
import verify_template_layout as verify  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_fix(template_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "fix_template_layout.py"),
         "--templates-dir", str(template_dir), *extra],
        cwd=str(REPO), text=True, capture_output=True, check=True,
    )


def _mutate_to_old_layout(src: Path, dst: Path) -> None:
    """把一份已修好的模板退回旧版：页眉 58 空格、编号 32 空格 + sz36。"""
    shutil.copy2(src, dst)
    with zipfile.ZipFile(dst, "r") as zin:
        parts = {name: zin.read(name) for name in zin.namelist()}

    header = etree.fromstring(parts["word/header1.xml"])
    for p in header.iter(f"{W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{W}t"))
        if "{{项目名称}}" in text:
            for node in p.iter(f"{W}t"):
                if "{{项目名称}}" in (node.text or ""):
                    node.text = " " * 58 + "{{项目名称}}"
                    node.set(XML_SPACE, "preserve")
                    break
            break
    parts["word/header1.xml"] = etree.tostring(
        header, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    document = etree.fromstring(parts["word/document.xml"])
    for p in document.iter(f"{W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{W}t"))
        if "档案编号" in text and "{{项目编号}}" in text:
            for node in p.iter(f"{W}t"):
                if "{{项目编号}}" in (node.text or ""):
                    node.text = " " * 32 + (node.text or "").lstrip(" ")
                    node.set(XML_SPACE, "preserve")
                    break
            for run in p.iter(f"{W}r"):
                rpr = run.find(f"{W}rPr")
                if rpr is None:
                    continue
                sz = rpr.find(f"{W}sz")
                if sz is not None:
                    sz.set(f"{W}val", "36")
            break
    parts["word/document.xml"] = etree.tostring(
        document, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    tmp = dst.with_name(dst.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, payload in parts.items():
            zout.writestr(name, payload)
    tmp.replace(dst)


@pytest.fixture(scope="module")
def layout_items():
    return verify.scan_templates(TEMPLATES)


def test_longest_project_name_is_29_chars():
    assert len(verify.LONGEST_PROJECT_NAME) == 29


def test_no_excessive_leading_spaces(layout_items):
    assert layout_items
    bad = [
        f"{item.template} {item.part} p{item.paragraph}: {item.leading}"
        for item in layout_items if item.leading > 8
    ]
    assert not bad, "页眉/编号前导空格超过 8：\n" + "\n".join(bad)


def test_header_has_no_padding_spaces(layout_items):
    """页眉不得用连续空格凑位置（2026-11-25 用户要求）。

    历史做法是「前导 8 个空格 + 项目名」，长项目名会溢出 487pt；
    现在统一改成「logo / 制表位 / 右对齐」，前导空格必须为 0。
    这里仍保留「页眉存在」的检查，防止模板丢页眉。
    """
    expected = {
        ("决算表.docx", "word/header1.xml"),
        ("志愿者补贴领取表.docx", "word/header1.xml"),
        ("总结报告.docx", "word/header2.xml"),
        ("总结报告.docx", "word/header1.xml"),
        ("活动目录.docx", "word/header1.xml"),
        ("满意度测评汇总表.docx", "word/header2.xml"),
        ("物资领取表.docx", "word/header1.xml"),
        ("简报.docx", "word/header2.xml"),
        ("简报.docx", "word/header1.xml"),
        ("计划书.docx", "word/header1.xml"),
        ("项目管理目录.docx", "word/header1.xml"),
        ("活动通知.docx", "word/header1.xml"),
        ("签到表.docx", "word/header1.xml"),
    }
    found = {
        (item.template, item.part): item
        for item in layout_items if item.kind == "页眉"
    }
    missing = expected - set(found)
    assert not missing, f"缺少页眉：{sorted(missing)}"
    wrong = {
        key: found[key].leading for key in expected if found[key].leading != 0
    }
    assert not wrong, f"页眉仍有凑位空格（应为 0）：{wrong}"


def test_archive_number_font_is_12pt(layout_items):
    archive = [item for item in layout_items if item.kind == "档案编号"]
    assert archive
    bad = {
        f"{item.template} {item.part} p{item.paragraph}": item.sizes
        for item in archive if any(sz != "24" for sz in item.sizes)
    }
    assert not bad, f"档案编号字号不是小四（12pt / 24 半磅）：{bad}"


def test_archive_number_spaces(layout_items):
    """档案编号必须右对齐（不再靠空格顶到右边）。

    2026-11-25 用户要求：「有的模板的档案编号没有靠最右，并且字号太大，
    用小四即可」。所以编号行前导空格必须为 0，且段落右对齐。
    """
    expected = {
        ("劳务领取表.docx", "word/document.xml"),
        ("志愿者补贴领取表.docx", "word/document.xml"),
        ("物资领取表.docx", "word/document.xml"),
        ("计划书.docx", "word/document.xml"),
        ("项目管理目录.docx", "word/document.xml"),
        ("活动目录.docx", "word/document.xml"),
        ("签到表.docx", "word/document.xml"),
    }
    found = {
        (item.template, item.part): item
        for item in layout_items if item.kind == "档案编号"
    }
    missing = expected - set(found)
    assert not missing, f"缺少档案编号：{sorted(missing)}"
    wrong = {
        key: found[key].leading for key in expected if found[key].leading != 0
    }
    assert not wrong, f"档案编号仍有凑位空格（应为 0）：{wrong}"


def test_archive_number_is_right_aligned():
    """档案编号段落必须是右对齐（jc=right），不靠空格。"""
    from lxml import etree as _etree
    import zipfile as _zipfile

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    bad = []
    for path in sorted(TEMPLATES.glob("*.docx")):
        with _zipfile.ZipFile(path) as zf:
            if "word/document.xml" not in zf.namelist():
                continue
            root = _etree.fromstring(zf.read("word/document.xml"))
        for p in root.iter(f"{W}p"):
            text = "".join(t.text or "" for t in p.iter(f"{W}t"))
            if not re.match(r"^[ \u3000\t]*(?:档案编号|项目编号)", text):
                continue
            if "{{" not in text:
                continue
            jc = p.find(f"{W}pPr/{W}jc")
            val = jc.get(f"{W}val") if jc is not None else None
            if val != "right":
                bad.append(f"{path.name}: jc={val} text={text.strip()[:24]!r}")
    assert not bad, "档案编号未右对齐：\n" + "\n".join(bad)


def test_width_with_real_project_name(layout_items):
    bad = {
        f"{item.template} {item.part} p{item.paragraph}": f"{item.width_pt:.1f}pt"
        for item in layout_items if item.width_pt > verify.MAX_WIDTH_PT
    }
    assert not bad, f"拼上 29 字项目名后超过 487pt：{bad}"


def test_fix_is_idempotent(tmp_path):
    target = tmp_path / "计划书.docx"
    _mutate_to_old_layout(TEMPLATES / "计划书.docx", target)

    first = _run_fix(tmp_path)
    assert "[改动]" in first.stdout
    assert "页眉前导空格" in first.stdout
    assert "档案编号字号 sz" in first.stdout

    items = verify.scan_templates(tmp_path)
    assert all(item.leading <= 8 for item in items)
    assert all(
        sz == "24" for item in items if item.kind == "档案编号" for sz in item.sizes
    )

    before = _sha256(target)
    second = _run_fix(tmp_path)
    assert "无改动" in second.stdout
    assert _sha256(target) == before


def test_fix_handles_placeholder_split_across_runs(tmp_path):
    """Word 把空格和 {{项目名称}} 拆成多个 run 时也要能修。"""
    target = tmp_path / "计划书.docx"
    shutil.copy2(TEMPLATES / "计划书.docx", target)

    with zipfile.ZipFile(target, "r") as zin:
        parts = {name: zin.read(name) for name in zin.namelist()}

    root = etree.fromstring(parts["word/header1.xml"])
    for p in root.iter(f"{W}p"):
        nodes = [n for n in p.iter(f"{W}t") if "{{项目名称}}" in (n.text or "")]
        if not nodes:
            continue
        node = nodes[0]
        run = node.getparent()
        node.text = " " * 30
        node.set(XML_SPACE, "preserve")

        run2 = deepcopy(run)
        node2 = run2.find(f"{W}t")
        node2.text = " " * 28
        node2.set(XML_SPACE, "preserve")
        run.addnext(run2)

        run3 = deepcopy(run)
        node3 = run3.find(f"{W}t")
        node3.text = "{{项目名称}}"
        run2.addnext(run3)
        break
    parts["word/header1.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    tmp = target.with_name(target.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, payload in parts.items():
            zout.writestr(name, payload)
    tmp.replace(target)

    first = fix.fix_file(target)
    assert any(c.field == "页眉前导空格" for c in first), first

    with zipfile.ZipFile(target, "r") as zf:
        header = etree.fromstring(zf.read("word/header1.xml"))
    text = ""
    for p in header.iter(f"{W}p"):
        candidate = "".join(n.text or "" for n in p.iter(f"{W}t"))
        if "{{项目名称}}" in candidate:
            text = candidate
            break
    assert text.startswith(" " * 8 + "{{项目名称}}"), repr(text[:40])
    assert not text.startswith(" " * 9)

    assert fix.fix_file(target) == []

