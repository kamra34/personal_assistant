from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import settings


class Provider(Protocol):
    async def generate(self, model: str, system_prompt: str, user_prompt: str) -> str: ...


@dataclass(slots=True)
class MockProvider:
    async def generate(self, model: str, system_prompt: str, user_prompt: str) -> str:
        _ = model
        _ = system_prompt
        compact = " ".join(user_prompt.split())
        snippet = compact[:260]
        return (
            "Suggested response:\n"
            f"- Key point: {snippet}\n"
            "- Action: Ask one clarifying question, then confirm next step."
        )


@dataclass(slots=True)
class OpenAICompatibleProvider:
    api_key: str
    base_url: str

    async def generate(self, model: str, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"]["content"].strip()


@dataclass(slots=True)
class AnthropicProvider:
    api_key: str

    async def generate(self, model: str, system_prompt: str, user_prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": model,
            "max_tokens": 400,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        texts = [block.get("text", "") for block in data.get("content", [])]
        return "\n".join(part for part in texts if part).strip()


def get_provider(provider_name: str) -> Provider:
    normalized = (provider_name or "").strip().lower()
    if normalized in {"openai", "openai-compatible"}:
        return OpenAICompatibleProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
    if normalized in {"anthropic", "claude"}:
        return AnthropicProvider(api_key=settings.anthropic_api_key)
    return MockProvider()

