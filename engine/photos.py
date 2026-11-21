#!/usr/bin/env python3
"""活动照片批量上传：压缩、归档、防重复、索引拼图。

设计前提（见 docs/照片上传协议.md）：
- 微信/飞书下载的图 EXIF 已被剥离，文件名不可控，无法从图片内容判断归属
- 归属由调用方在上传时指定（项目 + 活动 + 拍摄日期 + 场景）
- 服务端只做：重命名、压缩、去重、生成索引，不做识别

本模块是纯逻辑，不依赖 FastAPI；图片只走 Pillow 解码/编码，
不读 EXIF、不做 OCR、不调 LLM。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import math
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

PHOTO_DIRS = {"10-物资照片资料", "11-活动照片资料", "12-发票"}
# 默认照片子目录：上传端点固定落到这里
PHOTO_SUBDIR = "11-活动照片资料"
# 压缩目标：优先省空间（用户 2026-11-21 定）。
# 微信导出的图普遍是「大尺寸 + 低质量」（4096×3072 才 569KB），
# 用 q=95 重编码反而膨胀到 216%；实测 1920/q80 能压到 57%，
# 屏幕上看与原件无明显差别。
# compress_image 仍有「压完变大就保留原件」的兜底。
MAX_EDGE = 1920          # 原件压缩后的最大边
QUALITY = 80
PICK_COUNT = 8           # 精选张数
PICK_EDGE = 1600
CONTACT_SHEET_COLS = 3
CONTACT_SHEET_CELL = 400
CONTACT_SHEET_NAME = "_照片索引.jpg"

META_NAME = "_photos.json"
# 元数据时间戳统一按东八区写（与协议示例 2026-11-20T14:30:25+08:00 一致）
TZ_CN = timezone(timedelta(hours=8))
# 可接受的图片扩展名（与上传端点的白名单保持一致）
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
# 序号文件名：20261110_001.jpg
_SEQ_RE = re.compile(r"^(\d{8})_(\d{3,})\.jpe?g$", re.IGNORECASE)


# ---------------- 图片压缩 ----------------

def _to_rgb(img: Image.Image) -> Image.Image:
    """统一转 RGB；带 alpha 的先贴到白底，避免透明区变黑。"""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (
        img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def compress_image(src: bytes | Path, max_edge: int = MAX_EDGE,
                   quality: int = QUALITY) -> tuple[bytes, int, int]:
    """压缩到最大边 max_edge，返回 (jpeg_bytes, width, height)。

    RGB 转换（png 带 alpha 要先贴白底），失败抛 ValueError。
    只缩不放：原图小于 max_edge 时保持原尺寸，避免无谓放大。
    """
    if isinstance(src, Path):
        data = src.read_bytes()
    elif isinstance(src, (bytes, bytearray)):
        data = bytes(src)
    else:
        raise ValueError("图片输入必须是 bytes 或 Path")
    if not data:
        raise ValueError("图片内容为空")

    try:
        with Image.open(BytesIO(data)) as img:
            img.load()                      # 尽早暴露截断/损坏
            if img.width < 1 or img.height < 1:
                raise ValueError("图片尺寸非法")
            rgb = _to_rgb(img)
    except ValueError:
        raise
    except Exception as exc:                # UnidentifiedImageError / OSError 等
        raise ValueError(f"无法识别图片：{exc}") from exc

    if max(rgb.size) > max_edge:
        rgb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

    out = BytesIO()
    try:
        rgb.save(out, format="JPEG", quality=int(quality), optimize=True)
    except Exception as exc:
        raise ValueError(f"图片编码失败：{exc}") from exc
    encoded = out.getvalue()

    # 压缩后反而变大时保留原图：微信导出的图常是「大尺寸 + 低质量」，
    # 例如 4096×3072 只有 569KB，用 q=95 重编码会膨胀到 1228KB（216%）。
    # 这种情况压缩没有收益，原图质量也更好。
    if len(encoded) >= len(data):
        with Image.open(BytesIO(data)) as orig:
            return data, orig.width, orig.height
    return encoded, rgb.width, rgb.height


def sha256_of(data: bytes) -> str:
    """图片内容哈希：去重的主依据。"""
    return hashlib.sha256(bytes(data)).hexdigest()


# ---------------- 命名与序号 ----------------

def _compact_date(shot_date: str) -> str:
    """把 2026-11-10 / 20261110 统一成 20261110；非法格式抛 ValueError。"""
    raw = str(shot_date or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        return raw
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        raise ValueError(f"拍摄日期格式应为 YYYY-MM-DD：{raw or '（空）'}")


def target_name(shot_date: str, seq: int) -> str:
    """20261110_001.jpg"""
    return f"{_compact_date(shot_date)}_{int(seq):03d}.jpg"


def _photo_dir_of(act_dir: Path) -> Path:
    """活动根目录 → 照片子目录；传进来的本身就是照片目录时原样返回。"""
    base = Path(act_dir)
    candidate = base / PHOTO_SUBDIR
    if candidate.is_dir():
        return candidate
    # 兼容直接传照片目录的调用方（例如存量压缩脚本）
    return base


def next_seq(act_dir: Path, shot_date: str) -> int:
    """扫描活动照片目录里 YYYYMMDD_NNN.jpg，返回下一个序号（从 1 开始）。"""
    compact = _compact_date(shot_date)
    photo_dir = _photo_dir_of(act_dir)
    top = 0
    if photo_dir.is_dir():
        for item in photo_dir.iterdir():
            if not item.is_file():
                continue
            m = _SEQ_RE.match(item.name)
            if m and m.group(1) == compact:
                top = max(top, int(m.group(2)))
    return top + 1


# ---------------- 元数据 ----------------

def _empty_meta() -> dict:
    return {"batches": [], "photos": [], "hashes": {}}


@contextlib.contextmanager
def activity_lock(act_dir: Path):
    """活动级跨进程互斥：包住「读 meta → 分配序号 → 写图 → 写 meta」。

    单靠 save_meta 的原子 replace 只能保证文件不残缺，保证不了
    「读—算—写」这组操作互斥：两个请求会拿到同一个序号、覆盖同一张图、
    最后 meta 只记住其中一个（审计 I-01）。用 fcntl 排他锁覆盖整个临界区，
    多 worker / 多进程都有效。
    """
    base = Path(act_dir)
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / f".{META_NAME}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_meta(act_dir: Path) -> dict:
    """读 _photos.json，不存在返回 {"batches": [], "photos": [], "hashes": {}}。"""
    path = Path(act_dir) / META_NAME
    if not path.is_file():
        return _empty_meta()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{META_NAME} 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{META_NAME} 根节点不是对象")
    meta = _empty_meta()
    for key in ("batches", "photos"):
        value = data.get(key)
        if isinstance(value, list):
            meta[key] = value
    hashes = data.get("hashes")
    if isinstance(hashes, dict):
        meta["hashes"] = {str(k): str(v) for k, v in hashes.items()}
    return meta


def save_meta(act_dir: Path, meta: dict) -> None:
    """原子写（.tmp 再 replace），缩进 2，中文不转义。"""
    base = Path(act_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / META_NAME
    # 临时名带 pid+随机后缀：即使外部没拿锁，也不会两个写者互相覆盖 .tmp
    # （固定名会让并发下的 replace 报 FileNotFoundError）
    tmp = base / f"{META_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# ---------------- 防重复 ----------------

def dedup_check(meta: dict, hashes: list[str]) -> dict:
    """返回 {"new": [hash...], "duplicated": [hash...]}。

    同一批里重复出现的哈希只算一次；顺序按传入顺序保留。
    """
    known = (meta or {}).get("hashes") or {}
    new: list[str] = []
    duplicated: list[str] = []
    seen: set[str] = set()
    for digest in hashes or []:
        h = str(digest or "").strip().lower()
        if not h or h in seen:
            continue
        seen.add(h)
        if h in known:
            duplicated.append(h)
        else:
            new.append(h)
    return {"new": new, "duplicated": duplicated}


def _parse_time(value) -> datetime | None:
    """解析 uploaded_at；无时区的按 UTC 处理，解析不了返回 None。"""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def batch_duplicate(meta: dict, shot_date: str, scene: str,
                    hours: int = 24) -> str | None:
    """L3：24 小时内同活动+同日期+同场景已上传过，返回上次 batch_id；否则 None。"""
    try:
        day = _compact_date(shot_date)
    except ValueError:
        return None
    want_scene = str(scene or "").strip()
    now = datetime.now(timezone.utc)
    window = timedelta(hours=int(hours))
    for batch in reversed((meta or {}).get("batches") or []):
        if not isinstance(batch, dict):
            continue
        try:
            batch_day = _compact_date(batch.get("shot_date"))
        except ValueError:
            continue
        if batch_day != day:
            continue
        if str(batch.get("scene") or "").strip() != want_scene:
            continue
        uploaded = _parse_time(batch.get("uploaded_at"))
        if uploaded is None:
            continue
        if now - uploaded <= window:          # 未来时间戳也按「刚传过」处理
            return str(batch.get("batch_id") or "") or None
    return None


def _unique_batch_id(meta: dict, base: str) -> str:
    """同一秒内连续两次上传时给 batch_id 加后缀，避免元数据串号。"""
    used = {str(b.get("batch_id") or "") for b in (meta.get("batches") or [])
            if isinstance(b, dict)}
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


# ---------------- 保存一批照片 ----------------

def _prepare_bytes(data: bytes, suffix: str, compress: bool) -> tuple[bytes, int, int]:
    """决定落盘内容：默认压缩；compress=False 且本身是 JPEG 时保留原字节。"""
    if not compress and suffix in (".jpg", ".jpeg"):
        try:
            with Image.open(BytesIO(data)) as img:
                width, height = img.size
        except Exception as exc:
            raise ValueError(f"无法识别图片：{exc}") from exc
        return data, width, height
    return compress_image(data)


def save_photos(act_dir: Path, files: list[tuple[str, bytes]],
                shot_date: str, scene: str = "", note: str = "",
                uploaded_by: str = "", compress: bool = True,
                allow_duplicate: bool = False) -> dict:
    """核心：保存一批照片。

    files: [(原文件名, 原始字节), ...]
    返回 {
      "batch_id": "20261120-143025",
      "saved": 62, "skipped": [{"original": "IMG_001.jpg", "reason": "duplicate",
                                "existing": "20261110_005.jpg"}],
      "paths": ["11-活动照片资料/20261110_062.jpg", ...],
      "total_bytes": 123456,
      "duplicate_batch": None | "上次batch_id"
    }
    规则：
    - 逐张算 sha256，命中 meta["hashes"] → skipped(duplicate)
    - 目标路径已存在 → skipped(exists)
    - 落盘压缩后的 jpeg，更新 meta（batches / photos / hashes）
    - allow_duplicate=True 跳过 L2/L3
    """
    base = Path(act_dir)
    day = _compact_date(shot_date)            # 顺带校验日期
    photo_dir = base / PHOTO_SUBDIR
    photo_dir.mkdir(parents=True, exist_ok=True)

    # 整个「读 meta → 算序号 → 写图 → 写 meta」必须在同一临界区内，
    # 否则两个并发上传会拿到同一序号、覆盖同一张图、meta 只记住一个（审计 I-01）。
    with activity_lock(base):
        return _save_photos_locked(
            base, photo_dir, day, files, scene, note, uploaded_by,
            compress, allow_duplicate)


def _save_photos_locked(base: Path, photo_dir: Path, day: str,
                        files: list[tuple[str, bytes]], scene: str, note: str,
                        uploaded_by: str, compress: bool,
                        allow_duplicate: bool) -> dict:
    """save_photos 的临界区本体，调用方已持有 activity_lock。"""
    meta = load_meta(base)

    # L3：整批重复不落盘，把上次批次号交回调用方决定是否强制
    if not allow_duplicate:
        prev = batch_duplicate(meta, day, scene)
        if prev:
            return {"batch_id": "", "saved": 0, "skipped": [], "paths": [],
                    "total_bytes": 0, "duplicate_batch": prev}

    batch_id = _unique_batch_id(meta, datetime.now().strftime("%Y%m%d-%H%M%S"))
    seq = next_seq(base, day)
    saved = 0
    skipped: list[dict] = []
    paths: list[str] = []
    total_bytes = 0

    for original, raw in files:
        original = str(original or "")
        data = bytes(raw or b"")
        digest = sha256_of(data)

        # L2：内容级去重（allow_duplicate 时跳过）
        if not allow_duplicate and digest in meta["hashes"]:
            skipped.append({"original": original, "reason": "duplicate",
                            "existing": meta["hashes"][digest]})
            continue

        suffix = Path(original).suffix.lower()
        try:
            payload, _w, _h = _prepare_bytes(data, suffix, compress)
        except ValueError:
            skipped.append({"original": original, "reason": "invalid"})
            continue

        # L1：目标路径已存在（正常不会发生，next_seq 已避开）
        name = target_name(day, seq)
        dest = photo_dir / name
        if dest.exists():
            skipped.append({"original": original, "reason": "exists",
                            "existing": name})
            seq += 1
            continue

        dest.write_bytes(payload)
        meta["photos"].append({"name": name, "original": original,
                               "size": len(payload), "batch": batch_id})
        meta["hashes"][digest] = name
        paths.append(f"{photo_dir.name}/{name}")
        total_bytes += len(payload)
        saved += 1
        seq += 1

    if saved:
        meta["batches"].append({
            "batch_id": batch_id,
            "shot_date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "scene": str(scene or "").strip(),
            "note": str(note or ""),
            "uploaded_by": str(uploaded_by or ""),
            "uploaded_at": datetime.now(TZ_CN).isoformat(timespec="seconds"),
            "count": saved,
        })
        save_meta(base, meta)

    return {"batch_id": batch_id, "saved": saved, "skipped": skipped,
            "paths": paths, "total_bytes": total_bytes,
            "duplicate_batch": None}


# ---------------- 索引拼图 / 精选 ----------------

def _list_images(photo_dir: Path) -> list[Path]:
    """照片目录下的一级图片文件，按文件名排序；索引图自身不算。"""
    base = Path(photo_dir)
    if not base.is_dir():
        return []
    return sorted(
        (p for p in base.iterdir()
         if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
         and p.name != CONTACT_SHEET_NAME),
        key=lambda p: p.name,
    )


def make_contact_sheet(act_dir: Path, photo_dir: Path,
                       max_count: int = 9) -> Path | None:
    """生成 3 列索引拼图，只放前 max_count 张。返回路径，无图返回 None。"""
    images = _list_images(photo_dir)[:max(1, int(max_count))]
    if not images:
        return None

    cell = CONTACT_SHEET_CELL
    cols = CONTACT_SHEET_COLS
    gap = 6
    rows = math.ceil(len(images) / cols)
    width = cols * cell + gap * (cols + 1)
    height = rows * cell + gap * (rows + 1)
    sheet = Image.new("RGB", (width, height), (255, 255, 255))

    for idx, path in enumerate(images):
        try:
            with Image.open(path) as img:
                img.load()
                thumb = _to_rgb(img)
                thumb.thumbnail((cell, cell), Image.Resampling.LANCZOS)
                tile = Image.new("RGB", (cell, cell), (245, 245, 245))
                tile.paste(thumb, ((cell - thumb.width) // 2,
                                   (cell - thumb.height) // 2))
        except Exception:
            continue
        row, col = divmod(idx, cols)
        sheet.paste(tile, (gap + col * (cell + gap), gap + row * (cell + gap)))

    dest = Path(act_dir) / CONTACT_SHEET_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest, format="JPEG", quality=88, optimize=True)
    return dest


def pick_highlights(act_dir: Path, photo_dir: Path,
                    meta: dict | None = None,
                    count: int = PICK_COUNT) -> list[Path]:
    """挑精选：按文件大小排序（大的信息量通常多）+ 均匀采样。

    返回原图路径列表，不复制。meta 参数保留给后续按场景/批次加权，
    当前实现只依赖文件本身。
    """
    images = _list_images(photo_dir)
    if not images:
        return []
    want = max(1, int(count))
    ordered = sorted(images, key=lambda p: p.stat().st_size, reverse=True)
    if len(ordered) <= want:
        return ordered
    step = len(ordered) / want
    picked: list[Path] = []
    for i in range(want):
        path = ordered[min(len(ordered) - 1, int(i * step))]
        if path not in picked:
            picked.append(path)
    return picked


# ---------------- 存量压缩 ----------------

def compress_dir(act_dir: Path, photo_dir: Path,
                 dry_run: bool = False) -> dict:
    """把目录里已有的原图批量压缩（用于存量 130 张）。

    返回 {"compressed": n, "skipped": n, "saved_bytes": n}。
    dry_run=True 时只算不写，用来预估能省多少。
    """
    result = {"compressed": 0, "skipped": 0, "saved_bytes": 0}
    base = Path(photo_dir)
    if not base.is_dir():
        return result

    for path in _list_images(base):
        if path.suffix.lower() not in (".jpg", ".jpeg"):
            result["skipped"] += 1
            continue
        old = path.read_bytes()
        try:
            payload, _w, _h = compress_image(old)
        except ValueError:
            result["skipped"] += 1
            continue
        # 压完反而更大就不写，避免越压越糟
        if len(payload) >= len(old):
            result["skipped"] += 1
            continue
        result["compressed"] += 1
        result["saved_bytes"] += len(old) - len(payload)
        if not dry_run:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(payload)
            tmp.replace(path)
    return result


# ---------------- Cognee 入库文本 ----------------

def build_cognee_text(project: str, activity: str, code: str,
                      meta: dict) -> str:
    """生成入库文本：不存单张照片节点，只存「活动有照片」这条事实。"""
    meta = meta or {}
    photos = meta.get("photos") or []
    batches = [b for b in (meta.get("batches") or []) if isinstance(b, dict)]

    title = str(activity or "").strip() or "未命名活动"
    lines = [f"{title}（{str(project or '').strip() or '未知项目'}）有现场照片 {len(photos)} 张。"]
    if code:
        lines.append(f"项目编号：{code}")
    days = sorted({str(b.get("shot_date") or "").strip()
                   for b in batches if str(b.get("shot_date") or "").strip()})
    if days:
        lines.append("拍摄日期：" + "、".join(days))
    scenes: list[str] = []
    for batch in batches:
        scene = str(batch.get("scene") or "").strip()
        if scene and scene not in scenes:
            scenes.append(scene)
    if scenes:
        lines.append("场景：" + "、".join(scenes))
    if batches:
        lines.append(f"上传批次：{len(batches)} 批")
    return "\n".join(lines)
