"""Bounded, read-only model discovery. Never send company data or generate text."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from app.credentials import PROVIDERS, CredentialStorageError, resolve_api_key, validate_encrypted_endpoint

if TYPE_CHECKING:
    from app.database import AIRuntimeProfile

MAX_MODELS = 5000
MAX_PAGES = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DISCOVERY_TIMEOUT_SECONDS = 30
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,149}$")


@dataclass(frozen=True)
class CatalogModel:
    model_id: str
    selectable: bool
    reason: str = ""


@dataclass(frozen=True)
class CatalogResult:
    status: str
    models: tuple[CatalogModel, ...] = ()


CATALOG_MESSAGES = {
    "success": "Akses daftar model berhasil diuji. Daftar model diperbarui; chat dan saldo belum diuji.",
    "authentication": "API key ditolak atau tidak punya izin membaca daftar model.",
    "rate_limit": "Provider membatasi permintaan. Coba lagi nanti.",
    "unavailable": "Provider tidak dapat dihubungi. Daftar sebelumnya dipertahankan.",
    "configuration": "Key atau endpoint belum siap. Periksa konfigurasi server.",
    "request": "Provider menolak permintaan daftar model. Periksa izin API key.",
    "response": "Daftar model tidak valid atau melebihi batas. Daftar sebelumnya dipertahankan.",
}


def classify_model(provider: str, model_id: str, metadata: dict) -> CatalogModel:
    # List APIs do not consistently advertise endpoint capabilities. This is an
    # explicit adapter policy, not a guarantee of account quota or model access.
    base = model_id.casefold()
    if base.startswith("ft:"):
        base = base.split(":")[1]
    supported = False
    if provider == "anthropic":
        supported = base.startswith("claude-")
    elif provider == "deepseek":
        supported = base.startswith("deepseek-")
    elif provider == "gemini":
        methods = metadata.get("supportedGenerationMethods", [])
        supported = base.startswith("gemini-") and isinstance(methods, list) and "generateContent" in methods
    elif provider == "openai":
        supported = bool(re.match(r"^(gpt-\d|chatgpt-|o\d)", base))
        if re.search(r"(?:^|-)pro(?:-|$)|codex|deep-research", base):
            supported = False  # Responses-only model families are not this adapter.
        if base in {"o1-preview", "o1-mini"} or base.startswith(("o1-preview-", "o1-mini-")):
            supported = False  # Legacy models without the system-role contract.
    if any(token in base for token in ("embedding", "embed-", "image", "audio", "realtime", "transcribe", "tts", "moderation", "live-", "robotics")):
        supported = False
    return CatalogModel(model_id, supported, "" if supported else "Belum didukung adapter chat teks bot")


async def _page(client: httpx.AsyncClient, endpoint: str, params: dict) -> dict:
    async with client.stream("GET", endpoint, params=params) as response:
        response.raise_for_status()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError("Model response too large")
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("Invalid model response")
        return data


async def discover_models(profile: AIRuntimeProfile) -> CatalogResult:
    """Full snapshot only; malformed/partial pages never replace a good catalog."""
    try:
        if not profile.active or profile.provider not in PROVIDERS:
            return CatalogResult("configuration")
        # Even legacy connections may discover only at the official endpoint.
        validate_encrypted_endpoint(profile.provider, profile.base_url)
        key = resolve_api_key(profile)
        provider = profile.provider
        endpoint = PROVIDERS[provider][1].rstrip("/") + "/models"
        params: dict = {}
        headers = {"Authorization": f"Bearer {key}"}
        if provider == "anthropic":
            endpoint = "https://api.anthropic.com/v1/models"
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            params = {"limit": 1000}
        elif provider == "gemini":
            endpoint = "https://generativelanguage.googleapis.com/v1beta/models"
            headers = {"x-goog-api-key": key}
            params = {"pageSize": 1000}
        models: dict[str, CatalogModel] = {}
        cursors: set[str] = set()
        async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=False) as client:
                for _ in range(MAX_PAGES):
                    data = await _page(client, endpoint, params)
                    entries = data.get("models" if provider == "gemini" else "data")
                    if not isinstance(entries, list):
                        raise ValueError("Missing model array")
                    for entry in entries:
                        if not isinstance(entry, dict):
                            raise ValueError("Invalid model entry")
                        ident = entry.get("name" if provider == "gemini" else "id")
                        if provider == "gemini" and isinstance(ident, str):
                            if not ident.startswith("models/"):
                                raise ValueError("Invalid model name")
                            ident = ident.removeprefix("models/")
                        if not isinstance(ident, str) or not MODEL_ID.fullmatch(ident) or key in ident:
                            raise ValueError("Invalid model ID")
                        candidate = classify_model(provider, ident, entry)
                        if ident in models and models[ident] != candidate:
                            raise ValueError("Conflicting duplicate model")
                        models[ident] = candidate
                        if len(models) > MAX_MODELS:
                            raise ValueError("Too many models")
                    cursor = None
                    if provider == "gemini":
                        cursor = data.get("nextPageToken")
                        cursor_param = "pageToken"
                    else:
                        if not isinstance(data.get("has_more", False), bool):
                            raise ValueError("Invalid pagination")
                        if data.get("has_more"):
                            cursor = data.get("last_id")
                            if not cursor:
                                raise ValueError("Missing pagination cursor")
                        cursor_param = "after_id" if provider == "anthropic" else "after"
                    if cursor is None or cursor == "":
                        return CatalogResult("success", tuple(sorted(models.values(), key=lambda m: m.model_id)))
                    if (not isinstance(cursor, str) or len(cursor) > 2048
                            or cursor in cursors or key in cursor or not entries):
                        raise ValueError("Invalid pagination cursor")
                    cursors.add(cursor)
                    params[cursor_param] = cursor
        return CatalogResult("response")
    except CredentialStorageError:
        return CatalogResult("configuration")
    except (httpx.RequestError, TimeoutError):
        return CatalogResult("unavailable")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            return CatalogResult("authentication")
        if status == 429:
            return CatalogResult("rate_limit")
        return CatalogResult("unavailable" if status == 408 or status >= 500 else "request")
    except Exception:
        # Provider payloads/exceptions can reflect secrets; never persist or log them.
        return CatalogResult("response")
