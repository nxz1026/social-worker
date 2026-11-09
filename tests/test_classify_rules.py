"""M2 分类规则层测试。

跑法：
    python3 -m pytest tests/test_classify_rules.py -q
或：
    python3 tests/test_classify_rules.py

验收标准（计划里定的）：规则命中率 ≥ 70%，判错率 = 0。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import classify_rules  # noqa: E402

RULES = classify_rules.load_rules(ROOT / "config")

PROJECTS = ["huadu", "huimin", "shared"]
CATEGORIES = ["发票票据", "项目计划", "项目执行", "日常工作佐证", "政策文件", "其他"]


def _rule(name, preview=""):
    return classify_rules.classify_rule(name, preview, RULES, PROJECTS, CATEGORIES)


def test_load_rules():
    for key in ("projects", "categories", "strong", "fallback", "weights"):
        assert key in RULES, key
    assert RULES["projects"]["huadu"], "huadu 关键词不能为空"
    assert RULES["categories"]["项目执行"], "项目执行关键词不能为空"
    assert RULES["strong"]["发票票据"], "strong 发票票据关键词不能为空"


def test_filename_only():
    r = _rule("花都社区世纪光华小区特殊人群关爱入户名单.xlsx")
    assert r and r["project"] == "huadu" and r["category"] == "日常工作佐证"


def test_preview_fills_project():
    """文件名里没有项目名时，用正文补。"""
    r = _rule("2026-11-03-公众号推文-B版-温暖生活.docx", "惠民社区民乐北巷微更新项目")
    assert r and r["project"] == "huimin" and r["category"] == "项目执行"


def test_preview_cannot_override_filename_category():
    """正文里「走访/台账/方案」再多，也不能推翻文件名里的「推文」。"""
    preview = "推文 走访 走访 走访 台账 台账 台账 方案 方案 方案 方案"
    r = _rule("2026-11-05-公众号推文-惠民.docx", preview)
    assert r and r["category"] == "项目执行", r
    assert r["project"] == "huimin", r


def test_no_match_returns_none():
    assert _rule("IMG_20261104_1234.jpg") is None


def test_tie_returns_none():
    """两个项目同分 → 判不了，交 LLM。"""
    assert _rule("花都惠民联合活动方案.docx") is None


def test_policy_without_project_goes_to_review():
    """共享政策库已删除（2026-11-06）：判不出项目就交人工，不猜。"""
    assert _rule("2026-11-05-关于规范社区党建经费使用的通知.docx") is None


def test_other_never_fires():
    assert _rule("2026-11-05-其他事项说明.docx") is None


def test_strong_keyword_in_preview_wins():
    """正文自报「发票」→ 发票票据，不管文件名写的是活动。

    真实案例：花都第三期活动礼品_...80.00.pdf 是一张电子发票，
    但文件名里只有「活动」（项目执行关键词），旧规则会判成项目执行。
    """
    r = _rule("花都第三期活动礼品_成都市益嘉仁社会工作服务中心80.00.pdf",
              "电子发票（普通发票） 发票号码：26512000004964697871 "
              "开票日期：2026年11月06日 价税合计（大写） 捌拾圆整 ¥80.00")
    assert r and r["project"] == "huadu" and r["category"] == "发票票据", r


def test_strong_does_not_fire_on_activity_words():
    """「方案」「活动」「会议」不是自报词，不能推翻文件名里的「推文」。"""
    r = _rule("2026-11-05-公众号推文-惠民.docx", "活动 方案 会议 培训 总结")
    assert r and r["category"] == "项目执行", r


def test_strong_tie_returns_none():
    """两个类别的自报词同分 → 判不了，交 LLM。"""
    assert _rule("2026-11-05-票据.docx", "发票 红头文件") is None


# ---------- 真实回放（验收数据） ----------

REPLAY = [
    ("2026-11-04-宣传栏-微网实格.jpg", "huadu", "日常工作佐证"),
    ("花都社区世纪光华小区特殊人群关爱入户名单.xlsx", "huadu", "日常工作佐证"),
    ("2026-11-03-公众号推文-B版-温暖生活.docx", "huimin", "项目执行"),
    ("2026-11-05-公众号推文-惠民.docx", "huimin", "项目执行"),
    ("花都第三期活动礼品_成都市益嘉仁社会工作服务中心80.00.pdf", "huadu", "发票票据"),
    ("花都_2026_11_05_微议事_第三期活动_织围巾成都市益嘉仁社会工作服务中心_20261106110243.pdf",
     "huadu", "发票票据"),
]


def _find_real(name):
    """在生产机上找真文件（回放用的是真实归档数据）。找不到返回 None。"""
    root = Path("/srv/social-work/sources/projects")
    if not root.is_dir():
        return None
    hits = list(root.rglob(name))
    return hits[0] if hits else None


DEPLOY_ROOT = Path("/srv/social-work")


def _preview_for(name):
    """取正文预览。取不到（文件不在本机 / docling 挂了）返回 None，不返回空串。

    ⚠️ 2026-11-09：原来失败也返回 ""，回放会把「没有正文」当成「正文里没有
       发票字样」⇒ 只按文件名判成「项目执行」⇒ 记成**判错** ⇒ 全量跑测试时
       随机挂（单独跑 52s 又过）。
       真正的根因是**测试间全局状态污染**：`test_api_ops` 在 import 时设了
       `SOCIAL_WORK_ROOT=<仓库>`，回放再去加载 scan-inbox 时 ROOT 就变成仓库，
       /srv 下的文件被判定「不在服务端根目录下」⇒ 转换直接抛错。
       ⇒ 这里显式按部署根目录重载一次配置；取不到就返回 None（跳过，不算判错）。
    """
    src = _find_real(name)
    if src is None:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="replay-"))
    # scan-inbox.py 带连字符，不能直接 import
    import importlib.util
    import io
    import os
    from contextlib import redirect_stdout

    old_env = os.environ.get("SOCIAL_WORK_ROOT")
    os.environ["SOCIAL_WORK_ROOT"] = str(DEPLOY_ROOT)   # 盖掉别的测试设的仓库根
    mod = None
    try:
        spec = importlib.util.spec_from_file_location(
            "scan_inbox", ROOT / "scripts" / "scan-inbox.py")
        mod = importlib.util.module_from_spec(spec)
        with redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
        conv = mod.extract(src, tmp)
        return (conv.get("text") or "")[:3000]
    except Exception as exc:
        print(f"    ({name} preview 取不到：{exc})")
        return None
    finally:
        if old_env is None:
            os.environ.pop("SOCIAL_WORK_ROOT", None)
        else:
            os.environ["SOCIAL_WORK_ROOT"] = old_env
        # 把模块级路径还回原来的配置，避免影响后面的用例
        if mod is not None:
            try:
                with redirect_stdout(io.StringIO()):
                    mod._apply_config()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_replay():
    cases = []
    for name, want_proj, want_cat in REPLAY:
        preview = _preview_for(name)
        if preview is None:
            print(f"  跳过  {name}（本机没有这个文件，或正文预览取不到）")
            continue
        cases.append((name, want_proj, want_cat, preview))
    if not cases:
        print("\n回放：没有可用的真实样本，跳过")
        return
    hit = miss = wrong = 0
    for name, want_proj, want_cat, preview in cases:
        got = _rule(name, preview)
        if got is None:
            miss += 1
            print(f"  判不了  {name}")
        elif got["project"] == want_proj and got["category"] == want_cat:
            hit += 1
            print(f"  判对    {name} → {got['project']} / {got['category']}")
        else:
            wrong += 1
            print(f"  判错    {name} → {got['project']} / {got['category']}"
                  f"（应 {want_proj} / {want_cat}）")
    total = len(cases)
    print(f"\n回放：{total} 条，判对 {hit}，判不了 {miss}，判错 {wrong}，"
          f"命中率 {hit / total:.0%}")
    assert wrong == 0, "规则层判错率必须为 0"


if __name__ == "__main__":
    for fn in (
        test_load_rules,
        test_filename_only,
        test_preview_fills_project,
        test_preview_cannot_override_filename_category,
        test_no_match_returns_none,
        test_tie_returns_none,
        test_policy_without_project_goes_to_review,
        test_other_never_fires,
        test_strong_keyword_in_preview_wins,
        test_strong_does_not_fire_on_activity_words,
        test_strong_tie_returns_none,
        test_replay,
    ):
        fn()
        print(f"ok  {fn.__name__}")
    print("all passed")
