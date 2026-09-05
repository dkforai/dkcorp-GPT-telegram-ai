from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextvars import ContextVar
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
from httpx import AsyncClient
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from app.credentials import (
    PROVIDERS, CredentialStorageError, resolve_api_key, validate_encrypted_endpoint,
)

if TYPE_CHECKING:
    from app.database import AIRuntimeProfile

logger = logging.getLogger(__name__)
MODULE_AI_TIMEOUT_SECONDS = 300.0
PRIMARY_AI_TIMEOUT_SECONDS = 180.0
AI_PROBE_TIMEOUT_SECONDS = 30.0
ARTICLE_WEB_TIMEOUT_SECONDS = MODULE_AI_TIMEOUT_SECONDS
_request_timeout: ContextVar[float | None] = ContextVar("module_request_timeout", default=None)


def effective_timeout() -> float:
    value = _request_timeout.get()
    return MODULE_AI_TIMEOUT_SECONDS if value is None else value


class AIProvider(ABC):
    @abstractmethod
    async def generate(
        self, system_prompt: str, history: list[dict[str, str]], user_text: str
    ) -> str:
        raise NotImplementedError


class OpenAICompatibleProvider(AIProvider):
    def __init__(
        self, api_key: str, model: str, base_url: str | None = None,
        *, module_runtime: bool = False,
    ):
        self.module_runtime = module_runtime
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if module_runtime:
            # One attempt per selected AI; the resolver owns the fallback policy.
            kwargs.update(timeout=MODULE_AI_TIMEOUT_SECONDS, max_retries=0)
            kwargs["http_client"] = AsyncClient(
                timeout=MODULE_AI_TIMEOUT_SECONDS, follow_redirects=False
            )
        self.client = AsyncOpenAI(**kwargs)
        self.model = model

    async def probe(self) -> None:
        # No company context, history or user information is sent by a probe.
        output_limit = (
            {"max_completion_tokens": 64}
            if self.client.base_url.host == "api.openai.com"
            else {"max_tokens": 64}
        )
        response = await self.client.chat.completions.create(
            model=self.model, messages=[{"role": "user", "content": "Reply OK."}], **output_limit
        )
        if not response.choices or response.choices[0].message is None:
            raise ModuleGenerationError("Respons tes AI tidak valid")

    async def close(self) -> None:
        await self.client.close()

    async def generate(
        self, system_prompt: str, history: list[dict[str, str]], user_text: str
    ) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_text},
        ]
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **({"timeout": effective_timeout()} if self.module_runtime else {}),
        )
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise RuntimeError("AI provider mengembalikan jawaban kosong")
        return content.strip()


def create_provider(
    provider_name: str, api_key: str, model: str, base_url: str | None
) -> AIProvider:
    if provider_name not in {"openai", "deepseek"}:
        raise ValueError(f"Provider tidak didukung: {provider_name}")
    return OpenAICompatibleProvider(api_key=api_key, model=model, base_url=base_url)


class RuntimeCredentialError(RuntimeError):
    """A safe operational error that never contains the secret value."""


class ModuleGenerationError(RuntimeError):
    """Safe failure without the provider's response body, prompt, URL or key."""


class ProviderRequestError(RuntimeError):
    def __init__(self, status_code: int | None = None):
        super().__init__("Provider request failed")
        self.status_code = status_code


class AnthropicProvider(AIProvider):
    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        self.model = model
        self.client = AsyncClient(
            base_url=(base_url or PROVIDERS["anthropic"][1]).rstrip("/") + "/",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=MODULE_AI_TIMEOUT_SECONDS, follow_redirects=False,
        )

    async def _request(
        self, system: str, messages: list[dict[str, str]], limit: int
    ) -> dict:
        try:
            response = await self.client.post("v1/messages", json={
                "model": self.model, "max_tokens": limit, "system": system, "messages": messages,
            }, timeout=effective_timeout())
        except httpx.RequestError:
            raise ProviderRequestError() from None
        if response.status_code != 200:
            raise ProviderRequestError(response.status_code)
        try:
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise ValueError
            return data
        except ValueError:
            raise ModuleGenerationError("Respons AI tidak valid") from None

    async def generate(
        self, system_prompt: str, history: list[dict[str, str]], user_text: str
    ) -> str:
        data = await self._request(
            system_prompt, [*history, {"role": "user", "content": user_text}], 4096
        )
        content = "\n".join(
            block["text"] for block in data["content"]
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ).strip()
        if not content:
            raise ModuleGenerationError("AI provider mengembalikan jawaban kosong")
        return content

    async def probe(self) -> None:
        await self._request("", [{"role": "user", "content": "Reply OK."}], 64)

    async def close(self) -> None:
        await self.client.aclose()


def _create_module_provider(
    provider_name: str, api_key: str, model: str, base_url: str | None
) -> AIProvider:
    if provider_name not in PROVIDERS:
        raise RuntimeCredentialError("Provider Module tidak didukung")
    endpoint = base_url or PROVIDERS[provider_name][1]
    if provider_name == "anthropic":
        return AnthropicProvider(api_key, model, endpoint)
    return OpenAICompatibleProvider(api_key, model, endpoint, module_runtime=True)


def _can_use_backup(error: Exception) -> bool:
    if isinstance(error, ProviderRequestError):
        return (
            error.status_code is None or error.status_code in {408, 429}
            or 500 <= error.status_code < 600
        )
    if isinstance(error, (APIConnectionError, TimeoutError)):
        return True
    return isinstance(error, APIStatusError) and (
        error.status_code in {408, 429} or 500 <= error.status_code < 600
    )


class ModuleProviderResolver:
    def __init__(
        self,
        provider_factory: Callable[[str, str, str, str | None], AIProvider]
        = _create_module_provider,
    ):
        self._provider_factory = provider_factory
        self._cache: dict[tuple[str, str, str, str, str], AIProvider] = {}

    def resolve(self, profile: AIRuntimeProfile | None) -> AIProvider:
        if profile is None or not profile.active:
            raise RuntimeCredentialError("Credential profile Module tidak aktif")
        try:
            if profile.api_key_ciphertext:
                validate_encrypted_endpoint(profile.provider, profile.base_url)
            api_key = resolve_api_key(profile)
        except CredentialStorageError as exc:
            raise RuntimeCredentialError(str(exc)) from None
        base_url = profile.base_url or PROVIDERS.get(profile.provider, ("", ""))[1]
        if profile.provider == "openai" and not profile.base_url:
            base_url = ""
        key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        cache_key = (
            profile.profile_id,
            profile.provider,
            profile.model,
            base_url,
            key_fingerprint,
        )
        provider = self._cache.get(cache_key)
        if provider is None:
            self._cache = {
                key: value
                for key, value in self._cache.items()
                if key[0] != profile.profile_id or (key[2] != profile.model and key[4] == key_fingerprint)
            }
            provider = self._provider_factory(
                profile.provider,
                api_key,
                profile.model,
                base_url or None,
            )
            self._cache[cache_key] = provider
        return provider

    async def generate(
        self,
        primary: AIRuntimeProfile | None,
        backup_loader: Callable[[], AIRuntimeProfile | None],
        system_prompt: str,
        history: list[dict[str, str]],
        user_text: str,
        *, timeout_seconds: float | None = None,
    ) -> str:
        total_timeout = MODULE_AI_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        if total_timeout <= 0 or total_timeout > MODULE_AI_TIMEOUT_SECONDS:
            raise ValueError("Unsupported module timeout")
        return await self._generate(
            primary, backup_loader, system_prompt, history, user_text,
            total_timeout=total_timeout,
        )

    async def _generate(
        self, primary: AIRuntimeProfile | None,
        backup_loader: Callable[[], AIRuntimeProfile | None],
        system_prompt: str, history: list[dict[str, str]], user_text: str,
        *, total_timeout: float,
    ) -> str:
        deadline = time.monotonic() + total_timeout
        # Missing/disabled primary credentials must not trigger failover.
        provider = self.resolve(primary)
        # Reserve part of the total budget for one optional backup attempt.
        primary_timeout = min(PRIMARY_AI_TIMEOUT_SECONDS, total_timeout * 0.60)
        token = _request_timeout.set(primary_timeout)
        try:
            async with asyncio.timeout(primary_timeout):
                return await provider.generate(system_prompt, history, user_text)
        except Exception as exc:
            status = exc.status_code if isinstance(exc, (APIStatusError, ProviderRequestError)) else None
            logger.warning(
                "module_ai_failed primary=%s error_type=%s status=%s timeout_seconds=%s",
                primary.profile_id, type(exc).__name__, status, effective_timeout(),
            )
            if not _can_use_backup(exc):
                raise ModuleGenerationError("AI utama gagal memproses pesan") from None
        finally:
            _request_timeout.reset(token)

        # Resolve only after failure so a newly disabled backup is never used.
        backup = backup_loader()
        if backup is None:
            raise ModuleGenerationError("AI utama tidak tersedia dan tidak ada AI cadangan")
        if (backup.profile_id, backup.model) == (primary.profile_id, primary.model):
            raise RuntimeCredentialError("AI utama dan AI cadangan harus berbeda")
        backup_provider = self.resolve(backup)
        logger.warning(
            "module_ai_failover primary=%s backup=%s",
            primary.profile_id, backup.profile_id,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModuleGenerationError("Batas waktu total AI telah habis")
        token = _request_timeout.set(remaining)
        try:
            async with asyncio.timeout(remaining):
                return await backup_provider.generate(system_prompt, history, user_text)
        except Exception as exc:
            status = exc.status_code if isinstance(exc, (APIStatusError, ProviderRequestError)) else None
            logger.warning(
                "module_ai_failed backup=%s error_type=%s status=%s timeout_seconds=%s",
                backup.profile_id, type(exc).__name__, status, effective_timeout(),
            )
            raise ModuleGenerationError("AI utama dan AI cadangan tidak tersedia") from None
        finally:
            _request_timeout.reset(token)


def runtime_profile_is_configured(profile: AIRuntimeProfile) -> bool:
    try:
        resolve_api_key(profile)
        return True
    except CredentialStorageError:
        return False


AI_TEST_MESSAGES = {
    "success": "Koneksi dan model berhasil diuji. AI dapat dipilih pada Module jika aktif.",
    "authentication": "API key ditolak atau tidak memiliki izin. Periksa key dan akses model.",
    "rate_limit": "Provider membatasi permintaan atau kuota habis. Periksa akun provider.",
    "unavailable": "Provider tidak dapat dihubungi atau sedang bermasalah. Coba lagi nanti.",
    "configuration": "Credential belum siap. Periksa environment atau kunci enkripsi server.",
    "request": "Provider menolak konfigurasi. Periksa ID model dan dukungan API model tersebut.",
    "response": "Respons provider tidak dapat diproses. Periksa kompatibilitas model.",
}


async def test_ai_connection(profile: AIRuntimeProfile) -> str:
    """Explicit admin probe, never fail over and never use business data."""
    provider = None
    try:
        if profile.api_key_ciphertext:
            validate_encrypted_endpoint(profile.provider, profile.base_url)
        provider = _create_module_provider(
            profile.provider, resolve_api_key(profile), profile.model, profile.base_url or None
        )
        async with asyncio.timeout(AI_PROBE_TIMEOUT_SECONDS):
            await provider.probe()
        return "success"
    except (CredentialStorageError, RuntimeCredentialError):
        return "configuration"
    except (APIConnectionError, TimeoutError):
        return "unavailable"
    except (APIStatusError, ProviderRequestError) as exc:
        status = exc.status_code
        if status in {401, 403}:
            return "authentication"
        if status == 429:
            return "rate_limit"
        if status is None or status == 408 or status >= 500:
            return "unavailable"
        return "request"
    except Exception:
        # Never expose SDK exceptions, which can embed a key or request body.
        return "response"
    finally:
        if provider is not None:
            try:
                await provider.close()
            except Exception:
                logger.warning("ai_probe_client_close_failed")
