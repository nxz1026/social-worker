"""Office 文件转 PDF，供 Web 端预览。

docx / xlsx 浏览器无法原生渲染，前端要么引 mammoth.js（丢版式）要么调
在线服务。本机已有 LibreOffice，直接服务端转 PDF：版式 100% 保真、
前端零依赖、浏览器 iframe 原生显示。

转换结果缓存到活动目录的 .preview/，按源文件 (mtime, size) 失效。

实测（2026-11-23）：455KB docx 转 4 页 PDF 用 0.88s；xlsx 0.67s；
独立 profile 并发 3 个 1.8s 内完成。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# 交给 LibreOffice 转换的扩展名
OFFICE_EXTS = (".docx", ".xlsx", ".doc", ".xls", ".ppt", ".pptx", ".odt", ".ods")
# 直接原样返回的扩展名（浏览器自己会渲染）
DIRECT_EXTS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg")

# 转换超时（秒）。实测 1 秒内，给足余量；卡住就放弃并提示下载
TIMEOUT_SECONDS = 60

# 缓存目录名（活动目录下，以 . 开头会被文件列表过滤掉）
CACHE_DIR = ".preview"


class ConvertError(RuntimeError):
    """转换失败（LibreOffice 缺失、超时、源文件损坏等）。"""


def _soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    return None


def available() -> bool:
    """本机是否具备转换能力。"""
    return _soffice() is not None


def _cache_key(src: Path) -> str:
    """缓存键：路径 + mtime + size，源文件一变就换 key。"""
    st = src.stat()
    raw = f"{src.name}|{int(st.st_mtime)}|{st.st_size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def cached_pdf(src: Path, cache_root: Path) -> Path:
    """返回缓存 PDF 路径（不一定存在）；用 key 保证源文件变更后不复用旧缓存。"""
    return Path(cache_root) / f"{src.stem}-{_cache_key(src)}.pdf"


def to_pdf(src: Path, cache_root: Path) -> Path:
    """把 Office 文件转成 PDF，返回 PDF 路径。有缓存直接复用。

    失败抛 ConvertError，调用方据此回退到「只能下载」。
    """
    src = Path(src)
    if not src.is_file():
        raise ConvertError(f"源文件不存在：{src.name}")
    exe = _soffice()
    if exe is None:
        raise ConvertError("服务器未安装 LibreOffice，无法预览 Office 文件")

    pdf = cached_pdf(src, cache_root)
    if pdf.is_file() and pdf.stat().st_size > 0:
        return pdf

    cache_root = Path(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    # 每个转换任务用独立 LibreOffice profile，避免并发时抢锁
    with tempfile.TemporaryDirectory(prefix="lo-prof-") as prof, \
            tempfile.TemporaryDirectory(prefix="lo-out-") as out_dir:
        cmd = [
            exe,
            f"-env:UserInstallation=file://{prof}",
            "--headless",
            "--norestore",
            "--convert-to", "pdf",
            "--outdir", out_dir,
            str(src),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            raise ConvertError(
                f"转换超时（>{TIMEOUT_SECONDS}s）：{src.name}") from None
        produced = Path(out_dir) / f"{src.stem}.pdf"
        if proc.returncode != 0 or not produced.is_file():
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise ConvertError(
                f"LibreOffice 转换失败（exit {proc.returncode}）：{err[:200] or '无输出'}")
        # 先写临时名再原子替换，避免并发读到半个文件
        tmp = pdf.with_name(pdf.name + f".{os.getpid()}.tmp")
        shutil.copyfile(produced, tmp)
        os.replace(tmp, pdf)
    return pdf


def cleanup(cache_root: Path, keep: int = 20) -> int:
    """清掉旧缓存，只保留最近 keep 个（按 mtime）。返回删除数量。"""
    root = Path(cache_root)
    if not root.is_dir():
        return 0
    pdfs = sorted(root.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in pdfs[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed
