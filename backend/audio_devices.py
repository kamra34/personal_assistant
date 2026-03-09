from __future__ import annotations

from typing import Any


def list_audio_devices() -> dict[str, Any]:
    try:
        import sounddevice as sd
    except Exception as exc:
        return {
            "available": False,
            "error": f"sounddevice import failed: {exc}",
            "devices": [],
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
            "devices": [],
            "suggested": {"mic_device": "", "system_device": ""},
        }

    items: list[dict[str, Any]] = []
    suggested_system = ""
    for idx, info in enumerate(devices):
        hostapi_index = int(info.get("hostapi", -1))
        hostapi_name = "unknown"
        if 0 <= hostapi_index < len(hostapis):
            hostapi_name = str(hostapis[hostapi_index].get("name", "unknown"))

        name = str(info.get("name", ""))
        is_stereo_mix_like = any(
            token in name.lower()
            for token in ("stereo mix", "what u hear", "wave out", "loopback")
        )
        if suggested_system == "" and int(info.get("max_input_channels", 0)) > 0 and is_stereo_mix_like:
            suggested_system = str(idx)

        items.append(
            {
                "id": str(idx),
                "name": name,
                "hostapi": hostapi_name,
                "max_input_channels": int(info.get("max_input_channels", 0)),
                "max_output_channels": int(info.get("max_output_channels", 0)),
                "default_sample_rate": int(info.get("default_samplerate", 0)),
                "is_default_input": idx == int(default_in),
                "is_default_output": idx == int(default_out),
                "is_stereo_mix_like": is_stereo_mix_like,
            }
        )

    suggested_mic = ""
    if int(default_in) >= 0:
        for item in items:
            if int(item["id"]) == int(default_in) and item["max_input_channels"] > 0:
                suggested_mic = item["id"]
                break
    if suggested_mic == "":
        for item in items:
            if item["max_input_channels"] > 0:
                suggested_mic = item["id"]
                break

    return {
        "available": True,
        "error": "",
        "devices": items,
        "suggested": {
            "mic_device": suggested_mic,
            "system_device": suggested_system,
        },
    }

