from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import ExitStack
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import sounddevice as sd
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus
from dotenv import load_dotenv

try:
    from helper.transcribe_stream import Transcriber, make_transcriber
except ModuleNotFoundError:  # Support direct script execution from helper/ path.
    from transcribe_stream import Transcriber, make_transcriber

BASE_DIR = Path(__file__).resolve().parents[1]


def load_runtime_env() -> None:
    candidates: list[Path] = [BASE_DIR / ".env", Path.cwd() / ".env"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve().parent / ".env")
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.exists():
            continue
        load_dotenv(resolved, override=False)


load_runtime_env()


@dataclass(slots=True)
class StreamConfig:
    source: str
    device_index: int
    sample_rate: int
    channels: int
    loopback: bool


@dataclass(slots=True)
class VadConfig:
    speech_start_rms: float
    speech_end_rms: float
    end_silence_seconds: float
    min_utterance_seconds: float
    max_utterance_seconds: float
    min_start_seconds: float
    hangover_seconds: float
    pre_roll_seconds: float
    cooldown_seconds: float
    adaptive_noise: bool
    noise_floor_alpha: float
    start_rms_ratio: float
    end_rms_ratio: float


@dataclass(slots=True)
class UtteranceChunk:
    source: str
    sample_rate: int
    samples: np.ndarray
    duration_seconds: float
    reason: str
    avg_rms: float
    emitted_at: float


@dataclass(slots=True)
class AdaptiveEndpointDetector:
    source: str
    sample_rate: int
    config: VadConfig
    in_speech: bool = False
    speech_run_seconds: float = 0.0
    speech_seconds: float = 0.0
    silence_seconds: float = 0.0
    utterance_frames: list[np.ndarray] = field(default_factory=list)
    pre_roll_frames: deque[np.ndarray] = field(default_factory=deque)
    pre_roll_durations: deque[float] = field(default_factory=deque)
    pre_roll_total_seconds: float = 0.0
    pre_roll_total_rms: float = 0.0
    utterance_rms_sum: float = 0.0
    utterance_rms_count: int = 0
    noise_floor_rms: float = 80.0

    def __post_init__(self) -> None:
        self.noise_floor_rms = max(20.0, float(self.config.speech_end_rms))

    def _trim_pre_roll(self) -> None:
        limit = max(0.0, float(self.config.pre_roll_seconds))
        while self.pre_roll_total_seconds > limit and self.pre_roll_frames:
            old_frame = self.pre_roll_frames.popleft()
            old_seconds = self.pre_roll_durations.popleft()
            old_rms = compute_rms(old_frame)
            self.pre_roll_total_seconds -= old_seconds
            self.pre_roll_total_rms = max(0.0, self.pre_roll_total_rms - old_rms)

    def _add_pre_roll(self, frame: np.ndarray, frame_seconds: float) -> None:
        if self.config.pre_roll_seconds <= 0:
            return
        self.pre_roll_frames.append(frame)
        self.pre_roll_durations.append(frame_seconds)
        self.pre_roll_total_seconds += frame_seconds
        self.pre_roll_total_rms += compute_rms(frame)
        self._trim_pre_roll()

    def _consume_pre_roll_into_utterance(self) -> None:
        if not self.pre_roll_frames:
            return
        self.utterance_frames.extend(self.pre_roll_frames)
        self.speech_seconds += self.pre_roll_total_seconds
        if self.pre_roll_frames:
            self.utterance_rms_sum += self.pre_roll_total_rms
            self.utterance_rms_count += len(self.pre_roll_frames)
        self.pre_roll_frames.clear()
        self.pre_roll_durations.clear()
        self.pre_roll_total_seconds = 0.0
        self.pre_roll_total_rms = 0.0

    def _dynamic_start_threshold(self) -> float:
        if not self.config.adaptive_noise:
            return self.config.speech_start_rms
        return max(self.config.speech_start_rms, self.noise_floor_rms * self.config.start_rms_ratio)

    def _dynamic_end_threshold(self) -> float:
        if not self.config.adaptive_noise:
            return self.config.speech_end_rms
        return max(self.config.speech_end_rms, self.noise_floor_rms * self.config.end_rms_ratio)

    def _update_noise_floor(self, frame_rms: float) -> None:
        if not self.config.adaptive_noise:
            return
        alpha = min(max(self.config.noise_floor_alpha, 0.5), 0.995)
        self.noise_floor_rms = alpha * self.noise_floor_rms + (1.0 - alpha) * frame_rms
        self.noise_floor_rms = min(max(self.noise_floor_rms, 20.0), max(25.0, self.config.speech_start_rms * 1.7))

    def _reset_utterance_state(self) -> None:
        self.in_speech = False
        self.speech_run_seconds = 0.0
        self.speech_seconds = 0.0
        self.silence_seconds = 0.0
        self.utterance_frames = []
        self.utterance_rms_sum = 0.0
        self.utterance_rms_count = 0

    def _finalize(self, reason: str) -> UtteranceChunk | None:
        if not self.utterance_frames:
            self._reset_utterance_state()
            return None
        utterance = np.concatenate(self.utterance_frames)
        duration = utterance.size / float(self.sample_rate)
        avg_rms = self.utterance_rms_sum / float(self.utterance_rms_count or 1)
        self._reset_utterance_state()
        if duration <= 0.0:
            return None
        return UtteranceChunk(
            source=self.source,
            sample_rate=self.sample_rate,
            samples=utterance,
            duration_seconds=duration,
            reason=reason,
            avg_rms=avg_rms,
            emitted_at=perf_counter(),
        )

    def feed(self, frame: np.ndarray) -> UtteranceChunk | None:
        if frame.size == 0:
            return None
        frame_seconds = frame.size / float(self.sample_rate)
        if frame_seconds <= 0.0:
            return None
        frame_rms = compute_rms(frame)

        if not self.in_speech:
            self._update_noise_floor(frame_rms)
            self._add_pre_roll(frame, frame_seconds)
            if frame_rms >= self._dynamic_start_threshold():
                self.speech_run_seconds += frame_seconds
            else:
                self.speech_run_seconds = max(0.0, self.speech_run_seconds - (frame_seconds * 0.7))
            if self.speech_run_seconds >= self.config.min_start_seconds:
                self.in_speech = True
                self.speech_seconds = 0.0
                self.silence_seconds = 0.0
                self.utterance_frames = []
                self.utterance_rms_sum = 0.0
                self.utterance_rms_count = 0
                self._consume_pre_roll_into_utterance()
                self.speech_run_seconds = 0.0
            return None

        self.utterance_frames.append(frame)
        self.utterance_rms_sum += frame_rms
        self.utterance_rms_count += 1
        self.speech_seconds += frame_seconds

        if frame_rms >= self._dynamic_end_threshold():
            self.silence_seconds = 0.0
        else:
            self.silence_seconds += frame_seconds

        if self.speech_seconds >= self.config.max_utterance_seconds:
            return self._finalize("max_duration")

        required_silence = self.config.end_silence_seconds + self.config.hangover_seconds
        if (
            self.speech_seconds >= self.config.min_utterance_seconds
            and self.silence_seconds >= required_silence
        ):
            return self._finalize("silence")
        return None


def env_float(name: str, fallback: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Windows audio helper: capture mic + system audio and stream transcripts."
    )
    parser.add_argument("--session-id", default="default-room")
    parser.add_argument("--server", default="ws://127.0.0.1:8000")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--context", default="")
    parser.add_argument("--history-mode", default="focused", choices=["focused", "full", "stateless"])

    parser.add_argument("--stt-provider", default=os.getenv("STT_PROVIDER", "openai"))
    parser.add_argument("--stt-model", default=os.getenv("STT_MODEL", "whisper-1"))
    parser.add_argument("--stt-language", default=os.getenv("STT_LANGUAGE", "en"))
    parser.add_argument("--openai-base-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))

    parser.add_argument("--chunk-seconds", type=float, default=env_float("AUDIO_CHUNK_SECONDS", 4.0))
    parser.add_argument("--min-rms", type=float, default=env_float("AUDIO_MIN_RMS", 220.0))
    parser.add_argument("--speech-start-rms", type=float, default=env_float("AUDIO_SPEECH_START_RMS", env_float("AUDIO_MIN_RMS", 220.0)))
    parser.add_argument("--speech-end-rms", type=float, default=env_float("AUDIO_SPEECH_END_RMS", 130.0))
    parser.add_argument("--end-silence-seconds", type=float, default=env_float("AUDIO_END_SILENCE_SECONDS", 0.55))
    parser.add_argument("--min-utterance-seconds", type=float, default=env_float("AUDIO_MIN_UTTERANCE_SECONDS", 0.7))
    parser.add_argument("--max-utterance-seconds", type=float, default=env_float("AUDIO_MAX_UTTERANCE_SECONDS", 14.0))
    parser.add_argument("--min-start-seconds", type=float, default=env_float("AUDIO_MIN_START_SECONDS", 0.12))
    parser.add_argument("--hangover-seconds", type=float, default=env_float("AUDIO_HANGOVER_SECONDS", 0.25))
    parser.add_argument("--pre-roll-seconds", type=float, default=env_float("AUDIO_PRE_ROLL_SECONDS", 0.35))
    parser.add_argument("--cooldown-seconds", type=float, default=env_float("AUDIO_COOLDOWN_SECONDS", 0.25))
    parser.add_argument("--min-emit-interval-seconds", type=float, default=env_float("AUDIO_MIN_EMIT_INTERVAL_SECONDS", 0.25))
    parser.add_argument(
        "--adaptive-noise",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("AUDIO_ADAPTIVE_NOISE", "1").strip() not in {"0", "false", "False"},
    )
    parser.add_argument("--noise-floor-alpha", type=float, default=env_float("AUDIO_NOISE_FLOOR_ALPHA", 0.965))
    parser.add_argument("--start-rms-ratio", type=float, default=env_float("AUDIO_START_RMS_RATIO", 2.4))
    parser.add_argument("--end-rms-ratio", type=float, default=env_float("AUDIO_END_RMS_RATIO", 1.45))
    parser.add_argument("--queue-size", type=int, default=env_int("AUDIO_QUEUE_SIZE", 480))
    parser.add_argument("--transcribe-queue-size", type=int, default=env_int("AUDIO_TRANSCRIBE_QUEUE_SIZE", 32))

    parser.add_argument("--mic-device", default=os.getenv("MIC_DEVICE", ""))
    parser.add_argument("--system-device", default=os.getenv("SYSTEM_DEVICE", ""))
    parser.add_argument("--mic-sample-rate", type=int, default=0)
    parser.add_argument("--system-sample-rate", type=int, default=0)
    parser.add_argument("--mic-channels", type=int, default=1)
    parser.add_argument("--system-channels", type=int, default=2)

    parser.add_argument("--disable-mic", action="store_true")
    parser.add_argument("--disable-system", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    return parser.parse_args()


def hostapi_name(hostapi_index: int) -> str:
    hostapis = sd.query_hostapis()
    if 0 <= hostapi_index < len(hostapis):
        return str(hostapis[hostapi_index].get("name", "unknown"))
    return "unknown"


def list_devices() -> None:
    devices = sd.query_devices()
    print("Audio devices:")
    for idx, info in enumerate(devices):
        hapi = hostapi_name(int(info["hostapi"]))
        print(
            f"[{idx}] {info['name']} | hostapi={hapi} "
            f"| in={int(info['max_input_channels'])} out={int(info['max_output_channels'])} "
            f"| default_sr={int(info['default_samplerate'])}"
        )
    default_in, default_out = sd.default.device
    print(f"\nDefault input={default_in}, default output={default_out}")
    stereo_mix_idx = find_stereo_mix_input()
    if stereo_mix_idx is not None:
        print(f"Suggested system input device (Stereo Mix-like): {stereo_mix_idx}")
    else:
        print("No Stereo Mix-like input device detected.")


def parse_device_selector(selector: str) -> str | int | None:
    clean = (selector or "").strip()
    if not clean:
        return None
    if clean.isdigit() or (clean.startswith("-") and clean[1:].isdigit()):
        return int(clean)
    return clean


def resolve_input_device(selector: str) -> int:
    parsed = parse_device_selector(selector)
    devices = sd.query_devices()
    if isinstance(parsed, int):
        info = devices[parsed]
        if int(info["max_input_channels"]) < 1:
            raise RuntimeError(f"Selected input device index {parsed} has no input channels.")
        return parsed
    if isinstance(parsed, str):
        needle = parsed.lower()
        for idx, info in enumerate(devices):
            if int(info["max_input_channels"]) >= 1 and needle in str(info["name"]).lower():
                return idx
        raise RuntimeError(f"No input device matched '{selector}'. Use --list-devices.")
    default_in = int(sd.default.device[0])
    if default_in >= 0 and int(devices[default_in]["max_input_channels"]) >= 1:
        return default_in
    for idx, info in enumerate(devices):
        if int(info["max_input_channels"]) >= 1:
            return idx
    raise RuntimeError("No input device available.")


def resolve_output_device(selector: str) -> int:
    parsed = parse_device_selector(selector)
    devices = sd.query_devices()
    if isinstance(parsed, int):
        info = devices[parsed]
        if int(info["max_output_channels"]) < 1:
            raise RuntimeError(f"Selected output device index {parsed} has no output channels.")
        return parsed
    if isinstance(parsed, str):
        needle = parsed.lower()
        for idx, info in enumerate(devices):
            if int(info["max_output_channels"]) >= 1 and needle in str(info["name"]).lower():
                return idx
        raise RuntimeError(f"No output device matched '{selector}'. Use --list-devices.")
    default_out = int(sd.default.device[1])
    if default_out >= 0 and int(devices[default_out]["max_output_channels"]) >= 1:
        return default_out
    for idx, info in enumerate(devices):
        if int(info["max_output_channels"]) >= 1:
            return idx
    raise RuntimeError("No output device available for loopback capture.")


def resolve_system_device(selector: str) -> tuple[int, bool]:
    parsed = parse_device_selector(selector)
    devices = sd.query_devices()
    if isinstance(parsed, int):
        info = devices[parsed]
        if int(info["max_input_channels"]) >= 1:
            return parsed, False
        if int(info["max_output_channels"]) >= 1:
            return parsed, True
        raise RuntimeError(f"Selected system device index {parsed} has no usable channels.")
    if isinstance(parsed, str):
        needle = parsed.lower()
        for idx, info in enumerate(devices):
            name = str(info["name"]).lower()
            if needle not in name:
                continue
            if int(info["max_input_channels"]) >= 1:
                return idx, False
            if int(info["max_output_channels"]) >= 1:
                return idx, True
        raise RuntimeError(f"No system device matched '{selector}'. Use --list-devices.")
    raise RuntimeError("No system device selector provided.")


def find_stereo_mix_input() -> int | None:
    devices = sd.query_devices()
    keywords = ("stereo mix", "what u hear", "wave out", "loopback")
    for idx, info in enumerate(devices):
        if int(info["max_input_channels"]) < 1:
            continue
        name = str(info["name"]).lower()
        if any(keyword in name for keyword in keywords):
            return idx
    return None


def make_wasapi_settings_for_loopback() -> Any:
    try:
        return sd.WasapiSettings(loopback=True, auto_convert=True)
    except TypeError:
        return sd.WasapiSettings(auto_convert=True)


def supports_wasapi_output_loopback(device_index: int, sample_rate: int, channels: int) -> bool:
    try:
        extra = make_wasapi_settings_for_loopback()
        stream = sd.InputStream(
            device=device_index,
            channels=channels,
            samplerate=sample_rate,
            dtype="int16",
            latency="low",
            extra_settings=extra,
        )
        stream.close()
        return True
    except Exception:
        return False


def unique_preserve_order(values: list[int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def negotiate_input_params(
    device_index: int,
    preferred_sample_rate: int,
    preferred_channels: int,
    extra_settings: Any | None = None,
) -> tuple[int, int]:
    info = sd.query_devices(device_index)
    max_in = int(info["max_input_channels"])
    if max_in < 1:
        raise RuntimeError(f"Device {device_index} has no input channels.")
    default_sr = int(info["default_samplerate"])

    channel_candidates = unique_preserve_order(
        [preferred_channels, 1, 2, max_in]
    )
    channel_candidates = [ch for ch in channel_candidates if 1 <= ch <= max_in]
    if not channel_candidates:
        channel_candidates = [max_in]

    sample_rate_candidates = unique_preserve_order(
        [preferred_sample_rate, default_sr, 48000, 44100, 16000]
    )
    sample_rate_candidates = [sr for sr in sample_rate_candidates if sr > 0]

    last_error: Exception | None = None
    for sample_rate in sample_rate_candidates:
        for channels in channel_candidates:
            try:
                sd.check_input_settings(
                    device=device_index,
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="int16",
                    extra_settings=extra_settings,
                )
                return sample_rate, channels
            except Exception as exc:
                last_error = exc
                continue
    raise RuntimeError(
        f"Could not find valid input format for device {device_index}. Last error: {last_error}"
    )


def build_stream_configs(args: argparse.Namespace) -> list[StreamConfig]:
    configs: list[StreamConfig] = []
    devices = sd.query_devices()

    if not args.disable_mic:
        mic_idx = resolve_input_device(args.mic_device)
        mic_info = devices[mic_idx]
        mic_pref_channels = max(1, int(args.mic_channels))
        mic_pref_sr = int(args.mic_sample_rate) if int(args.mic_sample_rate) > 0 else int(mic_info["default_samplerate"])
        mic_sr, mic_channels = negotiate_input_params(
            device_index=mic_idx,
            preferred_sample_rate=mic_pref_sr,
            preferred_channels=mic_pref_channels,
            extra_settings=None,
        )
        if mic_sr != mic_pref_sr or mic_channels != mic_pref_channels:
            print(
                f"[capture] adjusted mic format for device {mic_idx}: "
                f"requested sr={mic_pref_sr}, ch={mic_pref_channels} -> "
                f"using sr={mic_sr}, ch={mic_channels}"
            )
        configs.append(
            StreamConfig(
                source="mic",
                device_index=mic_idx,
                sample_rate=mic_sr,
                channels=mic_channels,
                loopback=False,
            )
        )

    if not args.disable_system:
        user_selected_system = parse_device_selector(args.system_device) is not None
        if user_selected_system:
            system_idx, system_is_output = resolve_system_device(args.system_device)
            system_info = devices[system_idx]
            if not system_is_output:
                sys_pref_channels = max(1, int(args.system_channels))
                sys_pref_sr = (
                    int(args.system_sample_rate)
                    if int(args.system_sample_rate) > 0
                    else int(system_info["default_samplerate"])
                )
                sys_sr, sys_channels = negotiate_input_params(
                    device_index=system_idx,
                    preferred_sample_rate=sys_pref_sr,
                    preferred_channels=sys_pref_channels,
                    extra_settings=None,
                )
                if sys_sr != sys_pref_sr or sys_channels != sys_pref_channels:
                    print(
                        f"[capture] adjusted system input format for device {system_idx}: "
                        f"requested sr={sys_pref_sr}, ch={sys_pref_channels} -> "
                        f"using sr={sys_sr}, ch={sys_channels}"
                    )
                configs.append(
                    StreamConfig(
                        source="system",
                        device_index=system_idx,
                        sample_rate=sys_sr,
                        channels=sys_channels,
                        loopback=False,
                    )
                )
            else:
                out_hapi = hostapi_name(int(system_info["hostapi"]))
                if "wasapi" not in out_hapi.lower():
                    raise RuntimeError(
                        f"System output device must use WASAPI for loopback. Selected hostapi={out_hapi}."
                    )
                out_channels = max(1, min(int(args.system_channels), int(system_info["max_output_channels"])))
                out_sr = (
                    int(args.system_sample_rate)
                    if int(args.system_sample_rate) > 0
                    else int(system_info["default_samplerate"])
                )
                if not supports_wasapi_output_loopback(system_idx, out_sr, out_channels):
                    raise RuntimeError(
                        "WASAPI output loopback is not available in this sounddevice/PortAudio build. "
                        "Use a system input device like 'Stereo Mix' (for example --system-device 22)."
                    )
                configs.append(
                    StreamConfig(
                        source="system",
                        device_index=system_idx,
                        sample_rate=out_sr,
                        channels=out_channels,
                        loopback=True,
                    )
                )
        else:
            stereo_mix_idx = find_stereo_mix_input()
            if stereo_mix_idx is not None:
                info = devices[stereo_mix_idx]
                sys_pref_channels = max(1, int(args.system_channels))
                sys_pref_sr = (
                    int(args.system_sample_rate)
                    if int(args.system_sample_rate) > 0
                    else int(info["default_samplerate"])
                )
                sys_sr, sys_channels = negotiate_input_params(
                    device_index=stereo_mix_idx,
                    preferred_sample_rate=sys_pref_sr,
                    preferred_channels=sys_pref_channels,
                    extra_settings=None,
                )
                print(
                    f"[capture] auto-selected system input device {stereo_mix_idx} "
                    f"('{info['name']}') for system audio."
                )
                configs.append(
                    StreamConfig(
                        source="system",
                        device_index=stereo_mix_idx,
                        sample_rate=sys_sr,
                        channels=sys_channels,
                        loopback=False,
                    )
                )
            else:
                out_idx = resolve_output_device("")
                out_info = devices[out_idx]
                out_hapi = hostapi_name(int(out_info["hostapi"]))
                out_channels = max(1, min(int(args.system_channels), int(out_info["max_output_channels"])))
                out_sr = (
                    int(args.system_sample_rate)
                    if int(args.system_sample_rate) > 0
                    else int(out_info["default_samplerate"])
                )
                if "wasapi" in out_hapi.lower() and supports_wasapi_output_loopback(
                    out_idx, out_sr, out_channels
                ):
                    print(
                        f"[capture] auto-selected WASAPI output device {out_idx} "
                        f"('{out_info['name']}') for loopback."
                    )
                    configs.append(
                        StreamConfig(
                            source="system",
                            device_index=out_idx,
                            sample_rate=out_sr,
                            channels=out_channels,
                            loopback=True,
                        )
                    )
                else:
                    raise RuntimeError(
                        "Could not auto-configure system audio capture. "
                        "Please run --list-devices and pass a system input device "
                        "(usually 'Stereo Mix') with --system-device."
                    )

    if not configs:
        raise RuntimeError("Both mic and system capture are disabled.")
    return configs


def mono_int16(data: np.ndarray) -> np.ndarray:
    if data.ndim == 1:
        return data.astype(np.int16, copy=False)
    if data.shape[1] == 1:
        return data[:, 0].astype(np.int16, copy=False)
    mixed = data.astype(np.float32).mean(axis=1)
    return mixed.astype(np.int16)


def compute_rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


async def receiver(ws: Any) -> None:
    async for message in ws:
        data: dict[str, Any] = json.loads(message)
        msg_type = data.get("type")
        if msg_type == "suggestion":
            print(
                f"\n[{data.get('provider')}/{data.get('model')}] "
                f"{data.get('latency_ms')}ms\n{data.get('text')}\n"
            )
        elif msg_type == "status":
            print(f"[status] {data.get('message')}")
        elif msg_type == "error":
            print(f"[error] {data.get('message')}")


async def detect_source_utterances(
    source: str,
    sample_rate: int,
    frame_queue: asyncio.Queue[np.ndarray],
    utterance_queue: asyncio.Queue[UtteranceChunk],
    vad_config: VadConfig,
) -> None:
    detector = AdaptiveEndpointDetector(
        source=source,
        sample_rate=sample_rate,
        config=vad_config,
    )
    dropped_utterances = 0

    while True:
        frame = await frame_queue.get()
        mono = mono_int16(frame)
        if mono.size == 0:
            continue

        utterance = detector.feed(mono)
        if utterance is None:
            continue
        if utterance.duration_seconds < max(0.20, vad_config.min_utterance_seconds * 0.5):
            continue
        if utterance.avg_rms < max(20.0, vad_config.speech_end_rms * 0.45):
            continue

        if utterance_queue.full():
            try:
                utterance_queue.get_nowait()
                dropped_utterances += 1
            except asyncio.QueueEmpty:
                pass
        try:
            utterance_queue.put_nowait(utterance)
        except asyncio.QueueFull:
            dropped_utterances += 1
            continue
        if dropped_utterances and dropped_utterances % 5 == 0:
            print(f"[vad:{source}] dropped queued utterances={dropped_utterances}")


async def transcribe_and_send_source(
    source: str,
    utterance_queue: asyncio.Queue[UtteranceChunk],
    ws: Any,
    send_lock: asyncio.Lock,
    transcriber: Transcriber,
    min_emit_interval_seconds: float,
    cooldown_seconds: float,
) -> None:
    last_emit_at = 0.0
    recent_texts: deque[tuple[str, float]] = deque(maxlen=8)

    def normalize_text(text: str) -> str:
        return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())

    def recently_seen(text: str, now: float) -> bool:
        for prev_text, prev_at in recent_texts:
            if prev_text == text and (now - prev_at) < 16.0:
                return True
        return False

    while True:
        utterance = await utterance_queue.get()
        now = perf_counter()
        since_emit = now - last_emit_at
        if since_emit < min_emit_interval_seconds:
            await asyncio.sleep(min_emit_interval_seconds - since_emit)

        stt_start = perf_counter()
        try:
            text = (await transcriber.transcribe_pcm16(utterance.samples, utterance.sample_rate)).strip()
        except Exception as exc:
            print(f"[transcribe-error:{source}] {exc}")
            continue
        stt_ms = int((perf_counter() - stt_start) * 1000)
        if not text:
            continue

        normalized = normalize_text(text)
        if not normalized:
            continue
        now = perf_counter()
        if recently_seen(normalized, now):
            continue

        recent_texts.append((normalized, now))
        last_emit_at = now
        print(
            f"[{source}] {text}\n"
            f"[transcribe:{source}] {stt_ms}ms dur={utterance.duration_seconds:.2f}s "
            f"reason={utterance.reason} avg_rms={utterance.avg_rms:.1f}"
        )
        async with send_lock:
            await ws.send(
                json.dumps(
                    {
                        "type": "transcript",
                        "source": source,
                        "text": text,
                        "final": True,
                    }
                )
            )
        if cooldown_seconds > 0:
            await asyncio.sleep(cooldown_seconds)


def enqueue_audio(
    source: str,
    queue: asyncio.Queue[np.ndarray],
    data: np.ndarray,
    drop_counters: dict[str, int],
) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        drop_counters[source] = int(drop_counters.get(source, 0)) + 1
        dropped = drop_counters[source]
        if dropped % 50 == 0:
            print(f"[audio:{source}] dropped_frames={dropped} (capture is falling behind)")
    queue.put_nowait(data)


def make_callback(
    source: str,
    queue: asyncio.Queue[np.ndarray],
    loop: asyncio.AbstractEventLoop,
    drop_counters: dict[str, int],
) -> Any:
    def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        _ = frames
        _ = time_info
        if status:
            print(f"[audio:{source}] {status}")
        payload = np.copy(indata)
        loop.call_soon_threadsafe(enqueue_audio, source, queue, payload, drop_counters)

    return callback


def print_stream_selection(configs: list[StreamConfig]) -> None:
    devices = sd.query_devices()
    for cfg in configs:
        info = devices[cfg.device_index]
        mode = "loopback" if cfg.loopback else "input"
        print(
            f"[capture] {cfg.source} -> device={cfg.device_index} '{info['name']}' "
            f"mode={mode} sr={cfg.sample_rate} ch={cfg.channels}"
        )


def should_reconnect_ws(exc: Exception) -> tuple[bool, str]:
    if isinstance(exc, ConnectionClosed):
        code = int(getattr(exc, "code", 0) or 0)
        reason = str(getattr(exc, "reason", "") or "")
        if code == 1001 and "session deleted" in reason.lower():
            return False, f"session deleted by server (code={code})"
        return True, f"websocket closed code={code} reason={reason or 'n/a'}"
    if isinstance(exc, (OSError, TimeoutError, InvalidStatus)):
        return True, f"websocket transport error: {exc}"
    return False, str(exc)


async def run_capture(args: argparse.Namespace) -> None:
    stt = make_transcriber(
        provider=args.stt_provider,
        api_key=os.getenv("OPENAI_API_KEY", ""),
        model=args.stt_model,
        base_url=args.openai_base_url,
        language=args.stt_language,
    )

    configs = build_stream_configs(args)
    if args.stt_provider.strip().lower() == "openai" and not os.getenv("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY is not set. Add it to .env before running audio capture.")
    vad_config = VadConfig(
        speech_start_rms=float(args.speech_start_rms),
        speech_end_rms=float(args.speech_end_rms),
        end_silence_seconds=float(args.end_silence_seconds),
        min_utterance_seconds=float(args.min_utterance_seconds),
        max_utterance_seconds=float(args.max_utterance_seconds),
        min_start_seconds=float(args.min_start_seconds),
        hangover_seconds=float(args.hangover_seconds),
        pre_roll_seconds=float(args.pre_roll_seconds),
        cooldown_seconds=float(args.cooldown_seconds),
        adaptive_noise=bool(args.adaptive_noise),
        noise_floor_alpha=float(args.noise_floor_alpha),
        start_rms_ratio=float(args.start_rms_ratio),
        end_rms_ratio=float(args.end_rms_ratio),
    )
    print_stream_selection(configs)
    print(
        "[capture] endpointing: "
        f"start_rms={vad_config.speech_start_rms:.1f}, "
        f"end_rms={vad_config.speech_end_rms:.1f}, "
        f"start_min={vad_config.min_start_seconds:.2f}s, "
        f"end_silence={vad_config.end_silence_seconds:.2f}s, "
        f"hangover={vad_config.hangover_seconds:.2f}s, "
        f"pre_roll={vad_config.pre_roll_seconds:.2f}s, "
        f"min_utt={vad_config.min_utterance_seconds:.2f}s, "
        f"max_utt={vad_config.max_utterance_seconds:.2f}s, "
        f"adaptive={vad_config.adaptive_noise}, "
        f"ratio(start/end)=({vad_config.start_rms_ratio:.2f}/{vad_config.end_rms_ratio:.2f})"
    )
    print(
        "[capture] buffers: "
        f"frame_queue={max(20, int(args.queue_size))}, "
        f"utterance_queue={max(4, int(args.transcribe_queue_size))}, "
        f"min_emit_interval={float(args.min_emit_interval_seconds):.2f}s, "
        f"cooldown={vad_config.cooldown_seconds:.2f}s"
    )
    url = f"{args.server.rstrip('/')}/ws/{args.session_id}"
    reconnect_attempt = 0
    reconnect_delay = 1.0
    max_reconnect_delay = 12.0

    try:
        while True:
            send_lock = asyncio.Lock()
            frame_queues: dict[str, asyncio.Queue[np.ndarray]] = {
                cfg.source: asyncio.Queue(maxsize=max(20, int(args.queue_size))) for cfg in configs
            }
            utterance_queues: dict[str, asyncio.Queue[UtteranceChunk]] = {
                cfg.source: asyncio.Queue(maxsize=max(4, int(args.transcribe_queue_size))) for cfg in configs
            }
            dropped_frame_counters: dict[str, int] = {cfg.source: 0 for cfg in configs}
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    reconnect_attempt = 0
                    reconnect_delay = 1.0
                    async with send_lock:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "configure",
                                    "client_role": "capture",
                                    "provider": args.provider,
                                    "model": args.model,
                                    "context": args.context,
                                    "history_mode": args.history_mode,
                                }
                            )
                        )

                    loop = asyncio.get_running_loop()
                    with ExitStack() as stack:
                        for cfg in configs:
                            extra = make_wasapi_settings_for_loopback() if cfg.loopback else None
                            blocksize = max(256, int(cfg.sample_rate * 0.02))
                            stream = sd.InputStream(
                                device=cfg.device_index,
                                channels=cfg.channels,
                                samplerate=cfg.sample_rate,
                                dtype="int16",
                                latency="low",
                                blocksize=blocksize,
                                callback=make_callback(
                                    cfg.source,
                                    frame_queues[cfg.source],
                                    loop,
                                    dropped_frame_counters,
                                ),
                                extra_settings=extra,
                            )
                            stack.enter_context(stream)
                        print("[capture] streams started")

                        tasks = [asyncio.create_task(receiver(ws))]
                        for cfg in configs:
                            tasks.append(
                                asyncio.create_task(
                                    detect_source_utterances(
                                        source=cfg.source,
                                        sample_rate=cfg.sample_rate,
                                        frame_queue=frame_queues[cfg.source],
                                        utterance_queue=utterance_queues[cfg.source],
                                        vad_config=vad_config,
                                    )
                                )
                            )
                            tasks.append(
                                asyncio.create_task(
                                    transcribe_and_send_source(
                                        source=cfg.source,
                                        utterance_queue=utterance_queues[cfg.source],
                                        ws=ws,
                                        send_lock=send_lock,
                                        transcriber=stt,
                                        min_emit_interval_seconds=float(args.min_emit_interval_seconds),
                                        cooldown_seconds=float(args.cooldown_seconds),
                                    )
                                )
                            )
                        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in pending:
                            task.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        task_errors: list[Exception] = []
                        for task in done:
                            exc = task.exception()
                            if exc is not None:
                                task_errors.append(exc)
                        if not task_errors:
                            print("[capture] websocket closed; reconnecting...")
                            await asyncio.sleep(1.0)
                            continue
                        reconnectable, message = should_reconnect_ws(task_errors[0])
                        if reconnectable:
                            reconnect_attempt += 1
                            print(
                                f"[capture] reconnect attempt {reconnect_attempt} in "
                                f"{reconnect_delay:.1f}s ({message})"
                            )
                            await asyncio.sleep(reconnect_delay)
                            reconnect_delay = min(max_reconnect_delay, reconnect_delay * 1.8)
                            continue
                        raise task_errors[0]
            except Exception as exc:
                reconnectable, message = should_reconnect_ws(exc)
                if reconnectable:
                    reconnect_attempt += 1
                    print(
                        f"[capture] reconnect attempt {reconnect_attempt} in "
                        f"{reconnect_delay:.1f}s ({message})"
                    )
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(max_reconnect_delay, reconnect_delay * 1.8)
                    continue
                raise
    finally:
        await stt.aclose()


def main() -> None:
    args = build_args()
    if args.list_devices:
        list_devices()
        return
    if os.name != "nt":
        raise RuntimeError("This helper currently targets Windows only.")
    try:
        asyncio.run(run_capture(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
