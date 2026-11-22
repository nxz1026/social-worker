#!/usr/bin/env python3
"""飞书运维脚本补测：只测纯逻辑，绝不真调飞书。

覆盖 4 个此前无测试的脚本：
    check_feishu_auth / sync_feishu / index_cognee / ingest_activities

约束（工单 C）：
  - 只新增本文件，不改任何现有文件
  - subprocess.run / lark() / child_folders() 一律 monkeypatch
  - 不写真实数据，输入一律 tmp_path；不重启服务、不部署、不碰 /srv/social-work
  - 发现源码 bug 只在 test_feishu_bug_reports 里如实断言当前行为，不改正源码
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
    """按文件路径加载 scripts/<name>.py。

    必须注册进 sys.modules：index_cognee 用 @dataclass，类注解求值时要按
    __module__ 回查模块字典，否则抛 AttributeError。
    """
    spec = importlib.util.spec_from_file_location(f"sw_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"sw_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cf():
    """check_feishu_auth 模块；每例都重新加载，CONFIG 是模块级常量。"""
    return _load("check_feishu_auth")


class _R:
    """假的 subprocess.CompletedProcess。"""

    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


# ======================================================================
# scripts/check_feishu_auth.py —— 飞书登录健康检查
# ======================================================================

def test_配置不存在_返回码2(cf, tmp_path):
    cf.CONFIG = tmp_path / "nope.yaml"
    code, res = cf.check()
    assert code == 2
    assert res["ok"] is False
    assert "配置不存在" in res["reason"]


def test_配置解析失败_返回码2(cf, tmp_path):
    bad = tmp_path / "feishu-flow.yaml"
    bad.write_text("bad: [\n", encoding="utf-8")
    cf.CONFIG = bad
    code, res = cf.check()
    assert code == 2
    assert "配置解析失败" in res["reason"]


def test_缺feishu_root_返回码2(cf, tmp_path):
    cfg = tmp_path / "feishu-flow.yaml"
    cfg.write_text("other:\n  root: x\n", encoding="utf-8")
    cf.CONFIG = cfg
    code, res = cf.check()
    assert code == 2
    assert res["reason"] == "配置缺 feishu.root"


def test_lark成功_返回码0且ok(cf, tmp_path, monkeypatch):
    cfg = tmp_path / "feishu-flow.yaml"
    cfg.write_text('feishu:\n  root: "FOLDER123"\n', encoding="utf-8")
    cf.CONFIG = cfg
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _R(json.dumps({"ok": True, "data": {"files": [{"name": "制度.docx"}]}}))

    monkeypatch.setattr(cf.subprocess, "run", fake_run)
    code, res = cf.check()
    assert code == 0 and res["ok"] is True
    assert res["root"] == "FOLDER123"
    assert res["sample"] == ["制度.docx"]
    # folder_token 在 --params 的 JSON 串里，不是独立 argv
    assert any("FOLDER123" in str(a) for a in seen["cmd"])
    assert "--json" in seen["cmd"]


@pytest.mark.parametrize("msg", ["token expired", "unauthorized", "no permission",
                                 "identity mismatch"])
def test_认证类错误_返回码1且need_login(cf, tmp_path, monkeypatch, msg):
    cfg = tmp_path / "feishu-flow.yaml"
    cfg.write_text('feishu:\n  root: "R"\n', encoding="utf-8")
    cf.CONFIG = cfg
    payload = json.dumps({"ok": False, "error": {"message": msg, "code": 9999}})
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **k: _R(payload))
    code, res = cf.check()
    assert code == 1
    assert res["ok"] is False and res["need_login"] is True and res["auth_hint"] is True
    assert "lark-cli auth login" in res["hint"]


def test_其它错误_返回码2(cf, tmp_path, monkeypatch):
    cfg = tmp_path / "feishu-flow.yaml"
    cfg.write_text('feishu:\n  root: "R"\n', encoding="utf-8")
    cf.CONFIG = cfg
    payload = json.dumps({"ok": False, "error": {"message": "网络超时", "code": 500}})
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **k: _R(payload))
    code, res = cf.check()
    assert code == 2
    assert res["need_login"] is False and res["hint"] == ""


def test_lark命令不存在_不崩返回码2(cf, tmp_path, monkeypatch):
    cfg = tmp_path / "feishu-flow.yaml"
    cfg.write_text('feishu:\n  root: "R"\n', encoding="utf-8")
    cf.CONFIG = cfg

    def boom(*a, **k):
        raise FileNotFoundError("lark-cli")

    monkeypatch.setattr(cf.subprocess, "run", boom)
    code, res = cf.check()
    assert code == 2
    assert res["ok"] is False and res["need_login"] is False


def test_超时和坏JSON同样兜底(cf, tmp_path, monkeypatch):
    import subprocess as _sp

    cfg = tmp_path / "feishu-flow.yaml"
    cfg.write_text('feishu:\n  root: "R"\n', encoding="utf-8")
    cf.CONFIG = cfg

    def timeout(*a, **k):
        raise _sp.TimeoutExpired("lark-cli", 90)

    monkeypatch.setattr(cf.subprocess, "run", timeout)
    assert cf.check()[0] == 2
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **k: _R("not json at all"))
    assert cf.check()[0] == 2


def test_main输出与退出码一致(cf, tmp_path, monkeypatch, capsys):
    """现状记录：check() 已返回退出码，但 --json 输出里的 "code" 是错误码
    字段（code = err.get("code") or err.get("type")），两者不是一回事。"""
    cfg = tmp_path / "feishu-flow.yaml"
    cfg.write_text('feishu:\n  root: "R"\n', encoding="utf-8")
    cf.CONFIG = cfg
    payload = json.dumps({"ok": False, "error": {"message": "token expired"}})
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **k: _R(payload))
    rc = cf.main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1                       # main 的返回码是对的
    assert out["need_login"] is True
    assert out["code"] == ""             # BUG：JSON 里没有进程退出码，恒为错误码字段


# ======================================================================
# scripts/sync_feishu.py —— 本地项目镜像到飞书
# ======================================================================

@pytest.fixture()
def sf():
    return _load("sync_feishu")


def _mkprojects(root: Path, names=("P1", "P2")) -> Path:
    for n in names:
        (root / n).mkdir(parents=True, exist_ok=True)
    (root / ".hidden").mkdir(exist_ok=True)
    return root


def test_项目目录映射_全部与过滤(sf, tmp_path, monkeypatch):
    root = _mkprojects(tmp_path)
    monkeypatch.setattr(sf, "ROOT", root)
    assert [p.name for p in sf._project_dirs("")] == ["P1", "P2"]
    assert [p.name for p in sf._project_dirs(None)] == ["P1", "P2"]
    assert [p.name for p in sf._project_dirs("P1")] == ["P1"]
    assert [p.name for p in sf._project_dirs(["P2", " P1 ", ""])] == ["P1", "P2"]
    # 不存在的项目只是筛掉，不报错
    assert sf._project_dirs("不存在的项目") == []


def test_项目名越界被当作不存在(sf, tmp_path, monkeypatch):
    root = _mkprojects(tmp_path)
    monkeypatch.setattr(sf, "ROOT", root)
    for bad in ("..", "../x", "a/b", "/etc", "."):
        assert sf._project_dirs(bad) == [], bad


def test_空项目根不崩(sf, tmp_path, monkeypatch):
    empty = tmp_path / "empty-root"
    empty.mkdir()
    monkeypatch.setattr(sf, "ROOT", empty)
    assert sf._project_dirs("") == []
    assert sf.sync("", dry_run=True) == []


def test_child_folders只收文件夹(sf, monkeypatch):
    seen = {}

    def fake_lark(*args, **kw):
        seen["args"] = args
        return {"data": {"files": [
            {"name": "甲", "token": "t1", "type": "folder"},
            {"name": "乙.docx", "token": "t2", "type": "file"},
            {"name": "丙", "token": "t3", "type": "folder"},
        ]}}

    monkeypatch.setattr(sf, "lark", fake_lark)
    assert sf.child_folders("PARENT") == {"甲": "t1", "丙": "t3"}
    assert seen["args"][:3] == ("files", "list", "--folder-token")


def test_child_folders调用失败不抛异常(sf, monkeypatch):
    """lark() 拿不到 JSON 时返回没有 data 的字典，child_folders 应给空字典。"""
    monkeypatch.setattr(sf, "lark", lambda *a, **k: {"ok": False, "raw": "boom"})
    assert sf.child_folders("X") == {}


def test_ensure已存在则复用token(sf, monkeypatch):
    monkeypatch.setattr(sf, "child_folders", lambda parent: {"甲": "TOK"})
    monkeypatch.setattr(sf, "lark", lambda *a, **k: pytest.fail("不该再创建目录"))
    assert sf.ensure("甲") == {"name": "甲", "created_dir": False, "folder_token": "TOK"}


def test_ensure新建走lark并回传token(sf, monkeypatch, capsys):
    monkeypatch.setattr(sf, "child_folders", lambda parent: {})
    calls = []

    def fake_lark(*args, **kw):
        calls.append((args, kw))
        return {"data": {"folder_token": "NEW"}}

    monkeypatch.setattr(sf, "lark", fake_lark)
    res = sf.ensure("新项目")
    assert res == {"name": "新项目", "created_dir": True, "folder_token": "NEW"}
    args, kw = calls[0]
    assert "+create-folder" in args and kw["timeout"] == 120


def test_ensure创建失败不算建成(sf, monkeypatch):
    monkeypatch.setattr(sf, "child_folders", lambda parent: {})
    monkeypatch.setattr(sf, "lark", lambda *a, **k: {"ok": False})
    assert sf.ensure("P") == {"name": "P", "created_dir": False, "folder_token": None}


def test_ensure_dry_run不写飞书(sf, monkeypatch):
    monkeypatch.setattr(sf, "child_folders", lambda parent: {})
    monkeypatch.setattr(sf, "lark", lambda *a, **k: pytest.fail("dry-run 不该调飞书"))
    assert sf.ensure("P", dry_run=True) == {
        "name": "P", "created_dir": False, "folder_token": None}


def test_push无token直接短路(sf, tmp_path, monkeypatch):
    """folder_token=None 会先走 ensure()；ensure 读远端目录，失败时返回空 token，
    push 随即短路。这里顶掉 child_folders 与 lark，隔离出纯逻辑分支。"""
    root = _mkprojects(tmp_path)
    monkeypatch.setattr(sf, "ROOT", root)
    monkeypatch.setattr(sf, "child_folders", lambda parent: {})
    monkeypatch.setattr(sf, "lark", lambda *a, **k: {"ok": False})
    assert sf.push("P1", folder_token="") == {
        "name": "P1", "pushed": False, "uploaded": None, "skipped": None}
    # ensure 拿不到 token（远端读失败）→ push 同样短路，不碰本地目录
    assert sf.push("根本不存在的项目", folder_token=None) == {
        "name": "根本不存在的项目", "pushed": False, "uploaded": None,
        "skipped": None}


def test_push非dry_run且目录不存在_抛原始异常(sf, tmp_path, monkeypatch):
    """现状记录：非 dry-run 时目录不存在会一路穿到 shutil.copytree，
    抛原生 FileNotFoundError（无业务提示）。"""
    root = _mkprojects(tmp_path)
    monkeypatch.setattr(sf, "ROOT", root)
    monkeypatch.setattr(sf, "child_folders", lambda parent: {})
    monkeypatch.setattr(sf, "lark", lambda *a, **k: {"data": {"folder_token": "T"}})
    monkeypatch.setattr(sf, "REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        sf.push("根本不存在的项目")


def test_push_dry_run不读本地目录(sf, tmp_path, monkeypatch):
    """项目目录不存在也不能崩：dry-run 在读目录之前就返回。"""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(sf, "ROOT", root)
    monkeypatch.setattr(sf, "child_folders", lambda parent: {})
    assert sf.push("根本不存在", folder_token="T", dry_run=True) == {
        "name": "根本不存在", "pushed": False, "uploaded": None, "skipped": None}


def test_stage_project剔除同步元数据(sf, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".obsidian").mkdir(parents=True)
    (proj / "方案").mkdir()
    (proj / "方案" / "申报书.docx").write_text("x", encoding="utf-8")
    (proj / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (proj / ".stignore").write_text("", encoding="utf-8")
    (proj / "Thumbs.db").write_text("", encoding="utf-8")
    monkeypatch.setattr(sf, "REPO_ROOT", tmp_path)
    try:
        staged = sf._stage_project(proj)
        names = {p.relative_to(staged).as_posix() for p in staged.rglob("*")}
        assert names == {"方案", "方案/申报书.docx"}
    finally:
        # push() 会 rmtree(staged.parent)，这里手动清，避免污染仓库 state/
        import shutil
        shutil.rmtree(staged.parent, ignore_errors=True)


def test_stage_project源目录不存在直接抛(sf, tmp_path, monkeypatch):
    """现状：copytree 直接 FileNotFoundError，不做友好提示。"""
    monkeypatch.setattr(sf, "REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        sf._stage_project(tmp_path / "nope")


def test_prune_feishu_meta删元数据(sf, monkeypatch):
    """现状记录：删掉元数据**目录本身**后没有递归进去，目录内的 .stversions
    不会被清理（返回值只数了 2，不是 3）。断言的是当前行为。"""
    deleted = []
    pages = {
        "ROOT": [{"name": ".obsidian", "type": "folder", "token": "d1"},
                 {"name": ".stignore", "type": "file", "token": "f1"},
                 {"name": "方案", "type": "folder", "token": "keep"},
                 {"name": "无token", "type": "file"}],
        "d1": [{"name": ".stversions", "type": "folder", "token": "d2"}],
    }

    def fake_list(tok):
        return pages.get(tok, [])

    def fake_run(cmd, **kw):
        deleted.append(cmd)
        return _R(json.dumps({"ok": True}))

    monkeypatch.setattr(sf, "_list_children", fake_list)
    monkeypatch.setattr(sf.subprocess, "run", fake_run)
    assert sf._prune_feishu_meta("ROOT") == 2          # BUG：嵌套元数据没清到
    kinds = {(c[c.index("--file-token") + 1], c[c.index("--type") + 1]) for c in deleted}
    assert kinds == {("d1", "folder"), ("f1", "file")}
    # 无 token 的条目跳过；正常目录「方案」不被删
    assert "keep" not in {c[c.index("--file-token") + 1] for c in deleted}


def test_prune_normal目录仍递归(sf, monkeypatch):
    """普通目录（如「方案」）里的历史元数据会被递归清掉。"""
    deleted = []
    pages = {
        "ROOT": [{"name": "方案", "type": "folder", "token": "k"}],
        "k": [{"name": ".stfolder", "type": "folder", "token": "s"}],
        "s": [],
    }
    monkeypatch.setattr(sf, "_list_children", lambda tok: pages.get(tok, []))
    monkeypatch.setattr(sf.subprocess, "run",
                        lambda cmd, **kw: (deleted.append(cmd), _R('{"ok": true}'))[1])
    assert sf._prune_feishu_meta("ROOT") == 1


def test_prune深度上限防环(sf, monkeypatch):
    monkeypatch.setattr(sf, "_list_children", lambda tok: [])
    monkeypatch.setattr(sf.subprocess, "run", lambda *a, **k: pytest.fail("不该删除"))
    assert sf._prune_feishu_meta("X", depth=9) == 0


def test_sync远端根不可读抛RuntimeError(sf, tmp_path, monkeypatch):
    root = _mkprojects(tmp_path)
    monkeypatch.setattr(sf, "ROOT", root)
    monkeypatch.setattr(sf, "child_folders", lambda parent: {})
    with pytest.raises(RuntimeError, match="益嘉仁"):
        sf.sync(["P1"], dry_run=False)


def test_sync_dry_run逐项规划(sf, tmp_path, monkeypatch, capsys):
    """现状记录：dry-run 下 push() 内部会自己再调一次 ensure()，于是
    「将创建目录」打印两遍。断言的是当前输出。"""
    root = _mkprojects(tmp_path)
    monkeypatch.setattr(sf, "ROOT", root)
    monkeypatch.setattr(sf, "child_folders", lambda parent: {})
    out = sf.sync(["P1"], dry_run=True)
    assert out == [{"name": "P1", "created_dir": False, "pushed": False}]
    text = capsys.readouterr().out
    assert text.count("将创建目录 P1") == 2        # BUG：dry-run 下 ensure 被调两次
    assert "将镜像" not in text                    # push 的 dry-run print 没走到


# ======================================================================
# scripts/index_cognee.py —— 文档扫描与入图条目组装
# ======================================================================

@pytest.fixture()
def ic():
    return _load("index_cognee")


def _sample_tree(root: Path) -> Path:
    """造一个含方案/协议/报告/活动的项目树，返回 projects 根。"""
    proj = root / "projects" / "样例项目"
    for d in ("方案/sub", "协议", "报告", "活动/A1", "活动/A2"):
        (proj / d).mkdir(parents=True)
    (proj / "方案" / "sub" / "申报书.docx").write_text("x", encoding="utf-8")
    (proj / "方案" / "说明.txt").write_text("x", encoding="utf-8")
    (proj / "协议" / "协议.xlsx").write_text("x", encoding="utf-8")
    (proj / "报告" / "总结.doc").write_text("x", encoding="utf-8")
    (proj / "活动" / "A1" / "现场.pdf").write_text("x", encoding="utf-8")
    (proj / "活动" / "A1" / "记录.md").write_text("x", encoding="utf-8")
    (proj / "活动" / "A1" / "1.jpg").write_text("x", encoding="utf-8")
    (proj / "活动" / "A1" / "2.PNG").write_text("x", encoding="utf-8")
    (proj / "活动" / "A1" / "动图.gif").write_text("x", encoding="utf-8")
    (proj / "活动" / "A1" / "activity.yaml").write_text("code: HD-01\n", encoding="utf-8")
    (proj / "活动" / "A2" / "3.png").write_text("x", encoding="utf-8")
    return root / "projects"


def test_scan_project按类型与扩展名筛选(ic, tmp_path):
    proj = _sample_tree(tmp_path).parent / "样例项目" if False else None
    proj = _sample_tree(tmp_path) / "样例项目"
    items = ic.scan_project(proj)
    got = {(i.kind, i.rel) for i in items}
    assert ("方案", "方案/sub/申报书.docx") in got
    assert ("协议", "协议/协议.xlsx") in got
    assert ("报告", "报告/总结.doc") in got
    assert ("活动文档", "活动/A1/现场.pdf") in got
    assert ("活动图片清单", "活动/A1") in got
    # 非入图扩展名一律不收
    assert all("说明.txt" not in rel for _, rel in got)
    assert all("记录.md" not in rel for _, rel in got)
    assert all("动图.gif" not in rel for _, rel in got)


def test_scan_project扩展名大小写不敏感(ic, tmp_path):
    proj = _sample_tree(tmp_path) / "样例项目"
    items = ic.scan_project(proj)
    assert "活动/A1/2.PNG" in [i.images[1] for i in items if i.is_image_list][0] \
        or any("2.PNG" in i.images for i in items)


def test_scan_project照片清单与编号(ic, tmp_path):
    proj = _sample_tree(tmp_path) / "样例项目"
    lst = [i for i in ic.scan_project(proj) if i.is_image_list]
    a1 = [i for i in lst if i.activity == "A1"][0]
    assert a1.activity_code == "HD-01"
    assert a1.images == ["1.jpg", "2.PNG"]


def test_activity_code坏yaml返回空串(ic, tmp_path):
    d = tmp_path / "act"
    d.mkdir()
    (d / "activity.yaml").write_text("bad: [\n", encoding="utf-8")
    assert ic._activity_code(d) == ""
    (d / "activity.yaml").write_text("- a\n- b\n", encoding="utf-8")
    assert ic._activity_code(d) == ""
    assert ic._activity_code(tmp_path / "不存在") == ""


def test_scan项目过滤与不存在(ic, tmp_path):
    projects = _sample_tree(tmp_path)
    all_items = ic.scan(projects.parent)
    assert {i.project for i in all_items} == {"样例项目"}
    one = ic.scan(projects.parent, "样例项目")
    assert one and all(i.project == "样例项目" for i in one)
    with pytest.raises(SystemExit):
        ic.scan(projects.parent, "不存在的项目")


def test_upload_name扁平化(ic):
    """照片清单判定看 activity，不看 path 是否为空（2026-11-22 修）。"""
    doc = ic.Item(project="P", rel="方案/sub/a.docx", kind="方案")
    lst = ic.Item(project="P", rel="活动/A1", kind="活动图片清单", activity="A1",
                  path=None)
    assert doc.is_image_list is False, "普通文档（无 path、无 activity）不是照片清单"
    assert ic.upload_name(doc) == "方案__sub__a.docx.md"
    assert ic.upload_name(lst) == "A1__照片清单.md"
    real = ic.Item(project="P", rel="方案/sub/a.docx", kind="方案",
                   path=Path("方案/sub/a.docx"))
    assert ic.upload_name(real) == "方案__sub__a.docx.md"


def test_scan_project产出的条目带path(ic, tmp_path):
    """真实扫描路径下 path 一定非空，所以 upload_name 走扁平化分支。"""
    proj = _sample_tree(tmp_path) / "样例项目"
    docs = [i for i in ic.scan_project(proj) if not i.is_image_list]
    assert docs and all(d.path is not None for d in docs)
    assert ic.upload_name(docs[0]) == "方案__sub__申报书.docx.md"


def test_item_text带元数据头(ic):
    doc = ic.Item(project="P", rel="方案/a.docx", kind="方案")
    md = ic.metadata_block(doc)
    assert md.startswith("---\n") and md.endswith("---\n\n")
    assert "project: P" in md and "path: 方案/a.docx" in md
    lst = ic.Item(project="P", rel="r", kind="活动图片清单",
                  activity="A", activity_code="C-1", images=["x.jpg"])
    body = ic.item_text(lst)
    assert "activity_code: C-1" in body
    assert "- x.jpg" in body and "共 1 张" in body


# ======================================================================
# scripts/ingest_activities.py —— 归档计划推算与预览统计
# ======================================================================

@pytest.fixture()
def ia():
    return _load("ingest_activities")


def test_IMPORTS常量未被改动(ia):
    assert len(ia.IMPORTS) == 4
    assert all(len(row) == 4 for row in ia.IMPORTS)


def test_counts预览复制与跳过(ia, tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("1", encoding="utf-8")
    (src / "sub" / "b.txt").write_text("2", encoding="utf-8")
    dst = tmp_path / "dst"
    dst.mkdir()
    assert ia._counts(src, dst) == (2, 0)
    (dst / "a.txt").write_text("x", encoding="utf-8")
    assert ia._counts(src, dst) == (1, 1)


def test_scan_project扩展名大小写不敏感(ic, tmp_path):
    proj = _sample_tree(tmp_path) / "样例项目"
    lists = [i for i in ic.scan_project(proj) if i.is_image_list]
    # .PNG 大写同样进清单，说明后缀判断走了 lower()
    assert any("2.PNG" in lst.images for lst in lists)


def test_planned_target命中唯一指标(ia, monkeypatch, tmp_path):
    class FakeProjects:
        def load_project(self, name):
            return {"indicators": [
                {"name": "爱邻议事会第一期", "count": 1},
                {"name": "共治行动第一期", "count": 1},
            ]}

        def activity_dir(self, name, dirname):
            return tmp_path / name / "活动" / dirname

    monkeypatch.setattr(ia, "projects", FakeProjects())
    target, plan = ia._planned_target("花都社区保障资金项目", "爱邻议事会", "第一期")
    assert target is not None and plan is not None
    assert target.parent.name == "活动" and "爱邻议事会" in target.name
    assert plan["indicator_name"] == "爱邻议事会第一期"


def test_planned_target无唯一匹配返回空(ia, monkeypatch):
    class FakeProjects:
        def load_project(self, name):
            return {"indicators": [
                {"name": "爱邻议事会第一期", "count": 1},
                {"name": "爱邻议事会第二期", "count": 1},
            ]}

        def activity_dir(self, name, dirname):
            raise AssertionError("不该走到拼路径")

    monkeypatch.setattr(ia, "projects", FakeProjects())
    assert ia._planned_target("P", "爱邻议事会", "") == (None, None)   # 多匹配
    assert ia._planned_target("P", "不存在的关键词", "第一期") == (None, None)


def test_run_dry_run源缺失计失败(ia):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = ia.run(dry_run=True)
    assert rc == 1
    assert "源目录不存在" in buf.getvalue()


# ======================================================================
# 源码现状记录：发现的问题只如实断言当前行为，不改正源码
# ======================================================================

def test_feishu_bug_reports(sf, ia, monkeypatch, tmp_path):
    """以下断言的是**当前实现的真实行为**，用于暴露问题，不是期望契约。

    问题 1：sync_feishu._project_dirs 把越界项目名静默当成「不存在」，
            调用方拿不到任何信号（越权/拼错无法区分）。
    问题 2：sync_feishu.child_folders 在 lark() 失败（返回无 data）时
            静默返回 {}，ensure() 会把「读不到远端」误判成「目录不存在」，
            进而对已存在的目录重复建同名目录。
    问题 3：sync_feishu._stage_project 对不存在的源目录直接抛
            原生 FileNotFoundError，没有业务提示。
    问题 4：ingest_activities._planned_target 对不存在的项目直接抛
            原生 FileNotFoundError；run() 只处理「算不出匹配」，异常会穿透到 main()。
    问题 5：check_feishu_auth.main(--json) 里 "code" 是 API 错误码字段，
            不含进程退出码；机器消费方读到的 code 与 exit status 不一致。
    """
    root = _mkprojects(tmp_path)
    monkeypatch.setattr(sf, "ROOT", root)
    # 问题 1
    assert sf._project_dirs("../etc") == []
    assert sf._project_dirs("P1/../P1") == []
    # 问题 2
    monkeypatch.setattr(sf, "lark", lambda *a, **k: {"ok": False, "raw": "boom"})
    res = sf.ensure("已存在的目录")
    assert res["created_dir"] is False          # lark 无 token → 假成功
    assert res["folder_token"] is None
    # 问题 3
    monkeypatch.setattr(sf, "REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        sf._stage_project(tmp_path / "nope")
    # 问题 4
    with pytest.raises(FileNotFoundError):
        ia._planned_target("不存在的项目", "关键词", "第一期")
