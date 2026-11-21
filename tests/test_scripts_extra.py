#!/usr/bin/env python3
"""运维脚本补测：只测不依赖外部 IO 的纯逻辑。

覆盖 9 个此前无测试（或只有编译/导入冒烟）的脚本：
    name_parse / check_activity / image_policy / refresh_readme /
    stats / retry / verify_assets / error_queue / compress_photos

约束（工单 B）：
  - 不写真实数据，输入一律 tmp_path
  - 不重启服务、不部署、不碰 /srv/social-work
  - 发现源码问题只报告，不改源码
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _load(name: str):
    """按文件路径加载 scripts/<name>.py（脚本名与标准库不冲突，但统一走这条路）。"""
    spec = importlib.util.spec_from_file_location(f"sw_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ======================================================================
# scripts/name_parse.py —— 文件名解析（最高优先）
# ======================================================================

np = _load("name_parse")

# 与 config/project_aliases.yaml 同构的别名表（真实配置由规则层加载）
ALIASES = {"huadu": ["花都", "世纪光华", "光华小区"],
           "huimin": ["惠民", "民乐北巷", "公平街道"]}
LABELS = {"huadu": "花都", "huimin": "惠民"}

# 取自 /srv/social-work/projects/惠民社区保障资金项目/活动/ 的真实文件名（只读采样）
REAL_WECHAT = "微信图片_20261110131250_175_11.jpg"
REAL_SIGNIN = "03-1-20261110_共治行动签到表_签名19行_签到人数栏空白.jpg"
REAL_INVOICE = ("2026-11-13_电子发票_610元_温江三圆广告制作部_"
                "26512000005077830706.pdf")
REAL_SURVEY = ("04-20261111惠民志愿服务——智慧菜市食品安全与健康饮食宣传"
               "志愿活动服务对象满意度调查表.md")


@pytest.mark.parametrize("name,expected", [
    (REAL_WECHAT, "2026-11-10"),          # 微信时间戳 14 位
    (REAL_SIGNIN, "2026-11-10"),          # 前缀 + 8 位紧凑日期
    (REAL_INVOICE, "2026-11-13"),          # 分隔符日期，后面还挂着 20 位税号
    (REAL_SURVEY, "2026-11-11"),
    ("2026-11-05.jpg", "2026-11-05"),
    ("2026_11_05.jpg", "2026-11-05"),
    ("2026年11月5日.jpg", "2026-11-05"),
    ("20261105.jpg", "2026-11-05"),         # 8 位紧凑
])
def test_parse_date_real_names(name, expected):
    """真实文件名 + 常见写法都要能取出日期。"""
    assert np.parse_date(name) == expected


@pytest.mark.parametrize("name", [
    "微信图片_20261110131250_175_11",       # 无后缀：stem 被 Path 吃掉「.5」式尾巴
    "2026-13-05",                           # 非法月
    "2026-11-32",                           # 非法日
    "26512000005077830706",                 # 20 位税号：不是 8/14 位日期
    "202611101312",                         # 12 位：不匹配 TS(14) 也不匹配 COMPACT(8)
    "",
])
def test_parse_date_rejects_bad(name):
    """取不到就返回空串，不能编造日期。"""
    assert np.parse_date(name) == ""


def test_parse_unit_prefers_longest_keyword():
    """「世纪光华」压过「花都」，命中词要更精确。"""
    assert np.parse_unit("花都-世纪光华-入户名单.jpg", ALIASES, LABELS) == \
        ("huadu", "花都", "世纪光华")


def test_parse_unit_no_keyword_and_label_fallback():
    assert np.parse_unit("无关文件.jpg", ALIASES, LABELS) == ("", "", "")
    # labels 缺 key 时回退成 key 本身
    assert np.parse_unit("惠民-走访.jpg", {"huimin": ["惠民"]}, {}) == \
        ("huimin", "huimin", "惠民")


def test_parse_content_real_signin():
    """真实签到表名：去掉噪声后应留下可读内容。"""
    info = np.parse(REAL_SIGNIN, ALIASES, LABELS)
    assert info["date"] == "2026-11-10"
    assert "共治行动签到表" in info["content"]
    assert "签到人数栏空白" in info["content"]


def test_parse_content_drops_noise_only_name():
    """微信图片名去掉日期/噪声后剩不下有意义的关键词。"""
    info = np.parse(REAL_WECHAT, ALIASES, LABELS)
    assert info["content"] in ("", "175-11")   # 只剩序号碎片也算「无内容」


def test_usable_and_describe():
    assert np.usable({"date": "2026-11-05"}) is True
    assert np.usable({"unit": "huimin"}) is True
    assert np.usable({}) is False
    assert np.usable(None) is False
    line = np.describe(np.parse("花都-2026年11月5日-织围巾登记.jpg", ALIASES, LABELS))
    assert line == "日期 2026-11-05 / 单位 花都 / 内容 织围巾登记"


# ======================================================================
# scripts/check_activity.py —— 活动完整性检查逻辑
# ======================================================================

ca = _load("check_activity")


def _make_project(root: Path, name: str = "测试项目",
                  activity: str = "01-测试活动") -> Path:
    """在 tmp 下造一个带 yaml 的项目 + 活动目录。"""
    act = root / name / "活动" / activity
    act.mkdir(parents=True, exist_ok=True)
    (root / name / f"{name}.yaml").write_text(
        f"project:\n  short: {name}\n", encoding="utf-8")
    return act


@pytest.mark.parametrize("name", [
    "2026-13-05",                           # 非法月
    "2026-11-32",                           # 非法日
    "26512000005077830706",                 # 20 位税号：不是 8/14 位日期
    "202611101312",                         # 12 位：不匹配 TS(14) 也不匹配 COMPACT(8)
    "",
])
def test_parse_date_rejects_bad(name):
    """取不到就返回空串，不能编造日期。"""
    assert np.parse_date(name) == ""


def test_parse_date_dotted_date_without_suffix_is_lost():
    """源码问题（只报告不改）：无扩展名时 Path.stem 把「日」当成后缀吃掉。

    `2026.11.5.jpg` → 2026-11-05；裸名 `2026.11.5` → ''。
    微信时间戳写法（..._175_11）不受影响，因为最后一段是 `_` 不是 `.`。
    """
    assert np.parse_date("2026.11.5") == ""
    assert np.parse_date("2026.11.5.jpg") == "2026-11-05"

def test_resolve_missing_returns_none(tmp_path, monkeypatch, capsys):
    """路径不存在返回 None；报错在 _targets 里（main 才打 stderr）。"""
    monkeypatch.setattr(ca.projects, "PROJECTS_ROOT", tmp_path)
    assert ca._resolve("不存在的项目/不存在的活动") is None
    assert ca._targets(False, "不存在的项目/不存在的活动") == []
    assert "活动目录不存在" in capsys.readouterr().err


def test_targets_all_projects(tmp_path, monkeypatch):
    """--all 收集所有项目的所有活动，标签为「项目 / 活动」。"""
    _make_project(tmp_path, "甲项目", "01-活动一")
    _make_project(tmp_path, "甲项目", "02-活动二")
    _make_project(tmp_path, "乙项目", "01-活动一")
    monkeypatch.setattr(ca.projects, "PROJECTS_ROOT", tmp_path)
    targets = ca._targets(True, "")
    assert len(targets) == 3
    # 项目顺序按目录名排序，标签是「项目 / 活动」
    assert {label for label, _ in targets} == {
        "甲项目 / 01-活动一", "甲项目 / 02-活动二", "乙项目 / 01-活动一"}
    assert all(p.is_dir() for _, p in targets)


def test_main_reports_complete(tmp_path, monkeypatch, capsys):
    """完整活动（14 项齐 + 第 15 项不适用）→ complete，退出码 0。"""
    act = _make_project(tmp_path)
    (act / "activity.yaml").write_text("code: X\nstatus: imported\n", encoding="utf-8")
    # 文件类：01 02 05 06 07 08
    for pre in ("01-", "02-", "05-", "06-", "07-", "08-"):
        (act / f"{pre}x.docx").write_text("x", encoding="utf-8")
    # 签字类：03 09 04
    (act / "03-签到表.jpg").write_bytes(b"jpg")
    (act / "09-监督记录表.pdf").write_bytes(b"pdf")
    (act / "04-满意度调查表.jpg").write_bytes(b"jpg")
    # 报销单 xlsx（09-/21-）
    (act / "21-报销单.xlsx").write_text("x", encoding="utf-8")
    # 目录类：10 11 有图，12 有文件，13 非空
    (act / "10-物资照片资料").mkdir()
    (act / "10-物资照片资料" / "a.jpg").write_bytes(b"j")
    (act / "11-活动照片资料").mkdir()
    (act / "11-活动照片资料" / "b.jpg").write_bytes(b"j")
    (act / "12-发票").mkdir()
    (act / "12-发票" / "c.pdf").write_bytes(b"p")
    (act / "13-其他佐证").mkdir()
    (act / "13-其他佐证" / "d.txt").write_text("t", encoding="utf-8")

    monkeypatch.setattr(ca.projects, "PROJECTS_ROOT", tmp_path)
    assert ca.main([str(act)]) == 0
    out = capsys.readouterr().out
    assert "合计" in out and "完整 1 个" in out


def test_main_missing_dir_exit_1(tmp_path, monkeypatch, capsys):
    """目录不存在 → 无目标，退出码 1。"""
    monkeypatch.setattr(ca.projects, "PROJECTS_ROOT", tmp_path)
    assert ca.main([str(tmp_path / "nope")]) == 1
    assert "活动目录不存在" in capsys.readouterr().err


# ======================================================================
# scripts/image_policy.py —— 图片取舍规则
# ======================================================================

ip = _load("image_policy")
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _policy(tmp_path: Path, text: str) -> dict:
    (tmp_path / "image_policy.yaml").write_text(text, encoding="utf-8")
    return ip.load_policy(tmp_path)


def test_load_policy_missing_file_returns_default(tmp_path):
    pol = ip.load_policy(tmp_path)
    assert pol["rules"] == []
    assert pol["default"] == "archive_only"


def test_load_policy_bad_yaml_returns_default(tmp_path):
    pol = _policy(tmp_path, ": : : 坏掉的\n\t- [")
    assert pol["default"] == "archive_only"
    assert pol["rules"] == []


def test_decide_action_rules_and_enabled(tmp_path):
    pol = _policy(tmp_path, (
        "rules:\n"
        "  - dir: '要OCR'\n    action: ocr\n"
        "  - dir: '要识图'\n    action: vision\n    enabled: false\n"
        "default: archive_only\n"))
    assert ip.decide_action("要OCR/a.jpg", ".jpg", pol, IMG_EXTS) == "ocr"
    # 禁用规则跳过 → 落到 default
    assert ip.decide_action("要识图/a.jpg", ".jpg", pol, IMG_EXTS) == "archive_only"
    # 未命中任何规则 → default
    assert ip.decide_action("其它/a.jpg", ".jpg", pol, IMG_EXTS) == "archive_only"


def test_decide_action_non_image_and_none():
    """非图片 / 无策略 → None（走原流程，不进策略分支）。"""
    pol = {"rules": [{"dir": "要OCR", "action": "ocr"}], "default": "archive_only"}
    assert ip.decide_action("要OCR/a.pdf", ".pdf", pol, IMG_EXTS) is None
    assert ip.decide_action("要OCR/a.jpg", ".jpg", None, IMG_EXTS) is None


def test_decide_action_first_segment_and_case_sensitive():
    """只按相对路径第一段判定；扩展名大小写敏感。"""
    pol = {"rules": [{"dir": "X", "action": "ocr"}], "default": "archive_only"}
    assert ip.decide_action("X/子目录/a.jpg", ".jpg", pol, IMG_EXTS) == "ocr"
    # 大写后缀不在 image_exts 里 → 视为非图片
    assert ip.decide_action("X/a.JPG", ".JPG", pol, IMG_EXTS) is None


# ======================================================================
# scripts/refresh_readme.py —— 索引生成
# ======================================================================


@pytest.fixture
def readme_env(tmp_path, monkeypatch):
    """把项目根指到 tmp。

    engine.projects 的 PROJECTS_ROOT 是**模块级常量、导入时读一次环境变量**，
    且被 engine.readme / engine.doc_mirror 共享。所以：
      1) 直接给已在 sys.modules 里的 engine.projects 打补丁（所有引用同一对象）；
      2) 删掉 engine.readme / doc_mirror 缓存，让它们按新根重新绑定。
    """
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    monkeypatch.setattr("engine.projects.PROJECTS_ROOT", projects_root)
    for mod in list(sys.modules):
        if mod in ("engine.readme", "engine.doc_mirror"):
            del sys.modules[mod]
    rr = _load("refresh_readme")
    assert rr.readme.projects.PROJECTS_ROOT == projects_root
    assert rr.doc_mirror.projects.PROJECTS_ROOT == projects_root
    return rr, projects_root


def _seed_project(projects_root: Path, name: str = "P",
                  activity: str = "A") -> Path:
    proj = projects_root / name
    act = proj / "活动" / activity
    act.mkdir(parents=True)
    (act / "activity.yaml").write_text("code: X\nstatus: imported\n", encoding="utf-8")
    (act / "01-x.docx").write_text("x" * 10, encoding="utf-8")
    return proj


def test_refresh_all_writes_then_idempotent(readme_env):
    rr, root = readme_env
    _seed_project(root)
    first = rr.readme.refresh_all()
    assert len(first["written"]) == 2            # 活动索引 + 项目索引
    assert first["errors"] == []
    second = rr.readme.refresh_all()
    assert second["written"] == [] and second["skipped"] == 2


def test_refresh_all_dry_run_does_not_write(readme_env):
    rr, root = readme_env
    _seed_project(root)
    result = rr.readme.refresh_all(dry_run=True)
    assert len(result["written"]) == 2
    act = root / "P" / "活动" / "A"
    assert not (act / "_A-资料索引.md").exists()


def test_refresh_all_missing_project_root(readme_env, tmp_path):
    rr, _ = readme_env
    # 把根指向不存在目录：报错但不抛
    rr.readme.projects.PROJECTS_ROOT = tmp_path / "nope"
    result = rr.readme.refresh_all()
    assert result["written"] == [] and result["skipped"] == 0
    assert any("项目根目录不存在" in e for e in result["errors"])


def test_cli_main_refreshes_and_mirrors(readme_env, capsys):
    """CLI 入口：README 索引 + docx 同名 md 骨架一起产出。"""
    rr, root = readme_env
    _seed_project(root)
    assert rr.main([]) == 0
    out = capsys.readouterr().out
    assert "写入 2 个" in out
    act = root / "P" / "活动" / "A"
    assert (act / "01-x.md").is_file()


def test_cli_main_unknown_project_exit_1(readme_env, capsys):
    rr, _ = readme_env
    assert rr.main(["--project", "不存在"]) == 1
    err = capsys.readouterr().err
    assert "项目目录不存在" in err


# ======================================================================
# scripts/stats.py —— 统计计数
# ======================================================================

st = _load("stats")


def test_count_paths_reads_openapi(tmp_path):
    """起一个本地 http 服务喂 openapi.json，验证数路径条数。"""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                       # noqa: N802
            body = json.dumps({"paths": {"/a": {}, "/b": {}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):           # 静默
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/openapi.json"
        assert st.count_paths(url) == 2
    finally:
        srv.shutdown()


def test_count_paths_service_down_returns_none():
    assert st.count_paths("http://127.0.0.1:59998/openapi.json") is None


def test_count_tests_parses_summary(monkeypatch):
    """pytest 汇总行解析：passed / failed+error / skipped 分别累加。"""

    class FakeResult:
        stdout = "1 failed, 643 passed, 2 skipped, 1 error in 3.20s\n"

    def fake_run(*args, **kwargs):
        return FakeResult()

    monkeypatch.setattr(st.subprocess, "run", fake_run)
    assert st.count_tests() == (643, 2, 2)   # failed 1 + error 1 = 2


def test_count_tests_no_summary_returns_none(monkeypatch):
    class FakeResult:
        stdout = "no summary here\n"

    monkeypatch.setattr(st.subprocess, "run", lambda *a, **k: FakeResult())
    assert st.count_tests() is None


# ======================================================================
# scripts/retry.py —— 重试策略（此前只在 legacy 里做过编译/导入冒烟）
# ======================================================================

rt = _load("retry")


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    OSError("Connection reset by peer"),
    RuntimeError("502 Bad Gateway"),
    Exception("temporarily unavailable"),
    Exception("rate limit exceeded"),
])
def test_is_retryable_true(exc):
    assert rt.is_retryable(exc) is True


@pytest.mark.parametrize("exc", [
    ValueError("文件格式不支持"),
    RuntimeError("权限不足"),
    KeyError("missing"),
])
def test_is_retryable_false(exc):
    """格式/权限类错误不重试，立即抛。"""
    assert rt.is_retryable(exc) is False


def test_retry_first_try_ok():
    calls = []
    assert rt.retry(lambda: calls.append(1) or "ok", sleep=lambda s: None) == "ok"
    assert len(calls) == 1


def test_retry_recovers_after_transient():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError("timed out")
        return "done"

    sleeps = []
    assert rt.retry(flaky, attempts=3, base_delay=2.0,
                    sleep=sleeps.append) == "done"
    assert sleeps == [2.0, 4.0]              # 指数退避 2s → 4s


def test_retry_gives_up_and_raises():
    calls = []

    def always():
        calls.append(1)
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        rt.retry(always, attempts=2, sleep=lambda s: None)
    assert len(calls) == 2


def test_retry_non_retryable_raises_immediately():
    """不可重试异常一次都不该多跑。"""
    calls = []

    def bad():
        calls.append(1)
        raise ValueError("坏文件")

    with pytest.raises(ValueError):
        rt.retry(bad, attempts=5, sleep=lambda s: None)
    assert len(calls) == 1


def test_retry_log_callback():
    logs = []

    def flaky():
        if len(logs) == 0:
            raise TimeoutError("timed out")
        return "ok"

    rt.retry(flaky, attempts=2, base_delay=1.0, label="拉取未分类",
             log=logs.append, sleep=lambda s: None)
    assert any("拉取未分类" in ln and "重试" in ln for ln in logs)


# ======================================================================
# scripts/verify_assets.py —— 门禁脚本自身的判定逻辑
# ======================================================================

va = _load("verify_assets")


@pytest.fixture
def fake_sw_root(tmp_path, monkeypatch):
    """SOCIAL_WORK_ROOT 指向 tmp，重新加载 verify_assets（ROOT 是模块级常量）。"""
    monkeypatch.setenv("SOCIAL_WORK_ROOT", str(tmp_path))
    monkeypatch.setenv("SOCIAL_WORK_TEST_REPO", str(tmp_path / "repo"))
    mod = _load("verify_assets")
    assert mod.ROOT == tmp_path
    return mod, tmp_path


def _full_assets(root: Path) -> None:
    t = root / "templates" / "library"
    t.mkdir(parents=True)
    for i in range(10):
        (t / f"t{i}.docx").write_text("x", encoding="utf-8")
    (root / "projects" / "P1" / "活动").mkdir(parents=True)


def test_check_assets_all_present(fake_sw_root):
    mod, root = fake_sw_root
    _full_assets(root)
    assert mod.check_assets() == []


def test_check_assets_missing_dirs(fake_sw_root):
    mod, root = fake_sw_root
    missing = mod.check_assets()
    assert any("模板目录不存在" in m for m in missing)
    assert any("项目目录不存在" in m for m in missing)


def test_check_assets_too_few_templates(fake_sw_root):
    mod, root = fake_sw_root
    _full_assets(root)
    (root / "templates" / "library" / "t9.docx").unlink()   # 只剩 9 个
    missing = mod.check_assets()
    assert any("模板数量异常" in m and "9" in m for m in missing)


def test_check_assets_no_activity_dir(fake_sw_root):
    """项目存在但没有「活动」子目录 → 报缺失。"""
    mod, root = fake_sw_root
    _full_assets(root)
    import shutil
    shutil.rmtree(root / "projects" / "P1" / "活动")
    missing = mod.check_assets()
    assert any("没有「活动」目录" in m for m in missing)


def test_run_tests_without_repo_dir(fake_sw_root, tmp_path):
    """REPO 下没有 tests/ → 明确报「找不到测试目录」，而不是静默返回 0 通过。"""
    mod, root = fake_sw_root
    _full_assets(root)
    passed, failed, skipped, names = mod.run_tests()
    assert (passed, failed, skipped) == (0, 0, 0)
    assert any("找不到测试目录" in n for n in names)


# ======================================================================
# scripts/error_queue.py —— 错误队列（幂等 / 关闭 / 流水）
# ======================================================================

eq = _load("error_queue")


def test_error_id_stable_and_distinct():
    a = eq.error_id("a.pdf", "转换失败")
    assert a == eq.error_id("a.pdf", "转换失败")
    assert a != eq.error_id("a.pdf", "另一个错")
    assert len(a) == 16


@pytest.mark.parametrize("exc,expected", [
    (Exception("line1\nline2\nline3"), "line1"),
    ("  直接字符串  ", "直接字符串"),
    (Exception(""), "Exception"),      # 空消息退回类型名
])
def test_brief_error(exc, expected):
    assert eq.brief_error(exc) == expected


def test_add_dedup_and_trail(tmp_path):
    """同一 (file, error) 只入队一次，但流水每次都记。"""
    e1, is_new = eq.add(tmp_path, file="a.pdf", error="转换失败")
    assert is_new is True
    e2, is_new2 = eq.add(tmp_path, file="a.pdf", error="转换失败")
    assert is_new2 is False
    assert eq.count_open(tmp_path) == 1
    trail = eq.trail_path(tmp_path)
    assert trail.is_file()
    assert len(trail.read_text(encoding="utf-8").splitlines()) == 2   # 流水两次都留


def test_find_open_by_id_or_file(tmp_path):
    e, _ = eq.add(tmp_path, file="f.pdf", error="boom")
    assert eq.find_open(tmp_path, eid=e["id"])["file"] == "f.pdf"
    assert eq.find_open(tmp_path, file="f.pdf")["id"] == e["id"]
    assert eq.find_open(tmp_path, eid="deadbeef") is None


def test_resolve_removes_once(tmp_path):
    e, _ = eq.add(tmp_path, file="f.pdf", error="boom")
    assert eq.resolve(tmp_path, e["id"]) is True
    assert eq.resolve(tmp_path, e["id"]) is False
    assert eq.count_open(tmp_path) == 0


def test_close_by_file(tmp_path):
    eq.add(tmp_path, file="a.pdf", error="e1")
    eq.add(tmp_path, file="a.pdf", error="e2")
    eq.add(tmp_path, file="b.pdf", error="e3")
    assert eq.close_by_file(tmp_path, "a.pdf") == 2
    assert eq.count_open(tmp_path) == 1
    assert eq.close_by_file(tmp_path, "不存在的文件") == 0


def test_read_open_tolerates_bad_yaml(tmp_path):
    """open.yaml 坏了按空队列处理，不能让整轮扫描崩。"""
    d = eq.errors_dir(tmp_path)
    d.mkdir(parents=True)
    eq.open_path(tmp_path).write_text(": : : 坏\n\t- [", encoding="utf-8")
    assert eq._read_open(tmp_path) == []
    assert eq.count_open(tmp_path) == 0


# ======================================================================
# scripts/compress_photos.py —— 压缩参数选择（只测筛选，不真压）
# ======================================================================

cp = _load("compress_photos")


def test_dir_bytes_counts_jpg_only(tmp_path):
    """只算 .jpg/.jpeg，排除 png 与索引图。"""
    pd = tmp_path / "11-活动照片资料"
    pd.mkdir()
    (pd / "a.jpg").write_bytes(b"x" * 100)
    (pd / "b.jpeg").write_bytes(b"y" * 50)
    (pd / "c.png").write_bytes(b"z" * 20)
    (pd / cp._photos.CONTACT_SHEET_NAME).write_bytes(b"i" * 10)
    assert cp._dir_bytes(pd) == (2, 150)


def test_dir_bytes_missing_dir(tmp_path):
    assert cp._dir_bytes(tmp_path / "nope") == (0, 0)


def test_iter_photo_dirs_filters(tmp_path, monkeypatch):
    """只收 PHOTO_DIRS 下的目录，可按活动名过滤。"""
    act = tmp_path / "P" / "活动" / "01-甲"
    (act / "11-活动照片资料").mkdir(parents=True)
    (act / "99-别的").mkdir()
    monkeypatch.setattr(cp._projects, "PROJECTS_ROOT", tmp_path)
    hits = [(n, a.name, p.name) for n, a, p in cp._iter_photo_dirs(["P"], "")]
    assert hits == [("P", "01-甲", "11-活动照片资料")]
    assert list(cp._iter_photo_dirs(["P"], "99-x")) == []
    assert list(cp._iter_photo_dirs(["不存在的项目"], "")) == []


# ======================================================================
# 明确不测的脚本（工单指定）：依赖外部服务或纯 CLI 包装，无独立纯逻辑可测。
#   gen_brief / gen_plan        依赖 LLM
#   sync_feishu                 依赖飞书 API
#   index_cognee / ingest_activities  依赖 Cognee 容器
#   convert-md                  依赖 pandoc / docling 容器
#   autofill_plan_xlsx / fill_plan_from_xlsx  纯 CLI 包装
# ======================================================================
