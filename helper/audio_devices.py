from __future__ import annotations

import re
from typing import Any


def hostapi_rank(name: str) -> int:
    lower = name.lower()
    if "windows wasapi" in lower:
        return 0
    if "windows wdm-ks" in lower:
        return 1
    if "windows directsound" in lower:
        return 2
    if "mme" in lower:
        return 3
    return 9


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def is_stereo_mix_like(name: str) -> bool:
    lower = name.lower()
    return any(token in lower for token in ("stereo mix", "what u hear", "wave out", "loopback"))


def is_likely_microphone(name: str) -> bool:
    lower = name.lower()
    if is_stereo_mix_like(lower):
        return False
    mic_tokens = ("microphone", "mic", "array", "capture")
    return any(token in lower for token in mic_tokens)


def load_devices() -> dict[str, Any]:
    try:
        import sounddevice as sd
    except Exception as exc:
        return {
            "available": False,
            "error": f"sounddevice import failed: {exc}",
            "mic_devices": [],
            "system_devices": [],
            "all_devices": [],
            "suggested": {"mic_device": "", "system_device": ""},
        }

    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        default_in, default_out = sd.default.device
    except Exception as exc:
        return {
            "available": False,
            "error": f"audio query failed: {exc}",
            "mic_devices": [],
            "system_devices": [],
            "all_devices": [],
            "suggested": {"mic_device": "", "system_device": ""},
        }

    all_items: list[dict[str, Any]] = []
    for idx, info in enumerate(devices):
        hapi_idx = int(info.get("hostapi", -1))
        hostapi = "unknown"
        if 0 <= hapi_idx < len(hostapis):
            hostapi = str(hostapis[hapi_idx].get("name", "unknown"))
        name = str(info.get("name", ""))
        all_items.append(
            {
                "id": str(idx),
                "name": name,
                "hostapi": hostapi,
                "max_input_channels": int(info.get("max_input_channels", 0)),
                "max_output_channels": int(info.get("max_output_channels", 0)),
                "default_sample_rate": int(info.get("default_samplerate", 0)),
                "is_default_input": idx == int(default_in),
                "is_default_output": idx == int(default_out),
                "is_stereo_mix_like": is_stereo_mix_like(name),
            }
        )

    input_items = [item for item in all_items if item["max_input_channels"] > 0]
    dedup: dict[str, dict[str, Any]] = {}
    for item in input_items:
        key = normalize_name(item["name"])
        current = dedup.get(key)
        if current is None:
            dedup[key] = item
            continue
        if hostapi_rank(item["hostapi"]) < hostapi_rank(current["hostapi"]):
            dedup[key] = item
            continue
        if (
            hostapi_rank(item["hostapi"]) == hostapi_rank(current["hostapi"])
            and item["max_input_channels"] > current["max_input_channels"]
        ):
            dedup[key] = item

    preferred_inputs = list(dedup.values())
    preferred_inputs.sort(key=lambda x: (hostapi_rank(x["hostapi"]), x["name"].lower()))

    system_devices = [item for item in preferred_inputs if item["is_stereo_mix_like"]]
    if not system_devices:
        system_devices = [item for item in preferred_inputs if not is_likely_microphone(item["name"])]
    if not system_devices:
        system_devices = preferred_inputs

    mic_devices = [item for item in preferred_inputs if is_likely_microphone(item["name"])]
    if not mic_devices:
        mic_devices = preferred_inputs

    suggested_mic = ""
    for item in mic_devices:
        if item["is_default_input"]:
            suggested_mic = item["id"]
            break
    if not suggested_mic and mic_devices:
        suggested_mic = mic_devices[0]["id"]

    suggested_system = ""
    for item in system_devices:
        if item["is_stereo_mix_like"]:
            suggested_system = item["id"]
            break
    if not suggested_system and system_devices:
        suggested_system = system_devices[0]["id"]

    return {
        "available": True,
        "error": "",
        "mic_devices": mic_devices,
        "system_devices": system_devices,
        "all_devices": all_items,
        "suggested": {"mic_device": suggested_mic, "system_device": suggested_system},
    }
