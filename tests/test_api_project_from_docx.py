"""从项目书建项目（POST /api/projects/from-docx）+ 权限审计。

沿用 test_api_web.py 的做法：直接调用端点函数，不依赖 httpx / TestClient；
项目根用 tmp_path，审计落库用空操作，上传用真 docx（python-docx 现造）。

重点覆盖用户点名的三条硬规矩：
  1. 必须上传项目书，不传 / 传错类型 → 400
  2. 解析不到就留空 + warnings，绝不编造（关键字段缺失 → 400 并列出缺哪个）
  3. short 是目录名 / 数据集名，必须校验唯一性与合法性（重名 409、非法 400）
"""
from __future__ import annotations

import io
import os
import sys
import types
from pathlib import Path

import pytest
import yaml
from docx import Document

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402
from engine import project_parse, projects as _projects  # noqa: E402

ADMIN = {"id": 1, "username": "admin", "role": "admin", "projects": {}}
EDITOR = {"id": 2, "username": "editor", "role": "editor", "projects": {}}


class _Req:
    """端点只用到 request.client.host。"""

    client = types.SimpleNamespace(host="127.0.0.1")
    headers: dict = {}


class _Up:
    """最小 UploadFile 替身：端点只用 .filename 与 .file.read()。"""

    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.file = io.BytesIO(data)


@pytest.fixture(autouse=True)
def _silent_audit(monkeypatch):
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)
    monkeypatch.setattr("api.shared.audit.audit", lambda *a, **k: None)


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """一个既有项目；PROJECTS_ROOT 指向 tmp。"""
    root = tmp_path / "projects"
    proj = root / "惠民社区保障资金项目"
    for sub in ("方案", "协议", "报告", "活动", "其他"):
        (proj / sub).mkdir(parents=True)
    (proj / "惠民社区保障资金项目.yaml").write_text(
        yaml.safe_dump({"project": {"short": "惠民社区保障资金项目", "code_rules": {}}},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    monkeypatch.setattr(_projects, "PROJECTS_ROOT", root)
    return root


def _docx_bytes(rows: list[tuple[str, str]], *, table: bool = True) -> bytes:
    """造一份「两列横表」式项目书：| 标签 | 值 |。"""
    doc = Document()
    if table:
        t = doc.add_table(rows=0, cols=2)
        for label, value in rows:
            cells = t.add_row().cells
            cells[0].text = label
            cells[1].text = value
    else:
        for label, value in rows:
            doc.add_paragraph(f"{label}：{value}")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


FULL_ROWS = [
    ("项目名称", "“测试微格·精服善治”2026年测试社区项目"),
    ("立项单位", "四川省成都市温江区公平街道测试社区居民委员会"),
    ("申报单位", "成都市某某社会工作服务中心"),
    ("项目负责人", "张三 13800138000"),
    ("项目周期", "2026年7月-2027年2月"),
    ("申请类别", "□社区总体营造    ☑小区、院落（网格）营建"),
]


def _indicator_docx(extra_rows: list[tuple[str, str]] | None = None) -> bytes:
    """带量化指标表的项目书（指标表 + 后面紧跟一张预算表，防串表）。"""
    doc = Document()
    t = doc.add_table(rows=0, cols=2)
    for label, value in (extra_rows or FULL_ROWS):
        cells = t.add_row().cells
        cells[0].text = label
        cells[1].text = value
    # 量化指标表
    ind = doc.add_table(rows=1, cols=4)
    for k, head in enumerate(["策略", "服务类别", "指标明细", "预期量化指标"]):
        ind.rows[0].cells[k].text = head
    for strategy, service, name, target in (
            ("策略一", "赋能提质", "主题课程培训", "3期"),
            ("策略三", "闭环管控", "安全隐患专项排查", "6次")):
        cells = ind.add_row().cells
        cells[0].text = strategy
        cells[1].text = service
        cells[2].text = name
        cells[3].text = target
    # 紧跟一张「活动 / 费用类别 / …」预算表：不能被当成指标表继续读
    budget = doc.add_table(rows=1, cols=3)
    for k, head in enumerate(["活动", "费用类别", "金额（元）"]):
        budget.rows[0].cells[k].text = head
    cells = budget.add_row().cells
    cells[0].text = "项目启动"
    cells[1].text = "广告费用"
    cells[2].text = "300"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------- A. 必须上传项目书 ----------------

def test_from_docx_requires_file(projects_root):
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(request=_Req(), file=None, user=ADMIN, conn=None)
    assert exc.value.status_code == 400
    assert "必须上传项目书" in str(exc.value.detail)


def test_from_docx_rejects_unsupported_type(projects_root):
    up = _Up("项目书.txt", "项目名称：x".encode())
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(request=_Req(), file=up, user=ADMIN, conn=None)
    assert exc.value.status_code == 400
    assert "只支持" in str(exc.value.detail)


def test_from_docx_rejects_empty_file(projects_root):
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(request=_Req(), file=_Up("项目书.docx", b""), user=ADMIN, conn=None)
    assert exc.value.status_code == 400


def test_from_docx_requires_admin(projects_root):
    up = _Up("项目书.docx", _docx_bytes(FULL_ROWS))
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(request=_Req(), file=up, user=EDITOR, conn=None)
    assert exc.value.status_code == 403


# ---------------- B. 正常建项目 ----------------

def test_from_docx_creates_dirs_yaml_and_moves_doc(projects_root):
    data = _docx_bytes(FULL_ROWS)
    out = app.api_create_project_from_docx(
        request=_Req(), file=_Up("申报书.docx", data), project_name="测试项目", short="测试项目简称",
        user=ADMIN, conn=None)

    assert out["ok"] is True
    assert out["project"] == "测试项目"
    assert out["short"] == "测试项目简称"
    target = projects_root / "测试项目"
    for sub in ("方案", "协议", "报告", "活动", "其他"):
        assert (target / sub).is_dir()
    # 项目书原件进 方案/
    assert (target / "方案" / "申报书.docx").read_bytes() == data
    # yaml 文件名必须与目录名一致（list_projects / project_meta 都按它找）
    yaml_path = target / "测试项目.yaml"
    assert yaml_path.is_file()
    saved = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert saved["project"]["short"] == "测试项目简称"
    assert saved["project"]["name"] == "“测试微格·精服善治”2026年测试社区项目"
    assert saved["parties"]["funder"]["name"].endswith("测试社区居民委员会")
    assert saved["staff"]["leader"] == {"name": "张三", "phone": "13800138000"}
    # 项目书被认出来不等于编数据：没解析到的字段必须为空
    assert saved["project"]["code_rules"]["act_types"] == {}
    assert saved["indicators"] == []
    assert saved["personnel"] == []


def test_from_docx_parses_period_and_establish_month(projects_root):
    out = app.api_create_project_from_docx(
        request=_Req(), file=_Up("a.docx", _docx_bytes(FULL_ROWS)), project_name="周期项目", short="周期项目", user=ADMIN, conn=None)
    p = out["parsed"]
    assert p["period_start"] == "2026-07-01"
    assert p["period_end"] == "2027-02-28"
    saved = yaml.safe_load(Path(out["yaml"]).read_text(encoding="utf-8"))
    assert saved["project"]["code_rules"]["establish_month"] == 7


def test_from_docx_does_not_invent_missing_fields(projects_root):
    """项目书里没周期 / 没指标 → 留空 + warnings，绝不填默认值。"""
    rows = [r for r in FULL_ROWS if r[0] != "项目周期"]
    out = app.api_create_project_from_docx(
        request=_Req(), file=_Up("a.docx", _docx_bytes(rows)), project_name="缺项项目", short="缺项项目", user=ADMIN, conn=None)
    saved = yaml.safe_load(Path(out["yaml"]).read_text(encoding="utf-8"))
    assert saved["project"]["period"] == {"start": "", "end": "", "months": 0}
    assert saved["project"]["code_rules"]["establish_month"] is None
    assert any("项目周期" in w for w in out["warnings"])
    assert any("establish_month" in w for w in out["warnings"])


def test_from_docx_is_idempotent_409(projects_root):
    data = _docx_bytes(FULL_ROWS)
    app.api_create_project_from_docx(
        request=_Req(), file=_Up("a.docx", data), project_name="重复项目", short="重复项目",
        user=ADMIN, conn=None)
    # 同简称 → 409（数据集名冲突）
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(
        request=_Req(), file=_Up("b.docx", data), project_name="另一个目录", short="重复项目",
        user=ADMIN, conn=None)
    assert exc.value.status_code == 409


# ---------------- C. 关键字段缺失 → 400 + 列出缺哪个 ----------------

def test_from_docx_missing_project_name_lists_field(projects_root):
    rows = [r for r in FULL_ROWS if r[0] != "项目名称"]
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(
            request=_Req(), file=_Up("a.docx", _docx_bytes(rows)), project_name="", short="", user=ADMIN, conn=None)
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert "project_name" in detail["missing"]


def test_from_docx_missing_name_can_be_supplied_by_query(projects_root):
    rows = [r for r in FULL_ROWS if r[0] != "项目名称"]
    out = app.api_create_project_from_docx(
        request=_Req(), file=_Up("a.docx", _docx_bytes(rows)), project_name="手工补的名字", short="手工补简称", user=ADMIN, conn=None)
    assert out["ok"] is True
    assert out["project"] == "手工补的名字"


def test_from_docx_missing_funder_and_executer_lists_both(projects_root):
    rows = [r for r in FULL_ROWS if r[0] not in ("立项单位", "申报单位")]
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(
            request=_Req(), file=_Up("a.docx", _docx_bytes(rows)), project_name="", short="", user=ADMIN, conn=None)
    detail = exc.value.detail
    assert exc.value.status_code == 400
    assert set(detail["missing"]) == {"funder", "executer"}


# ---------------- D. short 校验（唯一性 + 合法性） ----------------

@pytest.mark.parametrize("bad_short", ["../逃逸", "a/b", "a\\b", "x" * 41, ".隐藏", ".."])
def test_from_docx_rejects_illegal_short(projects_root, bad_short):
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(
            request=_Req(), file=_Up("a.docx", _docx_bytes(FULL_ROWS)), project_name="目录名", short=bad_short, user=ADMIN, conn=None)
    assert exc.value.status_code == 400
    # 目录名不能因为简称非法而留下半截
    assert not (projects_root / "目录名").exists()


def test_from_docx_short_conflict_409(projects_root):
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(
            request=_Req(), file=_Up("a.docx", _docx_bytes(FULL_ROWS)), project_name="新目录名",
            short="惠民社区保障资金项目", user=ADMIN, conn=None)
    assert exc.value.status_code == 409
    assert not (projects_root / "新目录名").exists()


def test_from_docx_short_cannot_equal_existing_dirname(projects_root):
    """简称与任一现有项目目录名相同也算冲突（目录名就是 short 的一半语义）。"""
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(
            request=_Req(), file=_Up("a.docx", _docx_bytes(FULL_ROWS)), project_name="新目录名2",
            short="惠民社区保障资金项目", user=ADMIN, conn=None)
    assert exc.value.status_code == 409


def test_short_max_len_boundary(projects_root):
    ok = _projects.validate_short("短" * _projects.SHORT_MAX_LEN)
    assert ok == "短" * _projects.SHORT_MAX_LEN
    with pytest.raises(ValueError):
        _projects.validate_short("短" * (_projects.SHORT_MAX_LEN + 1))
    with pytest.raises(ValueError):
        _projects.validate_short("   ")


# ---------------- E. 指标表解析（不串表、不编期数） ----------------

def test_indicator_table_stops_at_budget_table():
    """指标表后面紧跟预算表时，预算行不能被读成指标。"""
    md = (
        "| 策略 | 服务类别 | 指标明细 | 预期量化指标 |\n"
        "| --- | --- | --- | --- |\n"
        "| 策略一 | 赋能提质 | 主题课程培训 | 3期 |\n"
        "| 策略三 | 闭环管控 | 安全隐患专项排查 | 6次 |\n"
        "| 活动 | 费用类别 | 金额（元） |\n"
        "| --- | --- | --- |\n"
        "| 项目启动 | 广告费用 | 300 |\n"
        "| 项目启动 | 物料费 | 400 |\n"
    )
    rows = project_parse._indicator_table_rows(md)
    assert [r[1] for r in rows] == ["主题课程培训", "安全隐患专项排查"]


def test_indicator_does_not_infer_count_from_target():
    """target 是服务量指标，不是期数 —— 绝不写 count。"""
    md = ("| 策略 | 指标明细 | 预期量化指标 |\n"
          "| --- | --- | --- |\n"
          "| 策略二 | 一户一档 | 120人次 |\n")
    indicators, rules, warns = project_parse._extract_indicators(md)
    assert indicators == [{"strategy": "策略二", "name": "一户一档", "target": "120人次"}]
    assert all("count" not in ind for ind in indicators)
    assert rules["act_types"] == {"一户一档": 1}
    assert warns == []


def test_no_indicator_table_yields_empty_and_warning():
    indicators, rules, warns = project_parse._extract_indicators("项目名称：x")
    assert indicators == []
    assert rules == {}
    assert any("指标" in w for w in warns)


def test_act_types_capped_at_nine():
    md = "| 策略 | 指标明细 | 预期量化指标 |\n| --- | --- | --- |\n"
    md += "".join(f"| 策略一 | 活动{i} | 1期 |\n" for i in range(12))
    indicators, rules, warns = project_parse._extract_indicators(md)
    assert len(indicators) == 12
    assert len(rules["act_types"]) == 9
    assert all(1 <= v <= 9 for v in rules["act_types"].values())
    assert any("超过 9" in w for w in warns)


def test_from_docx_full_flow_with_indicators(projects_root):
    out = app.api_create_project_from_docx(
        request=_Req(), file=_Up("a.docx", _indicator_docx()), project_name="指标项目", short="指标项目", user=ADMIN, conn=None)
    saved = yaml.safe_load(Path(out["yaml"]).read_text(encoding="utf-8"))
    names = [i["name"] for i in saved["indicators"]]
    assert names == ["主题课程培训", "安全隐患专项排查"]
    assert saved["project"]["code_rules"]["act_types"] == {
        "主题课程培训": 1, "安全隐患专项排查": 2}

    # 新项目能被后续接口立刻用起来（列表 / 拆分 / 补编号）
    from api.routes.activities import api_split_activities
    from api.routes.materials import api_set_activity_code
    listed = [p["name"] for p in _projects.list_projects()]
    assert "指标项目" in listed
    plan = api_split_activities("指标项目", {"dry_run": True}, ADMIN)
    assert plan["would_create"] == ["01-主题课程培训-第一期", "02-安全隐患专项排查-第一期"]
    api_split_activities("指标项目", {"dry_run": False}, ADMIN)
    out2 = api_set_activity_code("指标项目", "01-主题课程培训-第一期",
                                 {"code": "CDYJR2026007-C-17260001"}, ADMIN)
    assert out2["ok"] is True


def test_from_docx_dry_run_writes_nothing(projects_root):
    """dry_run=true：解析 + 全部校验照做，但磁盘一个字节都不动（Web 预览用）。"""
    out = app.api_create_project_from_docx(
        request=_Req(), file=_Up("a.docx", _docx_bytes(FULL_ROWS)),
        project_name="预演项目", short="预演项目", preview_only=True,
        user=ADMIN, conn=None)
    assert out["ok"] is True and out["dry_run"] is True
    assert not (projects_root / "预演项目").exists()
    # 预览里的 yaml 与真实创建写出来的必须一致（同一个序列化函数）
    assert "预演项目" in out["yaml_preview"]
    assert "confirm" not in out["yaml_preview"] and "预演结果" in out["hint"]

    real = app.api_create_project_from_docx(
        request=_Req(), file=_Up("a.docx", _docx_bytes(FULL_ROWS)),
        project_name="预演项目", short="预演项目", user=ADMIN, conn=None)
    assert Path(real["yaml"]).read_text(encoding="utf-8") == out["yaml_preview"]


def test_from_docx_dry_run_still_validates_short(projects_root):
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project_from_docx(
            request=_Req(), file=_Up("a.docx", _docx_bytes(FULL_ROWS)),
            project_name="预演", short="惠民社区保障资金项目", preview_only=True,
            user=ADMIN, conn=None)
    assert exc.value.status_code == 409


# ---------------- F. 权限审计 ----------------

class _NoRowConn:
    """没有任何 users / user_projects 行的连接。"""

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): self.last = a
        def fetchall(self): return []

    def cursor(self): return self._Cur()


def _audit_conn(users, grants):
    class _Cur:
        def __init__(self):
            self.rows = []

        def __enter__(self): return self

        def __exit__(self, *a): return False

        def execute(self, sql, params=None):
            q = " ".join(str(sql).split()).lower()
            if q.startswith("select id, username, real_name, role, enabled from users"):
                self.rows = users
            elif q.startswith("select user_id, project, access from user_projects"):
                self.rows = grants
            else:
                self.rows = []

        def fetchall(self): return self.rows

    class _Conn:
        def cursor(self): return _Cur()

    return _Conn()


def test_permissions_audit_lists_users_without_access(projects_root):
    out = app.api_create_project_from_docx(
        request=_Req(), file=_Up("a.docx", _docx_bytes(FULL_ROWS)), project_name="审计项目", short="审计项目", user=ADMIN, conn=None)
    assert out["permissions"]["audit_url"].endswith("/permissions-audit")

    conn = _audit_conn(
        users=[(1, "admin", "管理员", "admin", True),
               (2, "ed", "编辑", "editor", True),
               (3, "vv", "查看", "viewer", False)],
        grants=[(2, "惠民社区保障资金项目", "readonly")])
    audit = app.api_project_permissions_audit("审计项目", ADMIN, conn)
    assert audit["project"] == "审计项目"
    assert {u["username"] for u in audit["users_without_access"]} == {"ed", "vv"}
    # admin 不需要 user_projects 记录，但必须在「有权限」里
    assert [u["username"] for u in audit["users_with_access"]] == ["admin"]
    assert "用户管理" in audit["hint"]


def test_permissions_audit_recognizes_short_key(projects_root):
    """user_projects 里存 short（而非目录名）也要认。"""
    out = app.api_create_project_from_docx(
        request=_Req(), file=_Up("a.docx", _docx_bytes(FULL_ROWS)), project_name="短键项目", short="短键简称", user=ADMIN, conn=None)
    conn = _audit_conn(
        users=[(1, "admin", "管理员", "admin", True), (5, "ww", "小王", "editor", True)],
        grants=[(5, "短键简称", "write")])
    audit = app.api_project_permissions_audit("短键项目", ADMIN, conn)
    assert audit["users_without_access"] == []
    assert audit["users_with_access"][1]["access"] == "write"


# ---------------- G. 老端点：POST /api/projects 也补上 short 校验 ----------------

def test_create_project_rejects_duplicate_short(projects_root):
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project({"name": "新目录", "short": "惠民社区保障资金项目"},
                               _Req(), ADMIN, None)
    assert exc.value.status_code == 409


def test_create_project_rejects_illegal_short(projects_root):
    with pytest.raises(app.HTTPException) as exc:
        app.api_create_project({"name": "新目录", "short": "../evo"}, _Req(), ADMIN, None)
    assert exc.value.status_code == 400
