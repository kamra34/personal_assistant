from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, ttk, messagebox
from tkinter.scrolledtext import ScrolledText

from dotenv import load_dotenv

try:
    from helper.audio_devices import load_devices
except ModuleNotFoundError:
    from audio_devices import load_devices  # type: ignore


BASE_DIR = Path(__file__).resolve().parents[1]
WORK_DIR = Path.cwd() if getattr(sys, "frozen", False) else BASE_DIR


def default_settings_path() -> Path:
    if os.name == "nt":
        appdata = os.getenv("APPDATA", "").strip()
        if appdata:
            return Path(appdata) / "MeetingAssistant" / "desktop_agent.json"
    return Path.home() / ".meeting_assistant_desktop_agent.json"


@dataclass(slots=True)
class DesktopSettings:
    session_id: str = ""
    server: str = "ws://127.0.0.1:8000"
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    history_mode: str = "focused"
    dashboard_url: str = "http://localhost:3000"
    disable_mic: bool = False
    disable_system: bool = False
    mic_device_id: str = ""
    system_device_id: str = ""
    remember_api_key: bool = True
    openai_api_key: str = ""

    @classmethod
    def load(cls, path: Path) -> DesktopSettings:
        try:
            if not path.exists():
                return cls()
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return cls()
            return cls(
                session_id=str(payload.get("session_id", "")),
                server=str(payload.get("server", cls.server)),
                provider=str(payload.get("provider", cls.provider)),
                model=str(payload.get("model", cls.model)),
                history_mode=str(payload.get("history_mode", cls.history_mode)),
                dashboard_url=str(payload.get("dashboard_url", cls.dashboard_url)),
                disable_mic=bool(payload.get("disable_mic", False)),
                disable_system=bool(payload.get("disable_system", False)),
                mic_device_id=str(payload.get("mic_device_id", "")),
                system_device_id=str(payload.get("system_device_id", "")),
                remember_api_key=bool(payload.get("remember_api_key", True)),
                openai_api_key=str(payload.get("openai_api_key", "")),
            )
        except Exception:
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": self.session_id,
            "server": self.server,
            "provider": self.provider,
            "model": self.model,
            "history_mode": self.history_mode,
            "dashboard_url": self.dashboard_url,
            "disable_mic": self.disable_mic,
            "disable_system": self.disable_system,
            "mic_device_id": self.mic_device_id,
            "system_device_id": self.system_device_id,
            "remember_api_key": self.remember_api_key,
            "openai_api_key": self.openai_api_key if self.remember_api_key else "",
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def seed_environment_from_dotenv() -> list[Path]:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / ".env")
    else:
        candidates.append(BASE_DIR / ".env")
    candidates.append(Path.cwd() / ".env")

    loaded: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.exists():
            continue
        load_dotenv(resolved, override=False)
        loaded.append(resolved)
    return loaded


def run_capture_worker(argv: list[str]) -> None:
    try:
        from helper import audio_capture_windows as capture
    except ModuleNotFoundError:
        import audio_capture_windows as capture  # type: ignore

    original_argv = list(sys.argv)
    try:
        sys.argv = ["audio_capture_windows.py", *argv]
        try:
            capture.main()
        except Exception:
            print("[desktop-agent] capture worker failed:")
            print(traceback.format_exc())
            raise SystemExit(1)
    finally:
        sys.argv = original_argv


class DesktopAgentApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("Meeting Assistant Desktop Agent")
        self.root.geometry("860x670")
        self.root.minsize(760, 600)

        self.process: subprocess.Popen[str] | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.mic_map: dict[str, str] = {}
        self.system_map: dict[str, str] = {}
        self.loaded_env_files = seed_environment_from_dotenv()
        self.settings_path = default_settings_path()
        loaded_settings = DesktopSettings.load(self.settings_path)

        self.session_var = StringVar(value=loaded_settings.session_id)
        self.server_var = StringVar(
            value=loaded_settings.server or os.getenv("DESKTOP_AGENT_WS_SERVER", "ws://127.0.0.1:8000")
        )
        self.provider_var = StringVar(value=loaded_settings.provider or "openai")
        self.model_var = StringVar(value=loaded_settings.model or "gpt-4o-mini")
        self.history_mode_var = StringVar(value=loaded_settings.history_mode or "focused")
        self.mic_var = StringVar(value="auto")
        self.system_var = StringVar(value="auto")
        self.disable_mic_var = BooleanVar(value=loaded_settings.disable_mic)
        self.disable_system_var = BooleanVar(value=loaded_settings.disable_system)
        self.dashboard_url_var = StringVar(
            value=loaded_settings.dashboard_url
            or os.getenv("DESKTOP_AGENT_DASHBOARD_URL", "http://localhost:3000")
        )
        env_key = os.getenv("OPENAI_API_KEY", "")
        initial_key = loaded_settings.openai_api_key if loaded_settings.remember_api_key else env_key
        self.openai_api_key_var = StringVar(value=initial_key or env_key)
        self.remember_api_key_var = BooleanVar(value=loaded_settings.remember_api_key)
        self.api_key_status_var = StringVar(value="")
        self.status_var = StringVar(value="Stopped")
        self.pending_mic_id = loaded_settings.mic_device_id
        self.pending_system_id = loaded_settings.system_device_id

        self.openai_api_key_var.trace_add("write", self._on_api_key_change)
        self._on_api_key_change()
        self._build_ui()
        self.refresh_devices()
        if self.loaded_env_files:
            files = ", ".join(str(item) for item in self.loaded_env_files)
            self.append_log(f"[desktop-agent] loaded env file(s): {files}")
        else:
            self.append_log("[desktop-agent] no .env found; set OPENAI_API_KEY in app before start.")
        self.append_log(f"[desktop-agent] settings file: {self.settings_path}")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self.drain_logs)

    @staticmethod
    def _label_for_device(item: dict[str, object]) -> str:
        device_id = str(item.get("id", ""))
        name = str(item.get("name", ""))
        hostapi = str(item.get("hostapi", "unknown"))
        return f"{device_id} | {name} ({hostapi})"

    def _on_api_key_change(self, *_: object) -> None:
        has_key = bool(self.openai_api_key_var.get().strip())
        self.api_key_status_var.set(f"OPENAI_API_KEY: {'set' if has_key else 'missing'}")

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        header = ttk.Label(
            frame,
            text="Meeting Assistant Desktop Agent",
            font=("Segoe UI", 14, "bold"),
        )
        header.grid(row=0, column=0, columnspan=6, sticky="w", **pad)

        ttk.Label(frame, text="Session ID").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.session_var, width=28).grid(
            row=1, column=1, sticky="we", **pad
        )

        ttk.Label(frame, text="WebSocket Server").grid(row=1, column=2, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.server_var, width=30).grid(
            row=1, column=3, columnspan=3, sticky="we", **pad
        )

        ttk.Label(frame, text="Provider").grid(row=2, column=0, sticky="w", **pad)
        provider_combo = ttk.Combobox(
            frame,
            textvariable=self.provider_var,
            state="readonly",
            values=["openai", "anthropic", "mock"],
        )
        provider_combo.grid(row=2, column=1, sticky="we", **pad)

        ttk.Label(frame, text="Model").grid(row=2, column=2, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.model_var, width=28).grid(
            row=2, column=3, sticky="we", **pad
        )

        ttk.Label(frame, text="History Mode").grid(row=2, column=4, sticky="w", **pad)
        history_combo = ttk.Combobox(
            frame,
            textvariable=self.history_mode_var,
            state="readonly",
            values=["focused", "full", "stateless"],
        )
        history_combo.grid(row=2, column=5, sticky="we", **pad)

        ttk.Label(frame, text="Mic Device").grid(row=3, column=0, sticky="w", **pad)
        self.mic_combo = ttk.Combobox(frame, textvariable=self.mic_var, state="readonly")
        self.mic_combo.grid(row=3, column=1, columnspan=2, sticky="we", **pad)

        ttk.Label(frame, text="System Device").grid(row=3, column=3, sticky="w", **pad)
        self.system_combo = ttk.Combobox(frame, textvariable=self.system_var, state="readonly")
        self.system_combo.grid(row=3, column=4, columnspan=2, sticky="we", **pad)

        ttk.Checkbutton(
            frame,
            text="Disable Mic",
            variable=self.disable_mic_var,
        ).grid(row=4, column=0, sticky="w", **pad)
        ttk.Checkbutton(
            frame,
            text="Disable System",
            variable=self.disable_system_var,
        ).grid(row=4, column=1, sticky="w", **pad)

        ttk.Button(frame, text="Refresh Devices", command=self.refresh_devices).grid(
            row=4, column=2, sticky="we", **pad
        )

        ttk.Label(frame, text="Dashboard URL").grid(row=4, column=3, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.dashboard_url_var, width=30).grid(
            row=4, column=4, sticky="we", **pad
        )
        ttk.Button(frame, text="Open Dashboard", command=self.open_dashboard).grid(
            row=4, column=5, sticky="we", **pad
        )

        ttk.Label(frame, text="OpenAI API Key").grid(row=5, column=0, sticky="w", **pad)
        ttk.Entry(
            frame,
            textvariable=self.openai_api_key_var,
            width=28,
            show="*",
        ).grid(row=5, column=1, columnspan=2, sticky="we", **pad)
        ttk.Checkbutton(
            frame,
            text="Remember key on this device",
            variable=self.remember_api_key_var,
        ).grid(row=5, column=3, sticky="w", **pad)
        ttk.Label(frame, textvariable=self.api_key_status_var).grid(
            row=5, column=4, columnspan=2, sticky="w", **pad
        )

        ttk.Button(frame, text="Start Capture", command=self.start_capture).grid(
            row=6, column=0, sticky="we", **pad
        )
        ttk.Button(frame, text="Stop Capture", command=self.stop_capture).grid(
            row=6, column=1, sticky="we", **pad
        )
        ttk.Label(frame, textvariable=self.status_var).grid(row=6, column=2, columnspan=4, sticky="w", **pad)

        self.log_view = ScrolledText(frame, wrap="word", height=22, font=("Consolas", 9))
        self.log_view.grid(row=7, column=0, columnspan=6, sticky="nsew", padx=8, pady=8)
        self.log_view.configure(state="disabled")

        for col in range(6):
            frame.columnconfigure(col, weight=1)
        frame.rowconfigure(7, weight=1)

    def append_log(self, line: str) -> None:
        self.log_view.configure(state="normal")
        self.log_view.insert("end", line + "\n")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def refresh_devices(self) -> None:
        payload = load_devices()
        if not payload.get("available"):
            messagebox.showerror("Device Error", str(payload.get("error", "Unknown error")))
            return

        mic_devices = payload.get("mic_devices", [])
        system_devices = payload.get("system_devices", [])
        suggested = payload.get("suggested", {})
        suggested_mic = str(suggested.get("mic_device", ""))
        suggested_system = str(suggested.get("system_device", ""))

        mic_values = ["auto"]
        self.mic_map.clear()
        for item in mic_devices:
            label = self._label_for_device(item)
            mic_values.append(label)
            self.mic_map[label] = str(item.get("id", ""))
        self.mic_combo["values"] = mic_values

        system_values = ["auto"]
        self.system_map.clear()
        for item in system_devices:
            label = self._label_for_device(item)
            system_values.append(label)
            self.system_map[label] = str(item.get("id", ""))
        self.system_combo["values"] = system_values

        self.mic_var.set("auto")
        self.system_var.set("auto")
        for label, device_id in self.mic_map.items():
            if device_id == (self.pending_mic_id or suggested_mic):
                self.mic_var.set(label)
                break
        for label, device_id in self.system_map.items():
            if device_id == (self.pending_system_id or suggested_system):
                self.system_var.set(label)
                break

        self.append_log(
            f"[desktop-agent] loaded devices: mic={len(mic_devices)} system={len(system_devices)}"
        )

    def _selected_device_id(self, source: str) -> str:
        if source == "mic":
            value = self.mic_var.get()
            if value == "auto":
                return ""
            return self.mic_map.get(value, "")
        value = self.system_var.get()
        if value == "auto":
            return ""
        return self.system_map.get(value, "")

    def _collect_settings(self) -> DesktopSettings:
        stt_provider = os.getenv("STT_PROVIDER", "openai").strip().lower()
        key = self.openai_api_key_var.get().strip()
        if stt_provider != "openai":
            key = ""
        return DesktopSettings(
            session_id=self.session_var.get().strip(),
            server=self.server_var.get().strip() or "ws://127.0.0.1:8000",
            provider=self.provider_var.get().strip() or "openai",
            model=self.model_var.get().strip() or "gpt-4o-mini",
            history_mode=self.history_mode_var.get().strip() or "focused",
            dashboard_url=self.dashboard_url_var.get().strip() or "http://localhost:3000",
            disable_mic=bool(self.disable_mic_var.get()),
            disable_system=bool(self.disable_system_var.get()),
            mic_device_id=self._selected_device_id("mic"),
            system_device_id=self._selected_device_id("system"),
            remember_api_key=bool(self.remember_api_key_var.get()),
            openai_api_key=key,
        )

    def persist_settings(self) -> None:
        try:
            payload = self._collect_settings()
            payload.save(self.settings_path)
        except Exception as exc:
            self.append_log(f"[desktop-agent] warning: failed to save settings: {exc}")

    def _spawn_command(self, capture_args: list[str]) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--capture-worker", *capture_args]
        return [sys.executable, str(Path(__file__).resolve()), "--capture-worker", *capture_args]

    def start_capture(self) -> None:
        if os.name != "nt":
            messagebox.showerror(
                "Not Supported",
                "Live system/mic capture is currently supported on Windows only in this MVP.",
            )
            return
        if self.process is not None and self.process.poll() is None:
            messagebox.showinfo("Capture Running", "Capture is already running.")
            return

        session_id = self.session_var.get().strip()
        if not session_id:
            messagebox.showwarning("Missing Session", "Please enter a session ID.")
            return

        stt_provider = os.getenv("STT_PROVIDER", "openai").strip().lower()
        openai_api_key = self.openai_api_key_var.get().strip() or os.getenv("OPENAI_API_KEY", "").strip()
        if stt_provider == "openai" and not openai_api_key:
            messagebox.showerror(
                "Missing OPENAI_API_KEY",
                "OPENAI_API_KEY is required for speech transcription. "
                "Paste it in the OpenAI API Key field before starting capture.",
            )
            return

        capture_args = [
            "--session-id",
            session_id,
            "--server",
            self.server_var.get().strip() or "ws://127.0.0.1:8000",
            "--provider",
            self.provider_var.get().strip() or "openai",
            "--model",
            self.model_var.get().strip() or "gpt-4o-mini",
            "--history-mode",
            self.history_mode_var.get().strip() or "focused",
        ]
        mic_device = self._selected_device_id("mic")
        system_device = self._selected_device_id("system")
        if mic_device:
            capture_args.extend(["--mic-device", mic_device])
        if system_device:
            capture_args.extend(["--system-device", system_device])
        if self.disable_mic_var.get():
            capture_args.append("--disable-mic")
        if self.disable_system_var.get():
            capture_args.append("--disable-system")

        self.persist_settings()
        command = self._spawn_command(capture_args)
        env = os.environ.copy()
        if openai_api_key:
            env["OPENAI_API_KEY"] = openai_api_key
        self.process = subprocess.Popen(
            command,
            cwd=str(WORK_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.status_var.set(f"Running (PID {self.process.pid})")
        self.append_log("[desktop-agent] started capture")
        self.append_log(
            f"[desktop-agent] stt_provider={stt_provider} openai_key={'set' if bool(openai_api_key) else 'missing'}"
        )
        self.append_log(" ".join(command))

        thread = threading.Thread(target=self._read_process_logs, daemon=True)
        thread.start()

    def _read_process_logs(self) -> None:
        proc = self.process
        if proc is None or proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            clean = line.rstrip("\n")
            if clean:
                self.log_queue.put(clean)
        proc.stdout.close()
        code = proc.poll()
        self.log_queue.put(f"[desktop-agent] capture exited with code {code}")

    def stop_capture(self) -> None:
        proc = self.process
        if proc is None or proc.poll() is not None:
            self.status_var.set("Stopped")
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        self.status_var.set("Stopped")
        self.append_log("[desktop-agent] stopped capture")

    def open_dashboard(self) -> None:
        url = self.dashboard_url_var.get().strip() or "http://localhost:3000"
        webbrowser.open(url)

    def drain_logs(self) -> None:
        moved = False
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.append_log(line)
            moved = True
        if moved and self.process is not None and self.process.poll() is None:
            self.status_var.set(f"Running (PID {self.process.pid})")
        elif self.process is None or self.process.poll() is not None:
            self.status_var.set("Stopped")
        self.root.after(200, self.drain_logs)

    def on_close(self) -> None:
        self.persist_settings()
        self.stop_capture()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if "--capture-worker" in sys.argv:
        idx = sys.argv.index("--capture-worker")
        worker_argv = sys.argv[idx + 1 :]
        run_capture_worker(worker_argv)
        return
    app = DesktopAgentApp()
    app.run()


if __name__ == "__main__":
    main()
