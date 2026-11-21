"""G1：活动照片批量上传的纯逻辑（压缩 / 去重 / 命名 / 索引）。

不碰数据库与 FastAPI，全部在 tmp_path 里造图。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from engine import photos as P

ACT = "11-共治行动-第一期"


# ---------------- 造图工具 ----------------

def _jpeg_bytes(size=(1200, 800), color=(30, 120, 200), quality=92) -> bytes:
    from io import BytesIO
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _noise_jpeg(path: Path, size=(3000, 2000)) -> None:
    """随机噪点：保证压缩后体积明显变小。"""
    img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    img.save(path, "JPEG", quality=98)


def _activity(tmp_path: Path) -> Path:
    act = tmp_path / "活动" / ACT
    act.mkdir(parents=True)
    return act


def _photo_dir(act: Path) -> Path:
    d = act / P.PHOTO_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------- 压缩 ----------------

def test_compress_jpeg_resizes_max_edge():
    data, w, h = P.compress_image(_jpeg_bytes((3000, 2000)))
    assert max(w, h) == P.MAX_EDGE
    # 高按等比缩放，不写死数字（MAX_EDGE 会随省空间策略调整）
    assert w == P.MAX_EDGE
    assert h == round(P.MAX_EDGE * 2000 / 3000)
    assert data[:2] == b"\xff\xd8"          # JPEG SOI


def test_compress_does_not_upscale_small_image():
    _data, w, h = P.compress_image(_jpeg_bytes((640, 480)))
    assert (w, h) == (640, 480)


def test_compress_png_alpha_flattened_to_white():
    from io import BytesIO
    img = Image.new("RGBA", (300, 200), (0, 0, 255, 255))
    for x in range(100):
        for y in range(100):
            img.putpixel((x, y), (255, 0, 0, 0))   # 左上透明
    buf = BytesIO()
    img.save(buf, "PNG")
    src = buf.getvalue()
    data, w, h = P.compress_image(src)
    assert (w, h) == (300, 200)
    if data == src:
        # 小图重编码反而变大时保留原件（变大保护），此时不经过 RGB 转换
        assert Image.open(BytesIO(data)).mode == "RGBA"
        return
    with Image.open(BytesIO(data)) as out:
        assert out.mode == "RGB"
        assert out.getpixel((10, 10)) == (255, 255, 255)
        assert out.getpixel((200, 100))[2] > 200


def test_compress_invalid_raises_value_error():
    with pytest.raises(ValueError):
        P.compress_image(b"not an image")
    with pytest.raises(ValueError):
        P.compress_image(b"")


# ---------------- 哈希 / 命名 ----------------

def test_sha256_of_matches_hashlib():
    data = _jpeg_bytes((100, 100))
    assert P.sha256_of(data) == hashlib.sha256(data).hexdigest()


def test_target_name_both_date_forms():
    assert P.target_name("2026-11-10", 1) == "20261110_001.jpg"
    assert P.target_name("20261110", 62) == "20261110_062.jpg"
    with pytest.raises(ValueError):
        P.target_name("2026/11/10", 1)


def test_next_seq_continues_from_existing(tmp_path):
    act = _activity(tmp_path)
    pd = _photo_dir(act)
    for i in range(1, 6):
        (pd / f"20261110_{i:03d}.jpg").write_bytes(b"x")
    (pd / "20261111_009.jpg").write_bytes(b"x")     # 别的日期不参与
    assert P.next_seq(act, "2026-11-10") == 6
    assert P.next_seq(act, "2026-11-11") == 10


def test_next_seq_starts_at_one(tmp_path):
    act = _activity(tmp_path)
    assert P.next_seq(act, "2026-11-10") == 1


# ---------------- 元数据 ----------------

def test_load_meta_defaults_when_missing(tmp_path):
    assert P.load_meta(tmp_path) == {"batches": [], "photos": [], "hashes": {}}


def test_save_meta_atomic_and_keeps_chinese(tmp_path):
    meta = {"batches": [{"scene": "主会场"}], "photos": [], "hashes": {}}
    P.save_meta(tmp_path, meta)
    raw = (tmp_path / P.META_NAME).read_text(encoding="utf-8")
    assert "主会场" in raw and "\\u4e3b" not in raw
    assert json.loads(raw)["batches"][0]["scene"] == "主会场"
    assert not (tmp_path / (P.META_NAME + ".tmp")).exists()


def test_load_meta_corrupt_raises(tmp_path):
    (tmp_path / P.META_NAME).write_text("{bad json", encoding="utf-8")
    with pytest.raises(ValueError):
        P.load_meta(tmp_path)


# ---------------- 去重 ----------------

def test_dedup_check_splits_new_and_duplicated():
    meta = {"hashes": {"a" * 64: "20261110_001.jpg"}}
    out = P.dedup_check(meta, ["a" * 64, "b" * 64, "b" * 64])
    assert out == {"new": ["b" * 64], "duplicated": ["a" * 64]}


# ---------------- 保存一批 ----------------

def test_save_photos_writes_named_files_and_meta(tmp_path):
    act = _activity(tmp_path)
    r = P.save_photos(act, [("IMG_001.jpg", _jpeg_bytes(color=(1, 2, 3))),
                            ("IMG_002.jpg", _jpeg_bytes(color=(4, 5, 6)))],
                      "2026-11-10", scene="主会场", note="一口气传的",
                      uploaded_by="张三")
    assert r["saved"] == 2 and r["skipped"] == []
    assert r["paths"] == ["11-活动照片资料/20261110_001.jpg",
                          "11-活动照片资料/20261110_002.jpg"]
    assert r["total_bytes"] > 0
    assert (act / P.PHOTO_SUBDIR / "20261110_001.jpg").is_file()

    meta = P.load_meta(act)
    assert len(meta["batches"]) == 1
    batch = meta["batches"][0]
    assert batch["scene"] == "主会场" and batch["count"] == 2
    assert batch["shot_date"] == "2026-11-10" and batch["uploaded_by"] == "张三"
    assert meta["photos"][0]["original"] == "IMG_001.jpg"
    assert list(meta["hashes"].values()) == ["20261110_001.jpg", "20261110_002.jpg"]


def test_same_image_different_name_is_skipped_duplicate(tmp_path):
    act = _activity(tmp_path)
    data = _jpeg_bytes(color=(9, 9, 9))
    P.save_photos(act, [("IMG_001.jpg", data)], "2026-11-10", scene="主会场")
    # 换场景避开 L3，专测 L2
    r = P.save_photos(act, [("微信图片_20261110.jpg", data)],
                      "2026-11-10", scene="分组走访")
    assert r["saved"] == 0 and r["duplicate_batch"] is None
    assert r["skipped"] == [{"original": "微信图片_20261110.jpg",
                             "reason": "duplicate",
                             "existing": "20261110_001.jpg"}]


def test_duplicate_inside_same_batch(tmp_path):
    act = _activity(tmp_path)
    data = _jpeg_bytes(color=(7, 7, 7))
    r = P.save_photos(act, [("a.jpg", data), ("b.jpg", data)],
                      "2026-11-10", scene="主会场")
    assert r["saved"] == 1
    assert r["skipped"][0]["reason"] == "duplicate"


def test_l3_batch_duplicate_within_24h(tmp_path):
    act = _activity(tmp_path)
    first = P.save_photos(act, [("a.jpg", _jpeg_bytes(color=(1, 1, 1)))],
                          "2026-11-10", scene="主会场")
    again = P.save_photos(act, [("b.jpg", _jpeg_bytes(color=(2, 2, 2)))],
                          "2026-11-10", scene="主会场")
    assert again["saved"] == 0
    assert again["duplicate_batch"] == first["batch_id"]
    # 同日期不同场景不算重复
    other = P.save_photos(act, [("c.jpg", _jpeg_bytes(color=(3, 3, 3)))],
                          "2026-11-10", scene="分组走访")
    assert other["saved"] == 1 and other["duplicate_batch"] is None


def test_allow_duplicate_bypasses_l2_and_l3(tmp_path):
    act = _activity(tmp_path)
    data = _jpeg_bytes(color=(5, 5, 5))
    P.save_photos(act, [("a.jpg", data)], "2026-11-10", scene="主会场")
    r = P.save_photos(act, [("a-copy.jpg", data)], "2026-11-10", scene="主会场",
                      allow_duplicate=True)
    assert r["saved"] == 1 and r["duplicate_batch"] is None
    assert r["paths"] == ["11-活动照片资料/20261110_002.jpg"]


def test_compress_false_keeps_original_jpeg_bytes(tmp_path):
    act = _activity(tmp_path)
    data = _jpeg_bytes((1200, 800), color=(11, 22, 33))
    P.save_photos(act, [("a.jpg", data)], "2026-11-10", compress=False)
    stored = (act / P.PHOTO_SUBDIR / "20261110_001.jpg").read_bytes()
    assert stored == data


def test_save_photos_skips_invalid_image(tmp_path):
    act = _activity(tmp_path)
    r = P.save_photos(act, [("broken.jpg", b"not really a jpeg")],
                      "2026-11-10")
    assert r["saved"] == 0
    assert r["skipped"][0]["reason"] == "invalid"


# ---------------- 索引 / 精选 ----------------

def test_contact_sheet_written_with_three_columns(tmp_path):
    act = _activity(tmp_path)
    pd = _photo_dir(act)
    for i in range(1, 5):
        (pd / f"20261110_{i:03d}.jpg").write_bytes(_jpeg_bytes((800, 600)))
    out = P.make_contact_sheet(act, pd, max_count=9)
    assert out == act / P.CONTACT_SHEET_NAME and out.is_file()
    with Image.open(out) as img:
        assert img.width == P.CONTACT_SHEET_COLS * P.CONTACT_SHEET_CELL + 4 * 6
        # 4 张 → 2 行
        assert img.height == 2 * P.CONTACT_SHEET_CELL + 3 * 6


def test_contact_sheet_none_when_empty(tmp_path):
    act = _activity(tmp_path)
    assert P.make_contact_sheet(act, _photo_dir(act)) is None


def test_pick_highlights_uniform_sampling(tmp_path):
    act = _activity(tmp_path)
    pd = _photo_dir(act)
    for i in range(1, 11):
        # 越靠后越大，保证大小排序与文件名顺序不同
        (pd / f"20261110_{i:03d}.jpg").write_bytes(
            _jpeg_bytes((200 + i * 10, 150)))
    picked = P.pick_highlights(act, pd, count=3)
    assert len(picked) == 3
    assert all(p.is_file() for p in picked)
    assert picked[0].stat().st_size >= picked[-1].stat().st_size


# ---------------- 存量压缩 ----------------

def test_compress_dir_dry_run_then_write(tmp_path):
    act = _activity(tmp_path)
    pd = _photo_dir(act)
    big = pd / "20260101_001.jpg"
    _noise_jpeg(big, (3000, 2000))
    before = big.stat().st_size

    dry = P.compress_dir(act, pd, dry_run=True)
    assert dry["compressed"] == 1 and dry["saved_bytes"] > 0
    assert big.stat().st_size == before          # 预演不动文件

    real = P.compress_dir(act, pd)
    assert real["compressed"] == 1
    assert real["saved_bytes"] == dry["saved_bytes"]
    assert big.stat().st_size < before
    with Image.open(big) as img:
        assert max(img.size) == P.MAX_EDGE


# ---------------- Cognee 文本 ----------------

def test_build_cognee_text_only_activity_fact(tmp_path):
    act = _activity(tmp_path)
    P.save_photos(act, [("a.jpg", _jpeg_bytes(color=(1, 1, 1)))],
                  "2026-11-10", scene="主会场")
    P.save_photos(act, [("b.jpg", _jpeg_bytes(color=(2, 2, 2)))],
                  "2026-11-11", scene="分组走访")
    text = P.build_cognee_text("惠民社区保障资金项目", "共治行动（第一期）",
                               "CDYJR2026008-C-1110002", P.load_meta(act))
    assert "共治行动（第一期）" in text
    assert "有现场照片 2 张" in text
    assert "CDYJR2026008-C-1110002" in text
    assert "2026-11-10" in text and "2026-11-11" in text
    assert "主会场" in text and "分组走访" in text
    # 不把单张文件名写进入库文本
    assert "20261110_001.jpg" not in text
