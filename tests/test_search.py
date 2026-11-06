"""M7 检索参数下沉：pick_type / pick_dataset。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "cognee.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cg = _load("cognee_m7")


# ---------- pick_type ----------

def test_default_is_chunks():
    assert cg.pick_type("社区微更新做了什么") == "CHUNKS"


def test_date_picks_temporal():
    assert cg.pick_type("2026年9月的活动") == "TEMPORAL"
    assert cg.pick_type("去年11月") == "TEMPORAL"


def test_quotes_pick_lexical():
    assert cg.pick_type('"亲邻商居共治联盟"') == "CHUNKS_LEXICAL"
    assert cg.pick_type("「爱邻议事会」") == "CHUNKS_LEXICAL"


def test_quotes_and_date_prefers_date():
    assert cg.pick_type('"2026-11-05" 的推文') == "TEMPORAL"


# ---------- pick_dataset ----------

_CFG = type("C", (), {"path": ROOT / "config" / "config.yaml",
                      "datasets": {"huadu": "huadu", "huimin": "huimin"}})()


def test_dataset_by_project_keyword():
    assert cg.pick_dataset("惠民社区做了什么", _CFG, ROOT / "config") == "huimin"
    assert cg.pick_dataset("花都的入户名单", _CFG, ROOT / "config") == "huadu"


def test_dataset_none_when_no_keyword():
    assert cg.pick_dataset("今天天气怎么样", _CFG, ROOT / "config") is None


def test_dataset_none_on_tie():
    """两个项目关键词出现次数一样 → 不猜，搜全部。"""
    assert cg.pick_dataset("惠民 花都", _CFG, ROOT / "config") is None


def test_dataset_missing_alias_file(tmp_path):
    assert cg.pick_dataset("惠民", _CFG, tmp_path) is None


def test_dataset_bad_yaml(tmp_path):
    (tmp_path / "project_aliases.yaml").write_text("{{{", encoding="utf-8")
    assert cg.pick_dataset("惠民", _CFG, tmp_path) is None


def test_dataset_alias_without_dataset_mapping(tmp_path):
    """配置里有项目但 config.yaml 没给数据集名 → 回退用项目 key。"""
    (tmp_path / "project_aliases.yaml").write_text(
        "huadu: [花都]\nhuimin: [惠民]\n", encoding="utf-8")
    cfg = type("C", (), {"path": tmp_path / "config.yaml", "datasets": {}})()
    assert cg.pick_dataset("花都", cfg, tmp_path) == "huadu"


def test_dataset_empty_query():
    assert cg.pick_dataset("", _CFG, ROOT / "config") is None
