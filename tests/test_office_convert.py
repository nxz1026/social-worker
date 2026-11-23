#!/usr/bin/env python3
"""engine.office_convert：Office 转 PDF 供预览。

本机有 LibreOffice 才跑真实转换；没有就只测纯逻辑部分。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from engine import office_convert


requires_soffice = pytest.mark.skipif(
    not office_convert.available(), reason="本机无 LibreOffice")


def test_available_returns_bool():
    assert isinstance(office_convert.available(), bool)


def test_ext_lists_disjoint():
    """Office 类与直读类不能重叠，否则预览分流会乱。"""
    assert not set(office_convert.OFFICE_EXTS) & set(office_convert.DIRECT_EXTS)


def test_cache_key_changes_with_content(tmp_path):
    """缓存键含 mtime + size：源文件一变，键就变，不会拿旧缓存。"""
    f = tmp_path / "a.docx"
    f.write_bytes(b"v1")
    k1 = office_convert._cache_key(f)
    f.write_bytes(b"v2-longer")
    k2 = office_convert._cache_key(f)
    assert k1 != k2


@requires_soffice
def test_to_pdf_real_docx(tmp_path):
    """真实 docx → PDF，且二次调用命中缓存。"""
    lib = Path("/srv/social-work/templates/library")
    src = lib / "决算表.docx"
    if not src.is_file():
        pytest.skip("模板库无决算表.docx")
    cache = tmp_path / "cache"
    pdf = office_convert.to_pdf(src, cache)
    assert pdf.is_file() and pdf.stat().st_size > 1000
    assert pdf.read_bytes()[:5] == b"%PDF-"
    # 第二次不再转换：返回同一路径
    again = office_convert.to_pdf(src, cache)
    assert again == pdf


def test_to_pdf_missing_source(tmp_path):
    with pytest.raises(office_convert.ConvertError):
        office_convert.to_pdf(tmp_path / "nope.docx", tmp_path / "c")


def test_cleanup_keeps_recent(tmp_path):
    cache = tmp_path / "c"
    cache.mkdir()
    for i in range(5):
        p = cache / f"f{i}.pdf"
        p.write_bytes(b"%PDF-")
        p.touch()
    removed = office_convert.cleanup(cache, keep=2)
    assert removed == 3
    assert len(list(cache.glob("*.pdf"))) == 2


def test_cleanup_missing_dir(tmp_path):
    assert office_convert.cleanup(tmp_path / "nope") == 0
