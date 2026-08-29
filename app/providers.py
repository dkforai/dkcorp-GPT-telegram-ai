from __future__ import annotations

from abc import ABC, abstractmethod

from openai import AsyncOpenAI


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

