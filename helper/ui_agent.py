from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from helper.audio_devices import load_devices

BASE_DIR = Path(__file__).resolve().parents[1]

app = FastAPI(title="Meeting Assistant Helper Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class StartCaptureIn(BaseModel):
    session_id: str
    server: str
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    history_mode: Literal["focused", "full", "stateless"] = "focused"
    context: str = ""
    mic_device: str = ""
    system_device: str = ""
    disable_mic: bool = False
    disable_system: bool = False


class StopCaptureOut(BaseModel):
    stopped: bool
    message: str


@dataclass(slots=True)
class CaptureState:
    process: subprocess.Popen[str] | None = None
    started_at: datetime | None = None
    command: list[str] | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=300))
    reader_thread: threading.Thread | None = None


capture_state = CaptureState()
state_lock = threading.Lock()


def _reader_worker(process: subprocess.Popen[str], logs: deque[str]) -> None:
    stream = process.stdout
    if stream is None:
        return
    for line in iter(stream.readline, ""):
        clean = line.rstrip("\n")
        if clean:
            logs.append(clean)
    stream.close()


def _is_running(proc: subprocess.Popen[str] | None) -> bool:
    return proc is not None and proc.poll() is None


def _build_capture_command(payload: StartCaptureIn) -> list[str]:
    script = BASE_DIR / "helper" / "audio_capture_windows.py"
    cmd = [
        sys.executable,
        str(script),
        "--session-id",
        payload.session_id,
        "--server",
        payload.server,
        "--provider",
        payload.provider,
        "--model",
        payload.model,
        "--history-mode",
        payload.history_mode,
    ]
    if payload.context.strip():
        cmd.extend(["--context", payload.context.strip()])
    if payload.mic_device.strip():
        cmd.extend(["--mic-device", payload.mic_device.strip()])
    if payload.system_device.strip():
        cmd.extend(["--system-device", payload.system_device.strip()])
    if payload.disable_mic:
        cmd.append("--disable-mic")
    if payload.disable_system:
        cmd.append("--disable-system")
    return cmd


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/api/devices")
def api_devices() -> dict[str, Any]:
    return load_devices()


@app.get("/api/capture/status")
def api_capture_status() -> dict[str, Any]:
    with state_lock:
        running = _is_running(capture_state.process)
        return {
            "running": running,
            "pid": capture_state.process.pid if running and capture_state.process else None,
            "started_at": capture_state.started_at.isoformat() if capture_state.started_at else None,
            "command": capture_state.command or [],
            "logs": list(capture_state.logs),
            "exit_code": capture_state.process.poll() if capture_state.process else None,
        }


@app.post("/api/capture/start")
def api_capture_start(payload: StartCaptureIn) -> dict[str, Any]:
    with state_lock:
        if _is_running(capture_state.process):
            raise HTTPException(status_code=409, detail="Capture is already running.")

        cmd = _build_capture_command(payload)
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        capture_state.process = proc
        capture_state.started_at = datetime.now(UTC)
        capture_state.command = cmd
        capture_state.logs.clear()
        capture_state.logs.append(f"[agent] started pid={proc.pid}")

        thread = threading.Thread(
            target=_reader_worker,
            args=(proc, capture_state.logs),
            daemon=True,
        )
        capture_state.reader_thread = thread
        thread.start()

        return {"started": True, "pid": proc.pid, "command": cmd}


@app.post("/api/capture/stop", response_model=StopCaptureOut)
def api_capture_stop() -> StopCaptureOut:
    with state_lock:
        proc = capture_state.process
        if not _is_running(proc):
            return StopCaptureOut(stopped=False, message="Capture is not running.")
        assert proc is not None
        proc.terminate()
    try:
        proc.wait(timeout=6)
    except subprocess.TimeoutExpired:
        proc.kill()
    return StopCaptureOut(stopped=True, message="Capture process stopped.")


def run() -> None:
    import uvicorn

    host = os.getenv("HELPER_AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("HELPER_AGENT_PORT", "8765"))
    uvicorn.run(
        "helper.ui_agent:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    run()
