"""Server-only credential storage. Never return a decrypted key to templates."""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

if TYPE_CHECKING:
    from app.database import AIRuntimeProfile

ENCRYPTION_KEY_ENV = "AI_CREDENTIAL_ENCRYPTION_KEY"
PROVIDERS = {
    "openai": ("OpenAI / GPT", "https://api.openai.com/v1"),
    "anthropic": ("Anthropic / Claude", "https://api.anthropic.com"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com"),
    "gemini": ("Google / Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
}


class CredentialStorageError(ValueError):
    """Only static, non-sensitive messages may cross this boundary."""


def _cipher() -> Fernet:
    try:
        key = os.environ.get(ENCRYPTION_KEY_ENV, "").strip()
        if not key:
            raise ValueError
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise CredentialStorageError(
            "Kunci enkripsi aplikasi belum tersedia atau tidak valid. "
            "Atur AI_CREDENTIAL_ENCRYPTION_KEY di environment server."
        ) from None


def encryption_is_ready() -> bool:
    try:
        _cipher()
        return True
    except CredentialStorageError:
        return False


def _validate_key(value: str) -> str:
    key = value.strip()
    if not 1 <= len(key) <= 4096 or any(not 33 <= ord(char) <= 126 for char in key):
        raise CredentialStorageError("API key wajib diisi, maksimal 4096 karakter tanpa spasi di tengah.")
    return key


def encrypt_api_key(profile_id: str, provider: str, value: str) -> str:
    payload = {"version": 1, "profile_id": profile_id, "provider": provider,
               "key": _validate_key(value)}
    return _cipher().encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")


def resolve_api_key(profile: AIRuntimeProfile) -> str:
    if not profile.api_key_ciphertext:
        key = os.environ.get(profile.api_key_env, "").strip()
        if not key:
            raise CredentialStorageError(f"Environment variable {profile.api_key_env} belum dikonfigurasi.")
        return key
    cipher = _cipher()
    try:
        payload = json.loads(cipher.decrypt(profile.api_key_ciphertext.encode("ascii")))
        if (payload["version"], payload["profile_id"], payload["provider"]) != (
            1, profile.profile_id, profile.provider
        ):
            raise ValueError
        return _validate_key(payload["key"])
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        raise CredentialStorageError(
            "API key tersimpan tidak dapat dibuka. Periksa kunci enkripsi server atau isi ulang API key."
        ) from None


def validate_encrypted_endpoint(provider: str, base_url: str) -> None:
    if provider not in PROVIDERS or (base_url and base_url.rstrip("/") != PROVIDERS[provider][1].rstrip("/")):
        raise CredentialStorageError("API key tersimpan hanya boleh memakai alamat resmi provider.")
