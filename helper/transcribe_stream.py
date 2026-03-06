from __future__ import annotations

import io
import wave
from dataclasses import dataclass

import httpx
import numpy as np


class Transcriber:
    async def transcribe_pcm16(self, samples: np.ndarray, sample_rate: int) -> str:
        raise NotImplementedError

    async def aclose(self) -> None:
        return


def pcm16_to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    if samples.dtype != np.int16:
        samples = samples.astype(np.int16)
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(samples.tobytes())
        return buffer.getvalue()


@dataclass(slots=True)
class MockTranscriber(Transcriber):
    async def transcribe_pcm16(self, samples: np.ndarray, sample_rate: int) -> str:
        _ = samples
        _ = sample_rate
        return ""


@dataclass(slots=True)
class OpenAITranscriber(Transcriber):
    api_key: str
    model: str = "whisper-1"
    base_url: str = "https://api.openai.com/v1"
    language: str = ""

    def __post_init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=35.0)

    async def transcribe_pcm16(self, samples: np.ndarray, sample_rate: int) -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        wav_bytes = pcm16_to_wav_bytes(samples=samples, sample_rate=sample_rate)
        url = f"{self.base_url.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = {"model": self.model}
        if self.language:
            data["language"] = self.language
        files = {"file": ("chunk.wav", wav_bytes, "audio/wav")}
        response = await self._client.post(url, headers=headers, data=data, files=files)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = response.json()
            return (payload.get("text") or "").strip()
        return response.text.strip()

    async def aclose(self) -> None:
        await self._client.aclose()


def make_transcriber(
    provider: str,
    api_key: str,
    model: str,
    base_url: str,
    language: str,
) -> Transcriber:
    normalized = provider.strip().lower()
    if normalized == "openai":
        return OpenAITranscriber(
            api_key=api_key.strip(),
            model=model.strip() or "whisper-1",
            base_url=base_url.strip() or "https://api.openai.com/v1",
            language=language.strip(),
        )
    return MockTranscriber()

