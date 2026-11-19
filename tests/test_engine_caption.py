"""engine/caption.py：挑图规则与图注生成（LLM 调用被替换掉）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import caption, llm  # noqa: E402


def _touch(folder: Path, *names: str) -> None:
    """造空图片文件（只看文件名，不读内容）。"""
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"")


# --------------------------------------------------------------------------
# 挑图
# --------------------------------------------------------------------------

def test_pick_photos_prefers_keyword_names(tmp_path: Path):
    """规范命中词优先，微信默认名排后面。"""
    folder = tmp_path / "11-活动照片资料"
    _touch(folder, "微信图片_001.jpg", "微信图片_002.jpg",
           "2026-11-10-惠民-共治行动横幅-01.jpg", "2026-11-10-惠民-活动全景-01.jpg")

    picked = [path.name for path in caption.pick_photos(tmp_path)]

    assert picked[:2] == ["2026-11-10-惠民-活动全景-01.jpg",
                          "2026-11-10-惠民-共治行动横幅-01.jpg"]
    assert picked[2:] == ["微信图片_001.jpg", "微信图片_002.jpg"]


def test_pick_photos_keyword_order_beats_filename(tmp_path: Path):
    """同是规范命名时，按命中词顺序（全景 > 合影 > 横幅）。"""
    folder = tmp_path / "11-活动照片资料"
    _touch(folder, "a-横幅.jpg", "b-合影.jpg", "c-全景.jpg")

    picked = [path.name for path in caption.pick_photos(tmp_path)]

    assert picked == ["c-全景.jpg", "b-合影.jpg", "a-横幅.jpg"]


def test_pick_photos_limit_and_ignores_non_images(tmp_path: Path):
    folder = tmp_path / "11-活动照片资料"
    _touch(folder, "全景-01.jpg", "合影-01.jpg", "横幅-01.jpg", "说明.txt")
    (folder / "说明.docx").write_bytes(b"")

    picked = caption.pick_photos(tmp_path, limit=2)

    assert [path.name for path in picked] == ["全景-01.jpg", "合影-01.jpg"]


def test_pick_photos_missing_folder(tmp_path: Path):
    assert caption.pick_photos(tmp_path) == []
    assert caption.pick_photos(tmp_path, limit=0) == []


def test_pick_photos_custom_subdir(tmp_path: Path):
    _touch(tmp_path / "10-物资照片资料", "横幅-物资.jpg")

    picked = caption.pick_photos(tmp_path, sub="10-物资照片资料")

    assert [path.name for path in picked] == ["横幅-物资.jpg"]


# --------------------------------------------------------------------------
# 图注
# --------------------------------------------------------------------------

def test_write_captions_numbers_and_strips_model_prefix(tmp_path: Path, monkeypatch):
    """模型自带「图1：」时去重，统一由程序编号。"""
    folder = tmp_path / "11-活动照片资料"
    _touch(folder, "全景-01.jpg", "发放-01.jpg")
    photos = caption.pick_photos(tmp_path)

    monkeypatch.setattr(llm, "_chat", lambda prompt, max_tokens=None: json.dumps(
        {"图注": ["图1：活动现场全景", "图2 志愿者发放物资"]}, ensure_ascii=False))

    result = caption.write_captions(photos, {"活动名称": "共治行动（第一期）"})

    assert result == [
        {"file": "全景-01.jpg", "caption": "图1 活动现场全景"},
        {"file": "发放-01.jpg", "caption": "图2 志愿者发放物资"},
    ]


def test_write_captions_fills_missing_with_generic(tmp_path: Path, monkeypatch):
    """模型少给一条时用活动名称兜底，不瞎猜细节。"""
    folder = tmp_path / "11-活动照片资料"
    _touch(folder, "全景-01.jpg", "特写-01.jpg")
    photos = caption.pick_photos(tmp_path)

    monkeypatch.setattr(llm, "_chat", lambda prompt, max_tokens=None: json.dumps(
        {"图注": ["图1 活动现场全景"]}, ensure_ascii=False))

    result = caption.write_captions(photos, {"活动名称": "共治行动（第一期）"})

    assert result[1]["caption"] == "图2 共治行动（第一期）现场"


def test_write_captions_prompt_has_names_and_facts(tmp_path: Path, monkeypatch):
    """prompt 里带文件名与活动事实，且明确不许瞎猜。"""
    folder = tmp_path / "11-活动照片资料"
    _touch(folder, "合影-01.jpg")
    photos = caption.pick_photos(tmp_path)
    captured: dict = {}

    def fake_chat(prompt, max_tokens=None):
        captured["prompt"] = prompt
        return json.dumps({"图注": ["图1 集体合影"]}, ensure_ascii=False)

    monkeypatch.setattr(llm, "_chat", fake_chat)
    caption.write_captions(photos, {"活动名称": "共治行动（第一期）", "实际人数": 19})

    prompt = captured["prompt"]
    assert "合影-01.jpg" in prompt
    assert "- 活动名称：共治行动（第一期）" in prompt
    assert "不许瞎猜细节" in prompt


def test_write_captions_empty_photos_skips_llm(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("没有照片时不该调用 LLM")

    monkeypatch.setattr(llm, "_chat", boom)
    assert caption.write_captions([], {"活动名称": "某活动"}) == []


def test_as_fill_data_maps_to_placeholder_list():
    captions = [{"file": "a.jpg", "caption": "图1 全景"},
                {"file": "b.jpg", "caption": "图2 合影"}]

    assert caption.as_fill_data(captions) == {"图注": ["图1 全景", "图2 合影"]}
