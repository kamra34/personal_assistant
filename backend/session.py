from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from fastapi import WebSocket

from .providers import get_provider


DEFAULT_SYSTEM_PROMPT = """You are a live meeting response assistant.
Produce one response suggestion that is:
- accurate and directly relevant to the ongoing conversation
- concise but complete (roughly 3-6 sentences)
- professionally phrased and easy to say aloud
- formatted as short bullet points
If information is uncertain, state uncertainty briefly and suggest a clarifying question.
Prioritize the latest utterance first. Do not repeat old answers unless the latest utterance asks for it.
Avoid repeated preambles like "Yes, I can provide insights..."; start directly with the answer."""


@dataclass(slots=True)
class LiveSession:
    session_id: str
    provider_name: str = "mock"
    model: str = "gpt-4o-mini"
    context: str = ""
    history_mode: str = "focused"  # focused | full | stateless
    history_lines: int = 10
    transcript_lines: deque[str] = field(default_factory=lambda: deque(maxlen=80))
    sockets: set[WebSocket] = field(default_factory=set)
    capture_sockets: set[WebSocket] = field(default_factory=set)
    generation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def configure(self, payload: dict[str, Any]) -> None:
        provider = payload.get("provider")
        model = payload.get("model")
        context = payload.get("context")
        history_mode = payload.get("history_mode")
        history_lines = payload.get("history_lines")
        if isinstance(provider, str) and provider.strip():
            self.provider_name = provider.strip()
        if isinstance(model, str) and model.strip():
            self.model = model.strip()
        if isinstance(context, str):
            self.context = context.strip()
        if isinstance(history_mode, str) and history_mode.strip().lower() in {
            "focused",
            "full",
            "stateless",
        }:
            self.history_mode = history_mode.strip().lower()
        if isinstance(history_lines, int) and 1 <= history_lines <= 40:
            self.history_lines = history_lines
        if isinstance(history_lines, str) and history_lines.isdigit():
            parsed = int(history_lines)
            if 1 <= parsed <= 40:
                self.history_lines = parsed

    def add_transcript(self, source: str, text: str) -> None:
        label = source.strip().lower() if source else "unknown"
        self.transcript_lines.append(f"[{label}] {text.strip()}")

    def build_user_prompt(self, latest_source: str, latest_text: str) -> str:
        lines = list(self.transcript_lines)
        if lines and latest_text.strip() and lines[-1].endswith(latest_text.strip()):
            lines = lines[:-1]
        if self.history_mode == "stateless":
            context_window: list[str] = []
        elif self.history_mode == "full":
            context_window = lines
        else:
            context_window = lines[-self.history_lines :]
        transcript = "\n".join(context_window) if context_window else "(none)"
        context = self.context or "No extra context provided."
        return (
            "Optional context:\n"
            f"{context}\n\n"
            "Latest utterance to answer now:\n"
            f"[{latest_source}] {latest_text.strip()}\n\n"
            "Recent conversation context (for disambiguation only):\n"
            f"{transcript}\n\n"
            "Task:\n"
            "- Answer the latest utterance directly.\n"
            "- Use context only if needed.\n"
            "- Do not restate prior topics unless asked.\n"
            "- If the latest utterance is unclear/noisy, ask one brief clarifying question."
        )

    async def generate_suggestion(self, latest_source: str, latest_text: str) -> dict[str, Any]:
        async with self.generation_lock:
            provider = get_provider(self.provider_name)
            start = perf_counter()
            text = await provider.generate(
                model=self.model,
                system_prompt=DEFAULT_SYSTEM_PROMPT,
                user_prompt=self.build_user_prompt(
                    latest_source=latest_source,
                    latest_text=latest_text,
                ),
            )
            latency_ms = int((perf_counter() - start) * 1000)
            return {
                "type": "suggestion",
                "session_id": self.session_id,
                "provider": self.provider_name,
                "model": self.model,
                "latency_ms": latency_ms,
                "text": text,
            }
