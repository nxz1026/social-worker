"""api.keysafe：API Key 明文加密 / 解密。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))

import keysafe  # noqa: E402


def test_encrypt_decrypt_roundtrip(tmp_path):
    plain = "sk-sw-pnPzabcdefghijklmnopqrstuvwxyz0123456789"
    token = keysafe.encrypt(plain, tmp_path)

    assert token.startswith("v1:")
    assert plain not in token
    assert keysafe.decrypt(token, tmp_path) == plain


def test_key_file_is_0600(tmp_path):
    keysafe.encrypt("sk-sw-test", tmp_path)
    path = tmp_path / keysafe.ENC_KEY_FILE
    assert path.is_file()
    assert path.stat().st_size == 32
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_decrypt_missing_key_returns_none(tmp_path):
    token = keysafe.encrypt("sk-sw-test", tmp_path)
    (tmp_path / keysafe.ENC_KEY_FILE).unlink()
    assert keysafe.decrypt(token, tmp_path) is None


def test_decrypt_bad_token_returns_none(tmp_path):
    keysafe.encrypt("sk-sw-test", tmp_path)
    assert keysafe.decrypt("v1:not-base64", tmp_path) is None
    assert keysafe.decrypt("garbage", tmp_path) is None
    assert keysafe.decrypt("", tmp_path) is None


def test_empty_plain_returns_empty(tmp_path):
    assert keysafe.encrypt("", tmp_path) == ""
    assert keysafe.decrypt("", tmp_path) is None


def test_weak_fallback_roundtrip(tmp_path, monkeypatch):
    """没有 cryptography 时的弱兜底仍能往返，但 token 明确标 w1。"""
    monkeypatch.setattr(keysafe, "_HAS_AESGCM", False)
    monkeypatch.setattr(keysafe, "AESGCM", None)
    token = keysafe.encrypt("sk-sw-weak", tmp_path)

    assert token.startswith("w1:")
    assert keysafe.decrypt(token, tmp_path) == "sk-sw-weak"
