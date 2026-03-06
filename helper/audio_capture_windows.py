from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import ExitStack
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import sounddevice as sd
import websockets
from dotenv import load_dotenv

try:
    from helper.transcribe_stream import Transcriber, make_transcriber
except ModuleNotFoundError:  # Support direct script execution from helper/ path.
    from transcribe_stream import Transcriber, make_transcriber

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


@dataclass(slots=True)
class StreamConfig:
    source: str
    device_index: int
    sample_rate: int
    channels: int
    loopback: bool


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
    parser.add_argument("--end-silence-seconds", type=float, default=env_float("AUDIO_END_SILENCE_SECONDS", 0.9))
    parser.add_argument("--min-utterance-seconds", type=float, default=env_float("AUDIO_MIN_UTTERANCE_SECONDS", 0.8))
    parser.add_argument("--max-utterance-seconds", type=float, default=env_float("AUDIO_MAX_UTTERANCE_SECONDS", 10.0))
    parser.add_argument("--min-emit-interval-seconds", type=float, default=env_float("AUDIO_MIN_EMIT_INTERVAL_SECONDS", 1.0))
    parser.add_argument("--queue-size", type=int, default=env_int("AUDIO_QUEUE_SIZE", 120))

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


async def process_source(
    source: str,
    sample_rate: int,
    queue: asyncio.Queue[np.ndarray],
    ws: Any,
    send_lock: asyncio.Lock,
    transcriber: Transcriber,
    speech_start_rms: float,
    speech_end_rms: float,
    end_silence_seconds: float,
    min_utterance_seconds: float,
    max_utterance_seconds: float,
    min_emit_interval_seconds: float,
) -> None:
    in_speech = False
    silence_seconds = 0.0
    speech_seconds = 0.0
    utterance_parts: list[np.ndarray] = []
    last_emit_at = 0.0
    recent_texts: deque[str] = deque(maxlen=6)

    def normalize_text(text: str) -> str:
        return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())

    while True:
        frame = await queue.get()
        mono = mono_int16(frame)
        if mono.size == 0:
            continue
        frame_seconds = mono.size / float(sample_rate)
        frame_rms = compute_rms(mono)

        if not in_speech:
            if frame_rms >= speech_start_rms:
                in_speech = True
                silence_seconds = 0.0
                speech_seconds = frame_seconds
                utterance_parts = [mono]
            continue

        utterance_parts.append(mono)
        speech_seconds += frame_seconds
        if frame_rms < speech_end_rms:
            silence_seconds += frame_seconds
        else:
            silence_seconds = 0.0

        should_flush = False
        if speech_seconds >= max_utterance_seconds:
            should_flush = True
        elif silence_seconds >= end_silence_seconds and speech_seconds >= min_utterance_seconds:
            should_flush = True
        if not should_flush:
            continue

        utterance = np.concatenate(utterance_parts) if utterance_parts else np.empty((0,), dtype=np.int16)
        in_speech = False
        silence_seconds = 0.0
        speech_seconds = 0.0
        utterance_parts = []

        if utterance.size == 0:
            continue
        if compute_rms(utterance) < speech_end_rms:
            continue
        now = perf_counter()
        if (now - last_emit_at) < min_emit_interval_seconds:
            continue
        try:
            text = (await transcriber.transcribe_pcm16(utterance, sample_rate)).strip()
        except Exception as exc:
            print(f"[transcribe-error:{source}] {exc}")
            continue
        if not text:
            continue
        normalized = normalize_text(text)
        if not normalized:
            continue
        if normalized in recent_texts:
            continue
        recent_texts.append(normalized)
        last_emit_at = perf_counter()
        print(f"[{source}] {text}")
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


def enqueue_audio(queue: asyncio.Queue[np.ndarray], data: np.ndarray) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(data)


def make_callback(
    source: str,
    queue: asyncio.Queue[np.ndarray],
    loop: asyncio.AbstractEventLoop,
) -> Any:
    def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        _ = frames
        _ = time_info
        if status:
            print(f"[audio:{source}] {status}")
        payload = np.copy(indata)
        loop.call_soon_threadsafe(enqueue_audio, queue, payload)

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
    print_stream_selection(configs)
    print(
        "[capture] endpointing: "
        f"start_rms={float(args.speech_start_rms):.1f}, "
        f"end_rms={float(args.speech_end_rms):.1f}, "
        f"end_silence={float(args.end_silence_seconds):.2f}s, "
        f"min_utt={float(args.min_utterance_seconds):.2f}s, "
        f"max_utt={float(args.max_utterance_seconds):.2f}s"
    )
    url = f"{args.server.rstrip('/')}/ws/{args.session_id}"
    send_lock = asyncio.Lock()
    queues: dict[str, asyncio.Queue[np.ndarray]] = {
        cfg.source: asyncio.Queue(maxsize=max(20, int(args.queue_size))) for cfg in configs
    }

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            async with send_lock:
                await ws.send(
                    json.dumps(
                        {
                            "type": "configure",
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
                    stream = sd.InputStream(
                        device=cfg.device_index,
                        channels=cfg.channels,
                        samplerate=cfg.sample_rate,
                        dtype="int16",
                        latency="low",
                        callback=make_callback(cfg.source, queues[cfg.source], loop),
                        extra_settings=extra,
                    )
                    stack.enter_context(stream)
                print("[capture] streams started")

                tasks = [asyncio.create_task(receiver(ws))]
                for cfg in configs:
                    tasks.append(
                        asyncio.create_task(
                            process_source(
                                source=cfg.source,
                                sample_rate=cfg.sample_rate,
                                queue=queues[cfg.source],
                                ws=ws,
                                send_lock=send_lock,
                                transcriber=stt,
                                speech_start_rms=float(args.speech_start_rms),
                                speech_end_rms=float(args.speech_end_rms),
                                end_silence_seconds=float(args.end_silence_seconds),
                                min_utterance_seconds=float(args.min_utterance_seconds),
                                max_utterance_seconds=float(args.max_utterance_seconds),
                                min_emit_interval_seconds=float(args.min_emit_interval_seconds),
                            )
                        )
                    )
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    exc = task.exception()
                    if exc:
                        raise exc
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
