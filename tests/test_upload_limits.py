"""上传资源上限：照片整批大小 / 像素上限 / zip 预算（审计 C-03）。

沿用 test_api_photos.py 的做法：直接调用端点函数，项目根走 tmp_path。
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
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
from api.state import _ARCHIVE_MAX_ENTRIES  # noqa: E402

PROJECT = "惠民社区保障资金项目"
ACT = "11-共治行动-第一期"
ADMIN = {"id": 1, "username": "admin", "role": "admin",
         "projects": {PROJECT: "write"}, "auth_type": "api_key"}


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


def _upload(files, **kw):
    return app.api_sw_photos_upload(
        project=kw.pop("project", PROJECT), activity=kw.pop("activity", ACT),
        shot_date=kw.pop("shot_date", "2026-11-10"),
        scene=kw.pop("scene", ""), note=kw.pop("note", ""),
        allow_duplicate=kw.pop("allow_duplicate", False),
        compress=kw.pop("compress", True), files=files,
        user=kw.pop("user", ADMIN), conn=FakeConn())


def _archive(data: bytes, name="现场照片.zip", **kw):
    return app.api_sw_photos_upload_archive(
        project=kw.pop("project", PROJECT), archive=_file(name, data),
        shot_date=kw.pop("shot_date", "2026-11-10"),
        allow_duplicate=kw.pop("allow_duplicate", False),
        user=kw.pop("user", ADMIN), conn=FakeConn())


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------- 照片多文件上传 ----------------

def test_photo_total_bytes_413(env, monkeypatch):
    """整批总量超限 → 413 PAYLOAD_TOO_LARGE（单张仍在上限内）。"""
    monkeypatch.setattr(app, "_PHOTO_MAX_TOTAL_BYTES", 500)
    with pytest.raises(app.ApiError) as exc:
        _upload([_file("a.jpg", _jpeg(size=(10, 10), color=(1, 1, 1))),
                 _file("b.jpg", _jpeg(size=(10, 10), color=(2, 2, 2)))])
    assert exc.value.status_code == 413
    assert exc.value.code == "PAYLOAD_TOO_LARGE"


def test_photo_pixels_400(env, monkeypatch):
    """像素超上限 → 400 IMAGE_TOO_LARGE（防解压炸弹）。"""
    monkeypatch.setattr(app, "_PHOTO_MAX_PIXELS", 100)
    with pytest.raises(app.ApiError) as exc:
        _upload([_file("a.jpg", _jpeg(size=(1200, 800)))])
    assert exc.value.status_code == 400
    assert exc.value.code == "IMAGE_TOO_LARGE"


# ---------------- zip 上传 ----------------

def test_archive_compressed_size_413(env, monkeypatch):
    """压缩包本身超 1GB 预算 → 413（用缩小后的常量验证逻辑）。"""
    monkeypatch.setattr(app, "_ARCHIVE_MAX_BYTES", 100)
    with pytest.raises(app.ApiError) as exc:
        _archive(_zip_bytes({f"{ACT}/a.jpg": _jpeg(size=(10, 10))}))
    assert exc.value.status_code == 413
    assert exc.value.code == "PAYLOAD_TOO_LARGE"


def test_archive_too_many_entries_400(env):
    entries = {f"{ACT}/{i}.jpg": b"" for i in range(_ARCHIVE_MAX_ENTRIES + 1)}
    with pytest.raises(app.ApiError) as exc:
        _archive(_zip_bytes(entries))
    assert exc.value.status_code == 400
    assert exc.value.code == "ARCHIVE_TOO_MANY_ENTRIES"


def test_archive_uncompressed_budget_400(env, monkeypatch):
    """解压后总量超预算 → 400 ARCHIVE_TOO_LARGE（预算挡在 read 之前）。"""
    monkeypatch.setattr(app, "_ARCHIVE_MAX_UNCOMPRESSED", 1000)
    with pytest.raises(app.ApiError) as exc:
        _archive(_zip_bytes({
            f"{ACT}/a.jpg": b"\0" * 600,
            f"{ACT}/b.jpg": b"\0" * 600,
        }))
    assert exc.value.status_code == 400
    assert exc.value.code == "ARCHIVE_TOO_LARGE"


def test_archive_suspicious_ratio_400(env):
    """单条高压缩比 + 体积不小 → 400 ARCHIVE_SUSPICIOUS。"""
    bomb = b"\0" * (11 * 1024 * 1024)     # 压缩后约 11KB，压缩比远超 200
    with pytest.raises(app.ApiError) as exc:
        _archive(_zip_bytes({f"{ACT}/bomb.jpg": bomb}))
    assert exc.value.status_code == 400
    assert exc.value.code == "ARCHIVE_SUSPICIOUS"


def test_archive_normal_package_passes(env):
    """正常小包（2-3 张真实图片）仍然通过。"""
    data = _zip_bytes({
        f"{ACT}/IMG_001.jpg": _jpeg(color=(1, 1, 1)),
        f"{ACT}/IMG_002.jpg": _jpeg(color=(2, 2, 2)),
        f"{ACT}/IMG_003.jpg": _jpeg(color=(3, 3, 3)),
    })
    out = _archive(data)
    assert out["ok"] is True
    assert out["matched"] == {ACT: 3}
    assert out["total_saved"] == 3
