from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

if TYPE_CHECKING:
    from app.database import AIRuntimeProfile

logger = logging.getLogger(__name__)
MODULE_AI_TIMEOUT_SECONDS = 30.0


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
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if module_runtime:
            # One attempt per selected AI; the resolver owns the fallback policy.
            kwargs.update(timeout=MODULE_AI_TIMEOUT_SECONDS, max_retries=0)
        self.client = AsyncOpenAI(**kwargs)
        self.model = model

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


def _create_module_provider(
    provider_name: str, api_key: str, model: str, base_url: str | None
) -> AIProvider:
    if provider_name not in {"openai", "deepseek"}:
        raise RuntimeCredentialError("Provider Module tidak didukung")
    return OpenAICompatibleProvider(
        api_key, model, base_url, module_runtime=True
    )


def _can_use_backup(error: Exception) -> bool:
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
        api_key = os.getenv(profile.api_key_env, "").strip()
        if not api_key:
            raise RuntimeCredentialError(
                f"Environment variable {profile.api_key_env} belum dikonfigurasi"
            )
        base_url = profile.base_url or (
            "https://api.deepseek.com" if profile.provider == "deepseek" else ""
        )
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
                if key[0] != profile.profile_id
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
    ) -> str:
        # Missing/disabled primary credentials must not trigger failover.
        provider = self.resolve(primary)
        try:
            async with asyncio.timeout(MODULE_AI_TIMEOUT_SECONDS):
                return await provider.generate(system_prompt, history, user_text)
        except Exception as exc:
            status = exc.status_code if isinstance(exc, APIStatusError) else None
            logger.warning(
                "module_ai_failed primary=%s error_type=%s status=%s",
                primary.profile_id, type(exc).__name__, status,
            )
            if not _can_use_backup(exc):
                raise ModuleGenerationError("AI utama gagal memproses pesan") from None

        # Resolve only after failure so a newly disabled backup is never used.
        backup = backup_loader()
        if backup is None:
            raise ModuleGenerationError("AI utama tidak tersedia dan tidak ada AI cadangan")
        if backup.profile_id == primary.profile_id:
            raise RuntimeCredentialError("AI utama dan AI cadangan harus berbeda")
        backup_provider = self.resolve(backup)
        logger.warning(
            "module_ai_failover primary=%s backup=%s",
            primary.profile_id, backup.profile_id,
        )
        try:
            async with asyncio.timeout(MODULE_AI_TIMEOUT_SECONDS):
                return await backup_provider.generate(system_prompt, history, user_text)
        except Exception as exc:
            status = exc.status_code if isinstance(exc, APIStatusError) else None
            logger.warning(
                "module_ai_failed backup=%s error_type=%s status=%s",
                backup.profile_id, type(exc).__name__, status,
            )
            raise ModuleGenerationError("AI utama dan AI cadangan tidak tersedia") from None


def runtime_profile_is_configured(profile: AIRuntimeProfile) -> bool:
    return bool(os.getenv(profile.api_key_env, "").strip())
