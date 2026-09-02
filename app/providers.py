from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from app.database import AIRuntimeProfile


class AIProvider(ABC):
    @abstractmethod
    async def generate(
        self, system_prompt: str, history: list[dict[str, str]], user_text: str
    ) -> str:
        raise NotImplementedError


class OpenAICompatibleProvider(AIProvider):
    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        kwargs: dict[str, str] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
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
        if not content:
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


class ModuleProviderResolver:
    def __init__(
        self,
        provider_factory: Callable[[str, str, str, str | None], AIProvider]
        = create_provider,
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


def runtime_profile_is_configured(profile: AIRuntimeProfile) -> bool:
    return bool(os.getenv(profile.api_key_env, "").strip())
