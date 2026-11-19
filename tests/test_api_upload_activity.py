"""A2：活动目录内上传 API（带校验）。

全部直接调用端点函数；审计落库替成空操作，项目根用 tmp_path。
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import UploadFile

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402

ADMIN = {"id": 1, "username": "admin", "role": "admin",
         "projects": {"huimin": "write", "huadu": "write"}}
EDITOR = {"id": 2, "username": "editor", "role": "editor", "projects": {"huimin": "write"}}
VIEWER = {"id": 3, "username": "viewer", "role": "viewer", "projects": {"huimin": "readonly"}}


@pytest.fixture(autouse=True)
def _silent_audit(monkeypatch):
    """审计不在本工单范围，统一替成空操作。"""
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """惠民（立项月 6）/ 花都（立项月 5），各带最小 yaml 与 活动/。"""
    root = tmp_path / "projects"
    specs = (("惠民项目", "huimin", 6), ("花都项目", "huadu", 5))
    for name, short, month in specs:
        d = root / name
        (d / "活动").mkdir(parents=True)
        (d / f"{name}.yaml").write_text(
            yaml.safe_dump({
                "project": {
                    "short": short,
                    "code_rules": {"establish_month": month},
                    "period": {"start": "2026-07-01"},
                },
            }, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    monkeypatch.setattr(app._projects, "PROJECTS_ROOT", root)
    return root


def _activity(projects_root: Path, project: str, dirname: str,
              code: str = "CDYJR2026006-C-16110001",
              subdirs: tuple[str, ...] = ()) -> Path:
    """造活动目录：activity.yaml + 指定子目录。"""
    d = projects_root / project / "活动" / dirname
    d.mkdir(parents=True)
    (d / "activity.yaml").write_text(
        yaml.safe_dump({"code": code, "status": "imported"},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    for sub in subdirs:
        (d / sub).mkdir(parents=True)
    return d


def _file(name: str, data: bytes = b"test content") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _upload(project: str, dirname: str, file: UploadFile, sub: str = "",
            user=ADMIN, conn=None):
    return app.api_activity_upload(project, dirname, file=file, sub=sub,
                                   user=user, conn=conn)


# ---------------- 正常上传 ----------------

def test_upload_ok_writes_file_and_sha256(projects_root):
    act = _activity(projects_root, "惠民项目", "11-共治行动-第一期",
                    subdirs=("13-其他佐证",))
    data = b"test content\n"

    out = _upload("惠民项目", "11-共治行动-第一期", _file("t1.txt", data),
                  sub="13-其他佐证")

    assert out["path"] == "13-其他佐证/t1.txt"
    assert out["size"] == len(data)
    assert out["sha256"] == hashlib.sha256(data).hexdigest()
    assert "warning" not in out
    assert (act / "13-其他佐证" / "t1.txt").read_bytes() == data


def test_upload_to_activity_root(projects_root):
    act = _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    out = _upload("惠民项目", "01-走访调研-第一期", _file("备注.md", b"# hi"))
    assert out["path"] == "备注.md"
    assert (act / "备注.md").is_file()


def test_upload_nested_sub_ok(projects_root):
    act = _activity(projects_root, "惠民项目", "11-共治行动-第一期",
                    subdirs=("13-其他佐证/知识库规范命名照片",))
    out = _upload("惠民项目", "11-共治行动-第一期", _file("a.png", b"png"),
                  sub="13-其他佐证/知识库规范命名照片")
    assert out["path"] == "13-其他佐证/知识库规范命名照片/a.png"
    assert (act / out["path"]).is_file()


# ---------------- 路径安全 ----------------

def test_sub_traversal_400(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期",
              subdirs=("13-其他佐证",))
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("t.txt"), sub="../..")
    assert exc.value.status_code == 400


def test_sub_absolute_400(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("t.txt"), sub="/etc")
    assert exc.value.status_code == 400


def test_sub_backslash_400(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("t.txt"),
                sub="13-其他佐证\\..\\..")
    assert exc.value.status_code == 400


def test_activity_dir_missing_404(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "99-不存在-第一期", _file("t.txt"))
    assert exc.value.status_code == 404


def test_sub_dir_missing_400(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("t.txt"),
                sub="13-其他佐证")
    assert exc.value.status_code == 400


# ---------------- 文件名规范 ----------------

def test_bad_extension_400(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("t2.exe"))
    assert exc.value.status_code == 400


def test_hidden_filename_400(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file(".secret.txt"))
    assert exc.value.status_code == 400


def test_filename_too_long_400(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("长" * 118 + ".txt"))
    assert exc.value.status_code == 400


def test_filename_bad_char_400(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("a:b.txt"))
    assert exc.value.status_code == 400


def test_duplicate_name_409(projects_root):
    act = _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    _upload("惠民项目", "11-共治行动-第一期", _file("t.txt", b"first"))
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("t.txt", b"second"))
    assert exc.value.status_code == 409
    assert (act / "t.txt").read_bytes() == b"first"     # 不覆盖


# ---------------- 权限 ----------------

def test_viewer_403(projects_root):
    _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("t.txt"), user=VIEWER)
    assert exc.value.status_code == 403


def test_editor_foreign_project_403(projects_root):
    _activity(projects_root, "花都项目", "01-项目启动-第一期",
              code="CDYJR2026005-C-11050001")
    with pytest.raises(app.HTTPException) as exc:
        _upload("花都项目", "01-项目启动-第一期", _file("t.txt"), user=EDITOR)
    assert exc.value.status_code == 403


# ---------------- 大小 ----------------

def test_oversize_413_and_no_partial_file(projects_root, monkeypatch):
    act = _activity(projects_root, "惠民项目", "11-共治行动-第一期")
    # 把上限压到 0，1 字节就超限；避免构造 50MB 真文件
    monkeypatch.setitem(app.SETTINGS.upload, "max_mb", 0)
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "11-共治行动-第一期", _file("big.txt", b"x"))
    assert exc.value.status_code == 413
    assert not (act / "big.txt").exists()


# ---------------- 项目归属 / 编号前序 ----------------

def test_code_prefix_mismatch_400(projects_root):
    """惠民活动里出现花都编号 → 400。"""
    _activity(projects_root, "惠民项目", "01-走访调研-第一期",
              code="CDYJR2026005-C-11050001")
    with pytest.raises(app.HTTPException) as exc:
        _upload("惠民项目", "01-走访调研-第一期", _file("t.txt"))
    assert exc.value.status_code == 400
    assert "CDYJR2026006" in exc.value.detail


def test_code_prefix_accepts_existing_project_prefix(projects_root):
    """yaml 立项月 6、既有编号却是 008（真实数据）：同项目既有前缀应放行。"""
    _activity(projects_root, "惠民项目", "06-爱邻议事会-第三期",
              code="CDYJR2026008-C-1110001")
    act = _activity(projects_root, "惠民项目", "11-共治行动-第一期",
                    code="CDYJR2026008-C-1110002")

    out = _upload("惠民项目", "11-共治行动-第一期", _file("t.txt"))

    assert out["path"] == "t.txt"
    assert (act / "t.txt").is_file()


def test_numbering_prefix_warns_when_check_ok(projects_root):
    act = _activity(projects_root, "惠民项目", "01-走访调研-第一期")
    (act / "01-计划书.docx").write_bytes(b"plan")

    out = _upload("惠民项目", "01-走访调研-第一期", _file("01-计划书-补充.txt"))

    assert out["path"] == "01-计划书-补充.txt"
    assert "warning" in out
    assert "01-" in out["warning"]
    assert (act / "01-计划书-补充.txt").is_file()      # 不阻断
