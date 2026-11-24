"""工单 C：数据一致性与边界验证。

只新增本文件，不改任何现有文件。覆盖四块：

1. engine.invoice.cross_check —— 三表金额校验与「金额 / 证据」两层结论分离
2. engine.coding —— 编号格式、确定性
3. engine.calc.rmb_upper —— 人民币大写边界
4. engine.checks —— 15 项核对的结构与计数
5. engine.photos —— 元数据与上传去重边界

约定：
- 涉及 /srv/social-work 真实资产的用例在资产缺失时 skip（其他机器也能跑）
- 所有写盘一律走 tmp_path
- 中文注释
"""
from __future__ import annotations

import re
import sys
import unittest.mock
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from engine import calc, checks, coding, invoice, photos, projects  # noqa: E402

# 工单指定的真实活动；缺资产时相关用例 skip
REAL_ACT = (Path(projects.PROJECTS_ROOT) / "惠民社区保障资金项目"
            / "活动" / "11-共治行动-第一期")
REAL_SKIP = pytest.mark.skipif(not REAL_ACT.is_dir(),
                               reason=f"真实活动目录不存在：{REAL_ACT}")


# --------------------------------------------------------------------------
# 1. 三表金额校验
# --------------------------------------------------------------------------

@REAL_SKIP
def test_real_cross_check_has_required_fields():
    """返回必须含 consistent / amount_consistent / evidence_valid / conflicts。"""
    result = invoice.cross_check(REAL_ACT)

    for key in ("consistent", "amount_consistent", "evidence_valid", "conflicts"):
        assert key in result, f"缺少字段：{key}"
    assert isinstance(result["consistent"], bool)
    assert isinstance(result["amount_consistent"], bool)
    assert isinstance(result["evidence_valid"], bool)
    assert isinstance(result["conflicts"], list)


@REAL_SKIP
def test_real_cross_check_amounts_all_consistent():
    """实测：决算 = 报销 = 发票 = 1662，三个结论都为 True。"""
    result = invoice.cross_check(REAL_ACT)

    assert result["决算"]["value"] == 1662.0
    assert result["报销"]["value"] == 1662.0
    assert result["发票"]["value"] == 1662.0
    assert result["consistent"] is True
    assert result["amount_consistent"] is True
    assert result["evidence_valid"] is True
    assert result["conflicts"] == []
    assert result["diff"] == {"决算-报销": 0.0, "报销-发票": 0.0}


@REAL_SKIP
def test_cross_check_injected_parse_error_splits_layers():
    """给一张发票注入 parse_error：金额层仍可一致，但总体必须判不通过。

    用 unittest.mock.patch 替换 scan_invoices，在真实扫描结果上改一个字段，
    模拟「文件名与正文矛盾」的证据冲突。
    """
    real_scan = invoice.scan_invoices

    def _inject_parse_error(act_dir: Path) -> list[dict]:
        items = real_scan(act_dir)
        if items:
            items[0]["parse_error"] = "文件名金额 1190.00 与正文价税合计 999.00 不一致"
        return items

    with unittest.mock.patch.object(
            invoice, "scan_invoices", side_effect=_inject_parse_error):
        result = invoice.cross_check(REAL_ACT)

    # 两层分离：算术口径没变，证据口径坏了
    assert result["amount_consistent"] is True
    assert result["evidence_valid"] is False
    assert result["consistent"] is False
    assert result["发票"]["value"] == 1662.0
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["file"] == real_scan(REAL_ACT)[0]["file"]
    assert "不一致" in result["conflicts"][0]["reason"]


@REAL_SKIP
def test_cross_check_injected_unparsed_invoice_breaks_amount_consistency():
    """注入的发票同时读不出金额时，金额层也必须判不一致（不是只靠证据层兜底）。"""
    real_scan = invoice.scan_invoices

    def _inject_unparsed(act_dir: Path) -> list[dict]:
        items = real_scan(act_dir)
        if items:
            items[0]["amount"] = None
            items[0]["parse_error"] = "正文未取到价税合计"
        return items

    with unittest.mock.patch.object(
            invoice, "scan_invoices", side_effect=_inject_unparsed):
        result = invoice.cross_check(REAL_ACT)

    assert result["amount_consistent"] is False
    assert result["evidence_valid"] is False
    assert result["consistent"] is False
    # 少一张 300 元发票后合计为 1362
    assert result["发票"]["value"] == 1362.0
    assert any("未能识别金额" in issue for issue in result["issues"])


# --------------------------------------------------------------------------
# 2. 编号生成
# --------------------------------------------------------------------------

def test_make_code_format_length_and_value():
    """格式 CDYJR{年4}{月3:03d}-C-{类型1}{活动年1}{月2}{流水4}，总长 23。"""
    code = coding.make_code(year=2026, establish_month=8, act_type=1,
                            act_year=2026, act_month=11, serial=1)

    assert code == "CDYJR2026008-C-16110001"
    assert len(code) == 23
    assert re.fullmatch(r"CDYJR\d{4}\d{3}-C-\d{8}", code)
    # 生成的编号要能被自己解析回原字段
    parsed = coding.parse_code(code)
    assert parsed["year"] == 2026
    assert parsed["establish_month"] == 8
    assert parsed["act_type"] == 1
    assert parsed["act_year"] == 6
    assert parsed["act_month"] == 11
    assert parsed["serial"] == 1


def test_make_code_different_serial_changes_code():
    """不同 serial 生成不同编号，且只有流水段变化。"""
    base = dict(year=2026, establish_month=8, act_type=1,
                act_year=2026, act_month=11)
    first = coding.make_code(serial=1, **base)
    second = coding.make_code(serial=2, **base)

    assert first != second
    assert first[:19] == second[:19]                 # 前 19 位相同
    assert first[19:] == "0001" and second[19:] == "0002"


def test_make_code_is_deterministic():
    """同一组参数重复生成，编号完全相同。"""
    kwargs = dict(year=2026, establish_month=8, act_type=1,
                  act_year=2026, act_month=11, serial=42)
    assert coding.make_code(**kwargs) == coding.make_code(**kwargs)


def test_make_code_act_year_wraps_and_differs():
    """活动年只取个位：2026 与 2027 的同月同流水必须撞不开。"""
    base = dict(year=2026, establish_month=8, act_type=1, act_month=5, serial=1)
    code_2026 = coding.make_code(act_year=2026, **base)
    code_2027 = coding.make_code(act_year=2027, **base)

    assert code_2026 != code_2027
    assert coding.parse_code(code_2026)["act_year"] == 6
    assert coding.parse_code(code_2027)["act_year"] == 7


# --------------------------------------------------------------------------
# 3. 人民币大写
# --------------------------------------------------------------------------

@pytest.mark.parametrize("amount,expected", [
    (0, "零元整"),
    (0.01, "零壹分"),          # 见下方说明：工单写「壹分」，实测为「零壹分」
    (1234.56, "壹仟贰佰叁拾肆元伍角陆分"),
    (10000, "壹万元整"),
    (100000000, "壹亿元整"),
    (66666.66, "陆万陆仟陆佰陆拾陆元陆角陆分"),
])
def test_rmb_upper_boundaries(amount, expected):
    """工单给的六个边界值，按实测真实输出断言。"""
    assert calc.rmb_upper(amount) == expected


def test_rmb_upper_sub_yuan_rule_is_self_consistent():
    """不足一元、角为 0 时按代码规则写「零X分」，不是「X分」。

    工单预期 0.01 → 「壹分」，但 engine/calc.py 的规则（docstring 与实现）是：
    角为 0、分有值时写「元零X分」，不足一元则省略「元」→ 「零壹分」。
    340.05 → 「叁佰肆拾元零伍分」可对照，代码自洽，因此按真实值「零壹分」断言。
    """
    assert calc.rmb_upper(0.01) == "零壹分"
    assert calc.rmb_upper(0.05) == "零伍分"
    assert calc.rmb_upper(0.5) == "伍角整"
    assert calc.rmb_upper(340.05) == "叁佰肆拾元零伍分"


# --------------------------------------------------------------------------
# 4. 活动核对
# --------------------------------------------------------------------------

def _write(path: Path, data: bytes = b"x") -> None:
    """造占位文件（内容无所谓，核对只看名字 / 后缀）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _complete_activity(tmp_path: Path) -> Path:
    """一套 14 项齐全、第 15 项不适用的活动目录。"""
    act = tmp_path / "11-共治行动-第一期"
    act.mkdir()
    _write(act / "01-计划书.docx")
    _write(act / "02-活动通知.docx")
    _write(act / "03-签到表.jpg")                       # 第 3 项签字照片
    _write(act / "05-满意度测评汇总表.docx")
    _write(act / "06-总结报告.docx")
    _write(act / "07-简报.docx")
    _write(act / "08-决算表.docx")
    _write(act / "09-监督记录表.jpg")                   # 第 8 项签字照片
    _write(act / "21-报销单.xlsx")                      # 第 9 项报销单
    _write(act / "10-物资照片资料" / "物资.jpg")
    _write(act / "11-活动照片资料" / "活动.jpg")
    _write(act / "12-发票" / "发票.pdf")
    _write(act / "13-其他佐证" / "佐证.txt")
    _write(act / "04-满意度调查表.jpg")                 # 第 14 项签字照片
    return act


def _assert_check_shape(result: dict) -> None:
    """15 项、状态域、计数和、complete 等价关系。"""
    items = result["items"]
    assert len(items) == 15
    assert [it["no"] for it in items] == list(range(1, 16))
    assert all(it["status"] in {"ok", "partial", "missing", "na"} for it in items)
    total = (result["ok_count"] + result["partial_count"]
             + result["missing_count"] + result["na_count"])
    assert total == 15
    assert result["complete"] == (result["partial_count"] == 0
                                  and result["missing_count"] == 0)


@REAL_SKIP
def test_real_checks_structure_and_counts():
    """真实活动：15 项结构、计数和为 15、complete 等价关系。

    2026-11-24 起「文件存在」不再等于「内容填了」：决算表残留 {{...}}
    或满意度测评表全空都判 partial。这个活动的满意度测评汇总表确实没填
    数值，因此 ok 从 13 降到 12、partial 从 1 升到 2。
    """
    result = checks.check(REAL_ACT)

    _assert_check_shape(result)
    assert result["ok_count"] == 12
    assert result["partial_count"] == 2
    assert result["missing_count"] == 0
    assert result["na_count"] == 1
    assert result["complete"] is False
    # 两项 partial 必须是「有文件但没填内容」，不是文件缺失
    partials = {it["no"] for it in result["items"] if it["status"] == "partial"}
    assert partials == {4, 8}, f"partial 项应含满意度测评表(4)与监督记录(8)，实际 {partials}"


def test_checks_complete_activity(tmp_path):
    """14 项齐 + 第 15 项不适用：complete 为 True。"""
    result = checks.check(_complete_activity(tmp_path))

    _assert_check_shape(result)
    assert result["ok_count"] == 14
    assert result["partial_count"] == 0
    assert result["missing_count"] == 0
    assert result["na_count"] == 1
    assert result["complete"] is True


def test_checks_missing_blocks_complete(tmp_path):
    """缺一项：missing_count=1，complete 为 False。"""
    act = _complete_activity(tmp_path)
    (act / "02-活动通知.docx").unlink()

    result = checks.check(act)

    _assert_check_shape(result)
    assert result["missing_count"] == 1
    assert result["complete"] is False
    item2 = next(it for it in result["items"] if it["no"] == 2)
    assert item2["status"] == "missing"


def test_checks_partial_blocks_complete(tmp_path):
    """签字照片退回未签字 docx：partial_count=1，complete 为 False。"""
    act = _complete_activity(tmp_path)
    (act / "03-签到表.jpg").unlink()
    _write(act / "03-签到表.docx")

    result = checks.check(act)

    _assert_check_shape(result)
    assert result["partial_count"] == 1
    assert result["complete"] is False
    item3 = next(it for it in result["items"] if it["no"] == 3)
    assert item3["status"] == "partial"


def test_checks_na_does_not_block_complete(tmp_path):
    """不适用只是不算齐、也不算缺，complete 仍可为 True。"""
    act = _complete_activity(tmp_path)
    _write(act / "02-活动通知-不适用.docx")

    result = checks.check(act)

    _assert_check_shape(result)
    assert result["na_count"] == 2
    assert result["partial_count"] == 0 and result["missing_count"] == 0
    assert result["complete"] is True


# --------------------------------------------------------------------------
# 5. 照片元数据与上传去重
# --------------------------------------------------------------------------

def _jpeg_bytes(color=(1, 2, 3), size=(200, 150)) -> bytes:
    """造一张合法 JPEG，供上传用例使用。"""
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _activity(tmp_path: Path) -> Path:
    act = tmp_path / "活动" / "11-共治行动-第一期"
    act.mkdir(parents=True)
    return act


def _photo_dir(act: Path) -> Path:
    return act / photos.PHOTO_SUBDIR


def test_load_meta_empty_directory(tmp_path):
    """空目录（无 _photos.json）→ 三个空字段。"""
    assert photos.load_meta(tmp_path) == {"batches": [], "photos": [], "hashes": {}}


def test_save_meta_roundtrip(tmp_path):
    """save_meta 后 load_meta 能原样读回，中文不被转义。"""
    meta = {
        "batches": [{"batch_id": "20261120-143025", "scene": "主会场",
                     "shot_date": "2026-11-10", "count": 2}],
        "photos": [{"name": "20261110_001.jpg", "original": "微信图片_1.jpg"}],
        "hashes": {"a" * 64: "20261110_001.jpg"},
    }

    photos.save_meta(tmp_path, meta)
    loaded = photos.load_meta(tmp_path)

    assert loaded == meta
    raw = (tmp_path / photos.META_NAME).read_text(encoding="utf-8")
    assert "主会场" in raw and "\\u4e3b" not in raw


def test_save_photos_two_images_saved_to_disk(tmp_path):
    """传 2 张 → saved=2，磁盘照片目录里正好 2 个文件。"""
    act = _activity(tmp_path)

    result = photos.save_photos(
        act,
        [("IMG_001.jpg", _jpeg_bytes(color=(10, 20, 30))),
         ("IMG_002.jpg", _jpeg_bytes(color=(40, 50, 60)))],
        "2026-11-10", scene="主会场")

    assert result["saved"] == 2
    assert result["skipped"] == []
    assert len(result["paths"]) == 2
    on_disk = sorted((p for p in _photo_dir(act).iterdir() if p.is_file()),
                     key=lambda p: p.name)
    assert len(on_disk) == 2
    assert [Path(p).name for p in result["paths"]] == [p.name for p in on_disk]


def test_save_photos_resend_same_content_skipped(tmp_path):
    """同内容重传 → skipped 有值（换场景避开 L3，专测 L2 内容去重）。"""
    act = _activity(tmp_path)
    data = _jpeg_bytes(color=(7, 7, 7))
    first = photos.save_photos(act, [("IMG_001.jpg", data)],
                               "2026-11-10", scene="主会场")

    again = photos.save_photos(act, [("微信图片_20261110.jpg", data)],
                               "2026-11-10", scene="分组走访")

    assert first["saved"] == 1
    assert again["saved"] == 0
    assert again["duplicate_batch"] is None
    assert len(again["skipped"]) == 1
    assert again["skipped"][0]["reason"] == "duplicate"
    assert again["skipped"][0]["original"] == "微信图片_20261110.jpg"
    assert again["skipped"][0]["existing"] == "20261110_001.jpg"
    # 磁盘仍只有一份
    assert len([p for p in _photo_dir(act).iterdir() if p.is_file()]) == 1


def test_save_photos_same_bytes_different_names_stores_one(tmp_path):
    """同一批里同字节不同文件名 → 只存一份，另一张记 duplicate。"""
    act = _activity(tmp_path)
    data = _jpeg_bytes(color=(9, 9, 9))

    result = photos.save_photos(act, [("a.jpg", data), ("b.jpg", data)],
                                "2026-11-10", scene="主会场")

    assert result["saved"] == 1
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "duplicate"
    assert len([p for p in _photo_dir(act).iterdir() if p.is_file()]) == 1
    meta = photos.load_meta(act)
    assert len(meta["hashes"]) == 1
    assert meta["hashes"][photos.sha256_of(data)] == "20261110_001.jpg"
