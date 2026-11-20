"""G1：活动照片上传 API（预检 / 多文件 / zip）。

沿用仓库习惯：直接调用端点函数，FakeConn 只做审计占位，项目根走 tmp_path。
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from fastapi import UploadFile
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
os.environ.setdefault("SOCIAL_WORK_ROOT", str(REPO))
os.environ.setdefault(
    "SOCIALWORK_DB_DSN", "host=127.0.0.1 port=5432 dbname=test user=test password=x")

import app  # noqa: E402
from engine import photos as P  # noqa: E402

PROJECT = "惠民社区保障资金项目"
ACT = "11-共治行动-第一期"
ADMIN = {"id": 1, "username": "admin", "role": "admin",
         "projects": {PROJECT: "write"}, "auth_type": "api_key"}
VIEWER = {"id": 3, "username": "viewer", "role": "viewer",
          "projects": {PROJECT: "readonly"}, "auth_type": "api_key"}


class FakeConn:
    def commit(self):
        pass


@pytest.fixture(autouse=True)
def _silent_audit(monkeypatch):
    monkeypatch.setattr(app, "audit", lambda *a, **k: None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    project = root / PROJECT
    (project / "活动" / ACT).mkdir(parents=True)
    (project / f"{PROJECT}.yaml").write_text(
        yaml.safe_dump({"project": {"short": "huimin", "code_rules": {}}},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    monkeypatch.setattr(app._projects, "PROJECTS_ROOT", root)
    monkeypatch.setattr(app, "ROOT", tmp_path)
    return root


def _jpeg(size=(1200, 800), color=(20, 120, 200)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _file(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _upload(files, *, project=PROJECT, activity=ACT, shot_date="2026-11-10",
            scene="", note="", allow_duplicate=False, compress=True,
            user=ADMIN):
    return app.api_sw_photos_upload(
        project=project, activity=activity, shot_date=shot_date, scene=scene,
        note=note, allow_duplicate=allow_duplicate, compress=compress,
        files=files, user=user, conn=FakeConn())


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------- 预检 ----------------

def test_precheck_reports_new_and_duplicated(env):
    data = _jpeg(color=(1, 2, 3))
    _upload([_file("IMG_001.jpg", data)])
    digest = hashlib.sha256(data).hexdigest()
    other = hashlib.sha256(b"other").hexdigest()

    out = app.api_sw_photos_precheck(
        {"project": PROJECT, "activity": ACT, "hashes": [digest, other]},
        user=ADMIN)
    assert out["new"] == 1
    assert out["duplicated"] == [digest]
    assert out["duplicate_batch"] is None


def test_precheck_unknown_activity_404(env):
    with pytest.raises(app.ApiError) as exc:
        app.api_sw_photos_precheck(
            {"project": PROJECT, "activity": "99-不存在", "hashes": []},
            user=ADMIN)
    assert exc.value.status_code == 404
    assert exc.value.code == "ACTIVITY_NOT_FOUND"


# ---------------- 多文件上传 ----------------

def test_upload_saves_and_returns_protocol_shape(env):
    out = _upload([_file("IMG_001.jpg", _jpeg(color=(1, 1, 1))),
                   _file("IMG_002.jpg", _jpeg(color=(2, 2, 2)))],
                  scene="主会场")
    assert out["ok"] is True and out["saved"] == 2
    assert out["received"] == 2 and out["compressed"] == 2
    assert out["activity"] == ACT and out["batch_id"]
    assert out["paths"] == ["11-活动照片资料/20261110_001.jpg",
                            "11-活动照片资料/20261110_002.jpg"]
    assert out["total_bytes"] > 0 and out["skipped"] == []
    assert (env / PROJECT / "活动" / ACT / P.META_NAME).is_file()


def test_upload_readonly_project_403(env):
    with pytest.raises(app.ApiError) as exc:
        _upload([_file("a.jpg", _jpeg())], user=VIEWER)
    assert exc.value.status_code == 403


def test_upload_more_than_200_files_400(env):
    files = [_file(f"a{i}.jpg", b"x") for i in range(201)]
    with pytest.raises(app.ApiError) as exc:
        _upload(files)
    assert exc.value.status_code == 400
    assert exc.value.code == "TOO_MANY_FILES"


def test_upload_file_too_large_413(env):
    big = b"\0" * (50 * 1024 * 1024 + 1)
    with pytest.raises(app.ApiError) as exc:
        _upload([_file("big.jpg", big)])
    assert exc.value.status_code == 413
    assert exc.value.code == "FILE_TOO_LARGE"


def test_upload_non_image_400(env):
    with pytest.raises(app.ApiError) as exc:
        _upload([_file("note.txt", b"hello")])
    assert exc.value.status_code == 400
    assert exc.value.code == "NOT_AN_IMAGE"


@pytest.mark.parametrize("bad", ["", "2026/11/10", "20261110"])
def test_upload_bad_date_400(env, bad):
    with pytest.raises(app.ApiError) as exc:
        _upload([_file("a.jpg", _jpeg())], shot_date=bad)
    assert exc.value.status_code == 400
    assert exc.value.code == "VALIDATION"


def test_upload_future_date_400(env):
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    with pytest.raises(app.ApiError) as exc:
        _upload([_file("a.jpg", _jpeg())], shot_date=future)
    assert exc.value.status_code == 400
    assert exc.value.code == "VALIDATION"


def test_upload_same_batch_twice_409_duplicate_batch(env):
    _upload([_file("a.jpg", _jpeg(color=(1, 1, 1)))], scene="主会场")
    with pytest.raises(app.ApiError) as exc:
        _upload([_file("b.jpg", _jpeg(color=(2, 2, 2)))], scene="主会场")
    assert exc.value.status_code == 409
    assert exc.value.code == "DUPLICATE_BATCH"


def test_upload_allow_duplicate_bypasses_409(env):
    _upload([_file("a.jpg", _jpeg(color=(1, 1, 1)))], scene="主会场")
    out = _upload([_file("b.jpg", _jpeg(color=(2, 2, 2)))], scene="主会场",
                  allow_duplicate=True)
    assert out["saved"] == 1


# ---------------- zip 上传 ----------------

def _archive(data: bytes, name="现场照片.zip", **kw):
    return app.api_sw_photos_upload_archive(
        project=kw.pop("project", PROJECT), archive=_file(name, data),
        shot_date=kw.pop("shot_date", "2026-11-10"),
        allow_duplicate=kw.pop("allow_duplicate", False),
        user=kw.pop("user", ADMIN), conn=FakeConn())


def test_archive_matches_directory_and_reports_unmatched(env):
    data = _zip_bytes({
        f"{ACT}/IMG_001.jpg": _jpeg(color=(1, 1, 1)),
        f"{ACT}/IMG_002.jpg": _jpeg(color=(2, 2, 2)),
        "其他照片/IMG_003.jpg": _jpeg(color=(3, 3, 3)),
    })
    out = _archive(data)
    assert out["matched"] == {ACT: 2}
    assert out["unmatched"] == {"其他照片": 1}
    assert out["total_saved"] == 2


def test_archive_prefix_matches_activity(env):
    data = _zip_bytes({"11/IMG_001.jpg": _jpeg()})
    out = _archive(data)
    assert out["matched"] == {ACT: 1}


def test_archive_rejects_non_zip(env):
    with pytest.raises(app.ApiError) as exc:
        _archive(b"whatever", name="照片.rar")
    assert exc.value.status_code == 400
    assert exc.value.code == "ARCHIVE_UNSUPPORTED"


def test_archive_empty_400(env):
    data = _zip_bytes({"说明.txt": b"hello"})
    with pytest.raises(app.ApiError) as exc:
        _archive(data)
    assert exc.value.code == "ARCHIVE_EMPTY"


def test_archive_no_matched_activity_400(env):
    data = _zip_bytes({"其他照片/a.jpg": _jpeg()})
    with pytest.raises(app.ApiError) as exc:
        _archive(data)
    assert exc.value.code == "NO_MATCHED_ACTIVITY"


def test_archive_skips_macosx_and_dotfiles(env):
    data = _zip_bytes({
        f"{ACT}/a.jpg": _jpeg(),
        f"__MACOSX/{ACT}/._a.jpg": b"junk",
        f"{ACT}/.DS_Store": b"junk",
    })
    out = _archive(data)
    assert out["matched"] == {ACT: 1}
