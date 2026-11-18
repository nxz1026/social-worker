"""engine P2：编号生成/解析 + 活动目录建改。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import activities, coding, projects  # noqa: E402

HUADU_JILI = "花都社区激励资金项目"
HUIMIN = "惠民社区保障资金项目"


def test_make_code_sample():
    code = coding.make_code(year=2026, establish_month=8, act_type=1,
                            act_month=11, serial=1, act_year=2026)
    assert code == "CDYJR2026008-C-16110001"


def test_make_code_second_sample():
    # 旧数据 CDYJR2026005-c-1105001 有错位；新格式按 0005 / 类型 1 / 月 11 / 流水 0501
    code = coding.make_code(year=2026, establish_month=5, act_type=1,
                            act_month=11, serial=501, act_year=2026)
    assert code == "CDYJR2026005-C-16110501"


def test_make_code_rejects_out_of_range():
    ok = dict(year=2026, establish_month=8, act_type=1, act_month=11,
              serial=1, act_year=2026)
    for field, bad in (("year", 1999), ("year", 2101),
                       ("establish_month", 0), ("establish_month", 13),
                       ("act_type", -1), ("act_type", 10),
                       ("act_year", 1999), ("act_year", 2101),
                       ("act_month", 0), ("act_month", 13),
                       ("serial", 0), ("serial", 10000)):
        with pytest.raises(ValueError):
            coding.make_code(**{**ok, field: bad})


def test_parse_code_roundtrip():
    code = coding.make_code(year=2026, establish_month=8, act_type=3,
                            act_month=9, serial=42, act_year=2026)
    assert coding.parse_code(code) == {
        "prefix": "CDYJR",
        "year": 2026,
        "establish_month": 8,
        "act_type": 3,
        "act_year": 6,          # 只留个位，四位年已丢失
        "act_month": 9,
        "serial": 42,
    }
    parsed = coding.parse_code(code)
    assert coding.make_code(year=parsed["year"],
                            establish_month=parsed["establish_month"],
                            act_type=parsed["act_type"],
                            act_month=parsed["act_month"],
                            serial=parsed["serial"],
                            act_year=2026) == code


def test_parse_code_rejects_bad_format():
    with pytest.raises(ValueError):
        coding.parse_code("abc")
    with pytest.raises(ValueError):
        coding.parse_code("")


def test_parse_code_accepts_legacy_lowercase():
    parsed = coding.parse_code("CDYJR2026005-c-1105001")
    assert parsed["year"] == 2026
    assert parsed["establish_month"] == 5
    assert parsed["act_type"] == 1
    assert parsed["act_year"] is None    # 旧格式没有活动年
    assert parsed["serial"] == 5001


def test_make_code_with_act_year():
    assert coding.make_code(year=2026, establish_month=8, act_type=1,
                            act_month=11, serial=1,
                            act_year=2026) == "CDYJR2026008-C-16110001"
    assert coding.make_code(year=2026, establish_month=8, act_type=1,
                            act_month=5, serial=1,
                            act_year=2027) == "CDYJR2026008-C-17050001"


def test_cross_year_no_collision():
    c26 = coding.make_code(year=2026, establish_month=8, act_type=1,
                           act_month=5, serial=1, act_year=2026)
    c27 = coding.make_code(year=2026, establish_month=8, act_type=1,
                           act_month=5, serial=1, act_year=2027)
    assert c26 != c27


def test_parse_new_format():
    p = coding.parse_code("CDYJR2026008-C-16110001")
    assert p["act_year"] == 6 and p["act_month"] == 11 and p["serial"] == 1


def test_parse_legacy_format():
    p = coding.parse_code("CDYJR2026008-C-1110001")   # 旧 21 位
    assert p["act_year"] is None
    assert p["act_month"] == 11
    assert p["serial"] == 1


def test_next_serial_isolates_act_year():
    codes = ["CDYJR2026008-C-16110001", "CDYJR2026008-C-17050001"]
    assert coding.next_serial(codes, act_type=1, year=2026, act_year=2027) == 2
    assert coding.next_serial(codes, act_type=1, year=2026, act_year=2026) == 2


def test_next_serial_increments_same_type_and_year():
    assert coding.next_serial(["CDYJR2026008-C-16110001"],
                              act_type=1, year=2026, act_year=2026) == 2
    assert coding.next_serial([], act_type=1, year=2026, act_year=2026) == 1


def test_next_serial_ignores_other_type_year_and_junk():
    codes = [
        "CDYJR2026008-C-16110001",   # 同年同类型同活动年，最大
        "CDYJR2026008-C-16120002",   # 同年同类型同活动年（活动月不同也计入）
        "CDYJR2026008-C-26100009",   # 不同类型
        "CDYJR2025008-C-16110077",   # 不同年
        "CDYJR2026008-C-17100003",   # 不同活动年（2027）
        "CDYJR2026008-C-1110009",    # 旧格式（无活动年），跳过
        "随手起的目录名",
    ]
    assert coding.next_serial(codes, act_type=1, year=2026, act_year=2026) == 3


def test_project_meta_huadu_jili():
    meta = projects.project_meta(HUADU_JILI)
    assert meta["name"] == HUADU_JILI
    assert meta["short"] == HUADU_JILI
    assert meta["has_config"] is True
    assert meta["establish_month"] == 8
    assert meta["act_types"]["情境式游园会"] == 3
    assert meta["code"] == ""


def test_project_meta_huimin():
    meta = projects.project_meta(HUIMIN)
    assert meta["establish_month"] == 6
    assert meta["act_types"] == {
        "走访调研": 1, "联盟成立大会": 2, "街区氛围营造": 3,
        "爱邻议事会": 4, "共治行动": 5, "志愿服务": 6, "项目成果展": 7,
    }


def test_project_meta_without_yaml(tmp_path, monkeypatch):
    """无 yaml 的项目：project_meta 返回空配置（用 tmp 目录造）。"""
    (tmp_path / "无配置的项目").mkdir()
    monkeypatch.setattr(projects, "PROJECTS_ROOT", tmp_path)
    meta = projects.project_meta("无配置的项目")
    assert meta["has_config"] is False
    assert meta["establish_month"] is None
    assert meta["act_types"] == {}


def test_project_meta_rejects_traversal():
    with pytest.raises(ValueError):
        projects.project_meta("..")


def test_activity_dirs_only_top_level(tmp_path):
    act = tmp_path / "活动"
    act.mkdir()
    (act / "01-甲-第一期").mkdir()
    (act / "02-乙-第一期").mkdir()
    (act / "01-甲-第一期" / "子目录").mkdir()
    (act / "散文件.txt").write_text("x", encoding="utf-8")
    dirs = activities.activity_dirs(tmp_path)
    assert set(dirs) == {"01-甲-第一期", "02-乙-第一期"}
    assert all(p.is_absolute() for p in dirs.values())


def test_activity_dirs_missing_dir(tmp_path):
    assert activities.activity_dirs(tmp_path) == {}


def test_create_activity_dirs_idempotent(tmp_path):
    planned = [
        {"dirname": "01-走访调研-第一期"},
        {"dirname": "02-联盟成立大会-第一期"},
    ]
    first = activities.create_activity_dirs(tmp_path, planned)
    assert first == ["01-走访调研-第一期", "02-联盟成立大会-第一期"]
    assert (tmp_path / "活动" / "01-走访调研-第一期").is_dir()
    # 第二次全部已存在，返回空列表
    assert activities.create_activity_dirs(tmp_path, planned) == []
    # 只建目录，不写文件
    assert list((tmp_path / "活动" / "01-走访调研-第一期").iterdir()) == []


def test_create_activity_dirs_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        activities.create_activity_dirs(tmp_path, [{"dirname": "../逃逸"}])


def test_rename_activity_dir(tmp_path):
    act = tmp_path / "活动"
    act.mkdir()
    (act / "10-矛盾闭环处置运行-第一期").mkdir()
    activities.rename_activity_dir(tmp_path, "10-矛盾闭环处置运行-第一期",
                                  "10-矛盾闭环处置-第一期")
    assert (act / "10-矛盾闭环处置-第一期").is_dir()
    assert not (act / "10-矛盾闭环处置运行-第一期").exists()


def test_rename_activity_dir_missing_old(tmp_path):
    with pytest.raises(FileNotFoundError):
        activities.rename_activity_dir(tmp_path, "不存在-第一期", "新名字-第一期")


def test_rename_activity_dir_target_exists(tmp_path):
    act = tmp_path / "活动"
    act.mkdir()
    (act / "甲-第一期").mkdir()
    (act / "乙-第一期").mkdir()
    with pytest.raises(ValueError):
        activities.rename_activity_dir(tmp_path, "甲-第一期", "乙-第一期")


def test_rename_activity_dir_rejects_path_chars(tmp_path):
    act = tmp_path / "活动"
    act.mkdir()
    (act / "甲-第一期").mkdir()
    with pytest.raises(ValueError):
        activities.rename_activity_dir(tmp_path, "甲-第一期", "../乙")
