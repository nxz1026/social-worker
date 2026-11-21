#!/usr/bin/env python3
"""API Key 明文加密存储。

安全边界（务必如实理解，不要当成「零信任」）：
- ``key_hash`` 只解决「数据库里不能反推出 Key」，无法防「有数据库权限的人看明文」；
- 这里改用对称加密保存明文，密钥单独放在 ``ROOT/.apikey-secret``，权限 0600；
- 拿到数据库 + 密钥文件 = 明文；只拿到数据库、但和密钥文件在同一台机器上，
  也不能假设安全（备份、root、同用户进程都可能读到）；
- 用户明确选择此方案，用于「改完用户直接发 Key」的场景。这是便利性优先，
  不是「即使服务器被攻破也安全」。

实现：
- 有 ``cryptography`` 时用 AES-GCM（随机 nonce，密文自带认证标签）；
- 没有时默认 **拒绝写入**（fail closed）；只有显式设置环境变量
  ``SOCIAL_WORK_ALLOW_WEAK_KEYSAFE=1`` 才退化为「HMAC 校验 + base64 明文」，
  token 以 ``w1:`` 开头，明确标注弱加密。已有的 ``w1:`` token 仍可读取。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

ENC_KEY_FILE = ".apikey-secret"      # 相对 ROOT，权限 0600
# 允许弱加密的开关；默认未设置 = 禁止
ALLOW_WEAK_ENV = "SOCIAL_WORK_ALLOW_WEAK_KEYSAFE"
_KEY_LEN = 32
_AES_PREFIX = "v1:"
_WEAK_PREFIX = "w1:"

try:                                  # 本机已装 cryptography 46.0.5
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AESGCM = True
except Exception:                     # pragma: no cover - 兜底分支
    AESGCM = None                     # type: ignore[assignment]
    _HAS_AESGCM = False


def _key_path(root) -> Path:
    """密钥文件路径：ROOT/.apikey-secret。"""
    return Path(root) / ENC_KEY_FILE


def _valid_key(data: bytes | None) -> bool:
    """密钥必须是 32 字节；长度不对说明文件损坏，不能静默覆盖。"""
    return bool(data) and len(data) == _KEY_LEN


def _load_or_create_key(root) -> bytes:
    """读密钥；不存在则生成 32 字节随机密钥并以 0600 写入。"""
    path = _key_path(root)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = secrets.token_bytes(_KEY_LEN)
        try:
            # O_EXCL 防止两个进程同时生成不同密钥；权限直接给 0600
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            data = path.read_bytes()
        else:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(path, 0o600)
    if not _valid_key(data):
        raise RuntimeError(
            f"API Key 密钥文件损坏（应为 32 字节）：{path}；"
            "请从备份恢复，否则旧 Key 的明文无法解密")
    # 读已有文件时也校验权限：0600 之外的权限意味着本机其他用户可解密全部明文
    try:
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            import logging
            logging.getLogger(__name__).warning(
                "API Key 密钥文件权限为 %o，应为 600：%s", mode, path)
    except OSError:
        pass
    return data


def _read_key(root) -> bytes | None:
    """只读密钥，不存在或损坏返回 None（解密失败不应顺手生成新密钥）。"""
    try:
        data = _key_path(root).read_bytes()
    except OSError:
        return None
    return data if _valid_key(data) else None


def _weak_allowed() -> bool:
    """只有显式设置 SOCIAL_WORK_ALLOW_WEAK_KEYSAFE=1/true/yes/on 才允许弱加密。"""
    return os.environ.get(ALLOW_WEAK_ENV, "").strip().lower() in {
        "1", "true", "yes", "on"}


def encrypt(plain: str, root) -> str:
    """加密明文。空串返回空串。

    有 cryptography：AES-GCM，token = ``v1:`` + base64(nonce + ciphertext)。
    无 cryptography：默认抛 RuntimeError（fail closed）；设置了
    ``SOCIAL_WORK_ALLOW_WEAK_KEYSAFE`` 才写 ``w1:`` + base64(明文) + HMAC。
    """
    text = str(plain or "")
    if not text:
        return ""
    key = _load_or_create_key(root)

    if _HAS_AESGCM:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
        return _AES_PREFIX + base64.urlsafe_b64encode(
            nonce + ciphertext).decode("ascii")

    if not _weak_allowed():
        raise RuntimeError(
            "未安装 cryptography，且未设置 "
            f"{ALLOW_WEAK_ENV}=1；拒绝用 base64 明文保存 API Key。"
            "请安装 cryptography（pip install cryptography）后重试")

    # 弱兜底：base64 只是编码，任何能读文件的人都能直接看到明文；
    # HMAC 只能防止 token 被悄悄篡改，不能提供机密性。
    log.warning("cryptography 不可用，API Key 以弱加密（base64 明文 + HMAC）"
                "保存；生产环境请安装 cryptography")
    raw = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    mac = hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()
    return _WEAK_PREFIX + raw + ":" + mac


def decrypt(token: str, root) -> str | None:
    """解密；任何失败（缺密钥、格式坏、认证失败）都返回 None。"""
    raw = str(token or "")
    if not raw:
        return None
    key = _read_key(root)
    if key is None:
        return None

    try:
        if raw.startswith(_AES_PREFIX):
            if not _HAS_AESGCM:
                return None
            blob = base64.urlsafe_b64decode(
                raw[len(_AES_PREFIX):].encode("ascii"))
            if len(blob) < 13:        # 12 字节 nonce + 至少 1 字节密文
                return None
            nonce, ciphertext = blob[:12], blob[12:]
            return AESGCM(key).decrypt(
                nonce, ciphertext, None).decode("utf-8")

        if raw.startswith(_WEAK_PREFIX):
            body = raw[len(_WEAK_PREFIX):]
            encoded, sep, mac = body.rpartition(":")
            if not sep or not encoded or not mac:
                return None
            plain = base64.urlsafe_b64decode(
                encoded.encode("ascii")).decode("utf-8")
            expect = hmac.new(
                key, plain.encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(mac, expect):
                return None
            return plain
    except Exception:
        return None
    return None
