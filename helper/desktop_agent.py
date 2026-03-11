from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import END, BooleanVar, Listbox, StringVar, Tk, ttk, messagebox
from tkinter.scrolledtext import ScrolledText
from typing import Any

from dotenv import load_dotenv
from websockets.sync.client import connect as ws_connect

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
        self.live_queue: queue.Queue[str] = queue.Queue()
        self.live_status_queue: queue.Queue[str] = queue.Queue()
        self.mic_map: dict[str, str] = {}
        self.system_map: dict[str, str] = {}
        self.loaded_env_files = seed_environment_from_dotenv()
        self.settings_path = default_settings_path()
        self.live_thread: threading.Thread | None = None
        self.live_stop_event = threading.Event()
        self.live_session_id = ""
        self.helper_agent_process: subprocess.Popen[str] | None = None
        self.helper_agent_managed = False
        self.helper_agent_key = ""
        self.live_sessions_cache: list[dict[str, Any]] = []
        self.saved_sessions_cache: list[dict[str, Any]] = []
        loaded_settings = DesktopSettings.load(self.settings_path)

        # Start with an empty session field and require explicit Create/Join action.
        self.session_var = StringVar(value="")
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
        self.live_status_var = StringVar(value="Live view: disconnected")
        self.helper_status_var = StringVar(value="Helper: unknown")
        self.live_sessions_summary_var = StringVar(value="No live sessions running.")
        self.saved_sessions_summary_var = StringVar(value="No saved sessions found.")
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
        self.ensure_helper_agent_running()
        self.refresh_session_lists(silent=True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self.drain_logs)
        self.root.after(6000, self.poll_session_lists)

    @staticmethod
    def _label_for_device(item: dict[str, object]) -> str:
        device_id = str(item.get("id", ""))
        name = str(item.get("name", ""))
        hostapi = str(item.get("hostapi", "unknown"))
        return f"{device_id} | {name} ({hostapi})"

    def _on_api_key_change(self, *_: object) -> None:
        has_key = bool(self.openai_api_key_var.get().strip())
        self.api_key_status_var.set(f"OPENAI_API_KEY: {'set' if has_key else 'missing'}")
        if (
            self.helper_agent_managed
            and self.helper_agent_process is not None
            and self.helper_agent_process.poll() is None
            and (self.openai_api_key_var.get().strip() or os.getenv("OPENAI_API_KEY", "").strip()) != self.helper_agent_key
        ):
            self.helper_status_var.set("Helper: restart needed (key changed)")

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        header = ttk.Label(
            frame,
            text="Meeting Assistant Desktop Agent",
            font=("Segoe UI", 14, "bold"),
        )
        header.grid(row=0, column=0, columnspan=5, sticky="w", **pad)
        ttk.Label(frame, textvariable=self.helper_status_var).grid(row=0, column=5, sticky="e", **pad)

        ttk.Label(frame, text="Session ID").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.session_var, width=28).grid(
            row=1, column=1, sticky="we", **pad
        )
        ttk.Button(frame, text="Create Session", command=self.create_session).grid(
            row=1, column=2, sticky="we", **pad
        )

        ttk.Label(frame, text="WebSocket Server").grid(row=1, column=3, sticky="w", **pad)
        ttk.Entry(frame, textvariable=self.server_var, width=30).grid(
            row=1, column=4, columnspan=2, sticky="we", **pad
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
        ttk.Button(frame, text="Open Session in Web", command=self.open_dashboard).grid(
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
        ttk.Label(frame, textvariable=self.status_var).grid(row=6, column=2, columnspan=2, sticky="w", **pad)
        ttk.Label(frame, textvariable=self.live_status_var).grid(row=6, column=4, columnspan=2, sticky="w", **pad)

        tabs = ttk.Notebook(frame)
        tabs.grid(row=7, column=0, columnspan=6, sticky="nsew", padx=8, pady=8)

        conversation_tab = ttk.Frame(tabs)
        logs_tab = ttk.Frame(tabs)
        sessions_tab = ttk.Frame(tabs)
        tabs.add(conversation_tab, text="Conversation")
        tabs.add(logs_tab, text="System Logs")
        tabs.add(sessions_tab, text="Sessions")

        self.conversation_view = ScrolledText(
            conversation_tab, wrap="word", height=22, font=("Consolas", 9)
        )
        self.conversation_view.pack(fill="both", expand=True)
        self.conversation_view.configure(state="disabled")

        self.log_view = ScrolledText(logs_tab, wrap="word", height=22, font=("Consolas", 9))
        self.log_view.pack(fill="both", expand=True)
        self.log_view.configure(state="disabled")

        sessions_tab.columnconfigure(0, weight=1)
        sessions_tab.columnconfigure(1, weight=1)
        sessions_tab.rowconfigure(2, weight=1)
        sessions_tab.rowconfigure(5, weight=1)

        ttk.Label(sessions_tab, text="Live Sessions", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky="w", padx=6, pady=(6, 2)
        )
        ttk.Label(sessions_tab, textvariable=self.live_sessions_summary_var).grid(
            row=1, column=0, sticky="w", padx=6, pady=(0, 4)
        )
        self.live_sessions_listbox = Listbox(sessions_tab, height=8, exportselection=False)
        self.live_sessions_listbox.grid(row=2, column=0, sticky="nsew", padx=6, pady=4)
        live_actions = ttk.Frame(sessions_tab)
        live_actions.grid(row=3, column=0, sticky="ew", padx=6, pady=(0, 8))
        ttk.Button(live_actions, text="Refresh Live", command=self.refresh_live_sessions).pack(side="left")
        ttk.Button(live_actions, text="Join Live", command=self.join_selected_live_session).pack(
            side="left", padx=(6, 0)
        )

        ttk.Label(sessions_tab, text="Saved Sessions", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=1, sticky="w", padx=6, pady=(6, 2)
        )
        ttk.Label(sessions_tab, textvariable=self.saved_sessions_summary_var).grid(
            row=1, column=1, sticky="w", padx=6, pady=(0, 4)
        )
        self.saved_sessions_listbox = Listbox(sessions_tab, height=8, exportselection=False)
        self.saved_sessions_listbox.grid(row=2, column=1, rowspan=4, sticky="nsew", padx=6, pady=4)
        saved_actions = ttk.Frame(sessions_tab)
        saved_actions.grid(row=6, column=1, sticky="ew", padx=6, pady=(0, 8))
        ttk.Button(saved_actions, text="Refresh Saved", command=self.refresh_saved_sessions).pack(side="left")
        ttk.Button(saved_actions, text="Join Saved", command=self.join_selected_saved_session).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(saved_actions, text="Delete Saved", command=self.delete_selected_saved_session).pack(
            side="left", padx=(6, 0)
        )

        for col in range(6):
            frame.columnconfigure(col, weight=1)
        frame.rowconfigure(7, weight=1)

    def append_log(self, line: str) -> None:
        self.log_view.configure(state="normal")
        self.log_view.insert("end", line + "\n")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def append_conversation(self, line: str) -> None:
        self.conversation_view.configure(state="normal")
        self.conversation_view.insert("end", line + "\n")
        self.conversation_view.see("end")
        self.conversation_view.configure(state="disabled")

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

    def _api_base_from_server(self) -> str:
        raw = self.server_var.get().strip()
        if not raw:
            raise ValueError("WebSocket Server is empty.")
        parsed = urllib.parse.urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("WebSocket Server must include scheme and host, for example ws://127.0.0.1:8000")
        if parsed.scheme in {"ws", "http"}:
            scheme = "http"
        elif parsed.scheme in {"wss", "https"}:
            scheme = "https"
        else:
            raise ValueError(f"Unsupported server scheme: {parsed.scheme}")
        return f"{scheme}://{parsed.netloc}"

    def _helper_base_url(self) -> str:
        return os.getenv("HELPER_AGENT_BASE_URL", "http://127.0.0.1:8765").rstrip("/")

    def _helper_health_ok(self) -> bool:
        url = f"{self._helper_base_url()}/api/health"
        request = urllib.request.Request(url=url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=1.5) as response:
                body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            return str(payload.get("status", "")).strip().lower() == "ok"
        except Exception:
            return False

    def _spawn_helper_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--helper-agent"]
        return [sys.executable, str(Path(__file__).resolve()), "--helper-agent"]

    def _read_helper_logs(self, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            clean = line.rstrip("\n")
            if clean:
                self.log_queue.put(f"[helper-agent] {clean}")
        proc.stdout.close()
        code = proc.poll()
        self.log_queue.put(f"[helper-agent] exited with code {code}")

    def stop_helper_agent(self) -> None:
        proc = self.helper_agent_process
        if not self.helper_agent_managed or proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.helper_agent_process = None
        self.helper_agent_managed = False
        self.helper_agent_key = ""
        self.helper_status_var.set("Helper: stopped")

    def ensure_helper_agent_running(self) -> None:
        current_key = self.openai_api_key_var.get().strip() or os.getenv("OPENAI_API_KEY", "").strip()

        if self.helper_agent_managed and self.helper_agent_process is not None:
            if self.helper_agent_process.poll() is None and current_key == self.helper_agent_key:
                self.helper_status_var.set("Helper: running (desktop)")
                return
            self.stop_helper_agent()

        if self._helper_health_ok():
            self.helper_status_var.set("Helper: running (external)")
            return

        cmd = self._spawn_helper_command()
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        if current_key:
            env["OPENAI_API_KEY"] = current_key
        self.helper_agent_key = current_key
        proc = subprocess.Popen(
            cmd,
            cwd=str(WORK_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self.helper_agent_process = proc
        self.helper_agent_managed = True
        self.helper_status_var.set("Helper: starting...")
        self.append_log("[desktop-agent] starting helper agent for web capture control")
        self.append_log(" ".join(cmd))
        thread = threading.Thread(target=self._read_helper_logs, args=(proc,), daemon=True)
        thread.start()

        for _ in range(25):
            if proc.poll() is not None:
                break
            if self._helper_health_ok():
                self.helper_status_var.set("Helper: running (desktop)")
                self.append_log("[desktop-agent] helper agent is healthy")
                return
            time.sleep(0.2)

        if proc.poll() is not None:
            self.helper_status_var.set("Helper: failed")
            self.append_log("[desktop-agent] helper agent failed to start")
            return
        self.helper_status_var.set("Helper: starting (slow)")

    def _api_request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        api_base = self._api_base_from_server()
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=f"{api_base}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method=method.upper(),
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw else {}
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Unexpected API response for {path}: {raw[:200]}")
        return parsed

    @staticmethod
    def _session_label(item: dict[str, Any], include_socket_count: bool = False) -> str:
        title = str(item.get("title", "") or "Untitled Session")
        provider = str(item.get("provider", ""))
        model = str(item.get("model", ""))
        session_id = str(item.get("id", ""))
        socket_suffix = ""
        if include_socket_count:
            capture_count = int(item.get("capture_socket_count", 0) or 0)
            socket_count = int(item.get("socket_count", 0) or 0)
            socket_suffix = f" | capture={capture_count} | sockets={socket_count}"
        return f"{title} | {provider}/{model} | {session_id}{socket_suffix}"

    @staticmethod
    def _selected_session_id(listbox: Listbox, items: list[dict[str, Any]]) -> str:
        selection = listbox.curselection()
        if not selection:
            return ""
        index = int(selection[0])
        if index < 0 or index >= len(items):
            return ""
        return str(items[index].get("id", ""))

    def refresh_live_sessions(self, silent: bool = False) -> None:
        try:
            data = self._api_request_json("GET", "/api/live-sessions", timeout=4.0)
            items = data.get("items", [])
            if not isinstance(items, list):
                items = []
            self.live_sessions_cache = [item for item in items if isinstance(item, dict)]
            self.live_sessions_listbox.delete(0, END)
            for item in self.live_sessions_cache:
                self.live_sessions_listbox.insert(END, self._session_label(item, include_socket_count=True))
            if self.live_sessions_cache:
                self.live_sessions_summary_var.set(f"{len(self.live_sessions_cache)} live session(s)")
            else:
                self.live_sessions_summary_var.set("No live sessions running.")
        except Exception as exc:
            self.live_sessions_cache = []
            self.live_sessions_listbox.delete(0, END)
            self.live_sessions_summary_var.set("No live sessions running.")
            if silent:
                self.append_log(f"[desktop-agent] live sessions refresh failed: {exc}")
            else:
                messagebox.showerror("Live Sessions Error", str(exc))

    def refresh_saved_sessions(self, silent: bool = False) -> None:
        try:
            data = self._api_request_json("GET", "/api/sessions?limit=200", timeout=6.0)
            items = data.get("items", [])
            if not isinstance(items, list):
                items = []
            self.saved_sessions_cache = [item for item in items if isinstance(item, dict)]
            self.saved_sessions_listbox.delete(0, END)
            for item in self.saved_sessions_cache:
                self.saved_sessions_listbox.insert(END, self._session_label(item, include_socket_count=False))
            if self.saved_sessions_cache:
                self.saved_sessions_summary_var.set(f"{len(self.saved_sessions_cache)} saved session(s)")
            else:
                self.saved_sessions_summary_var.set("No saved sessions found.")
        except Exception as exc:
            self.saved_sessions_cache = []
            self.saved_sessions_listbox.delete(0, END)
            self.saved_sessions_summary_var.set("No saved sessions found.")
            if silent:
                self.append_log(f"[desktop-agent] saved sessions refresh failed: {exc}")
            else:
                messagebox.showerror("Saved Sessions Error", str(exc))

    def refresh_session_lists(self, silent: bool = False) -> None:
        self.refresh_live_sessions(silent=silent)
        self.refresh_saved_sessions(silent=silent)

    def poll_session_lists(self) -> None:
        self.refresh_session_lists(silent=True)
        self.root.after(6000, self.poll_session_lists)

    def join_selected_live_session(self) -> None:
        session_id = self._selected_session_id(self.live_sessions_listbox, self.live_sessions_cache)
        if not session_id:
            return
        self.session_var.set(session_id)
        self.ensure_live_view_for_current_session()
        self.persist_settings()
        self.status_var.set("Joined live session")
        self.append_log(f"[desktop-agent] joined live session: {session_id}")

    def join_selected_saved_session(self) -> None:
        session_id = self._selected_session_id(self.saved_sessions_listbox, self.saved_sessions_cache)
        if not session_id:
            return
        self.session_var.set(session_id)
        self.ensure_live_view_for_current_session()
        self.persist_settings()
        self.status_var.set("Joined saved session")
        self.append_log(f"[desktop-agent] joined saved session: {session_id}")

    def delete_selected_saved_session(self) -> None:
        session_id = self._selected_session_id(self.saved_sessions_listbox, self.saved_sessions_cache)
        if not session_id:
            return
        label = session_id
        index = self.saved_sessions_listbox.curselection()
        if index:
            idx = int(index[0])
            if 0 <= idx < len(self.saved_sessions_cache):
                label = str(self.saved_sessions_cache[idx].get("title", session_id))
        if not messagebox.askyesno("Delete Session", f'Delete session "{label}"? This cannot be undone.'):
            return
        try:
            self._api_request_json("DELETE", f"/api/sessions/{urllib.parse.quote(session_id, safe='')}", timeout=8.0)
            self.append_log(f"[desktop-agent] deleted session: {session_id}")
            if self.session_var.get().strip() == session_id:
                self.stop_capture()
                self.session_var.set("")
                self.stop_live_view()
            self.refresh_session_lists(silent=True)
            self.persist_settings()
        except Exception as exc:
            messagebox.showerror("Delete Failed", str(exc))

    def create_session(self) -> None:
        self.ensure_helper_agent_running()

        title = f"Desktop Session {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        payload = {
            "title": title,
            "context": "",
            "provider": self.provider_var.get().strip() or "openai",
            "model": self.model_var.get().strip() or "gpt-4o-mini",
            "history_mode": self.history_mode_var.get().strip() or "focused",
        }

        try:
            parsed = self._api_request_json("POST", "/api/sessions", payload=payload, timeout=12.0)
        except Exception as exc:
            messagebox.showerror("Session Create Failed", str(exc))
            return

        session_id = str(parsed.get("id", "")).strip()
        if not session_id:
            messagebox.showerror("Session Create Failed", "Backend response missing session id.")
            return

        self.session_var.set(session_id)
        self.status_var.set("Session created")
        self.append_log(f"[desktop-agent] created session: {session_id}")
        self.ensure_live_view_for_current_session()
        self.refresh_session_lists(silent=True)
        self.persist_settings()

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

    @staticmethod
    def _display_time() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _ws_url_for_session(self, session_id: str) -> str:
        raw = self.server_var.get().strip()
        if not raw:
            raise ValueError("WebSocket Server is empty.")
        parsed = urllib.parse.urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("WebSocket Server must include scheme and host.")
        if parsed.scheme in {"ws", "http"}:
            scheme = "ws"
        elif parsed.scheme in {"wss", "https"}:
            scheme = "wss"
        else:
            raise ValueError(f"Unsupported server scheme: {parsed.scheme}")
        return f"{scheme}://{parsed.netloc}/ws/{urllib.parse.quote(session_id, safe='')}"

    def ensure_live_view_for_current_session(self) -> None:
        session_id = self.session_var.get().strip()
        if not session_id:
            self.live_status_var.set("Live view: waiting for session")
            return
        if self.live_thread is not None and self.live_thread.is_alive() and self.live_session_id == session_id:
            return
        self.stop_live_view()
        self.start_live_view(session_id)

    def start_live_view(self, session_id: str) -> None:
        if not session_id:
            return
        self.live_stop_event.clear()
        self.live_session_id = session_id
        self.live_status_var.set(f"Live view: connecting ({session_id})")
        self.live_thread = threading.Thread(
            target=self._live_listener_worker,
            args=(session_id,),
            daemon=True,
        )
        self.live_thread.start()

    def stop_live_view(self) -> None:
        self.live_stop_event.set()
        thread = self.live_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self.live_thread = None
        self.live_session_id = ""
        self.live_status_var.set("Live view: disconnected")

    def _live_listener_worker(self, session_id: str) -> None:
        try:
            url = self._ws_url_for_session(session_id)
        except Exception as exc:
            self.log_queue.put(f"[desktop-agent] live view setup error: {exc}")
            self.live_status_queue.put("Live view: invalid server")
            return

        while not self.live_stop_event.is_set():
            try:
                with ws_connect(url, open_timeout=8, close_timeout=2) as ws:
                    self.log_queue.put(f"[desktop-agent] live view connected: {session_id}")
                    self.live_status_queue.put(f"Live view: connected ({session_id})")
                    while not self.live_stop_event.is_set():
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        if raw is None:
                            break
                        self._handle_live_message(raw)
            except Exception as exc:
                if self.live_stop_event.is_set():
                    break
                self.log_queue.put(f"[desktop-agent] live view reconnecting: {exc}")
                self.live_status_queue.put("Live view: reconnecting...")
                self.live_stop_event.wait(2.0)

    def start_capture(self) -> None:
        self.ensure_helper_agent_running()
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
        self.ensure_live_view_for_current_session()

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

    def _handle_live_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return
        msg_type = str(payload.get("type", ""))
        if msg_type == "transcript":
            source = str(payload.get("source", "unknown"))
            text = str(payload.get("text", "")).strip()
            if text:
                self.live_queue.put(f"[{self._display_time()}] {source}: {text}")
            return
        if msg_type == "suggestion":
            provider = str(payload.get("provider", "unknown"))
            model = str(payload.get("model", "unknown"))
            latency = int(payload.get("latency_ms", 0) or 0)
            text = str(payload.get("text", "")).strip()
            if text:
                self.live_queue.put(
                    f"[{self._display_time()}] suggestion {provider}/{model} {latency}ms\n{text}\n"
                )
            return
        if msg_type == "status":
            message = str(payload.get("message", "")).strip()
            if message:
                self.live_queue.put(f"[{self._display_time()}] status: {message}")
            return
        if msg_type == "error":
            message = str(payload.get("message", "")).strip()
            if message:
                self.live_queue.put(f"[{self._display_time()}] error: {message}")

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
        session_id = self.session_var.get().strip()
        if session_id:
            encoded = urllib.parse.quote(session_id, safe="")
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}session={encoded}"
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
        while True:
            try:
                line = self.live_queue.get_nowait()
            except queue.Empty:
                break
            self.append_conversation(line)
            moved = True
        while True:
            try:
                status = self.live_status_queue.get_nowait()
            except queue.Empty:
                break
            self.live_status_var.set(status)
            moved = True
        if moved and self.process is not None and self.process.poll() is None:
            self.status_var.set(f"Running (PID {self.process.pid})")
        elif self.process is None or self.process.poll() is not None:
            self.status_var.set("Stopped")
        self.root.after(200, self.drain_logs)

    def on_close(self) -> None:
        self.persist_settings()
        self.stop_live_view()
        self.stop_helper_agent()
        self.stop_capture()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if "--helper-agent" in sys.argv:
        try:
            from helper.ui_agent import run as run_helper_agent
        except ModuleNotFoundError:
            from ui_agent import run as run_helper_agent  # type: ignore
        run_helper_agent()
        return
    if "--capture-worker" in sys.argv:
        idx = sys.argv.index("--capture-worker")
        worker_argv = sys.argv[idx + 1 :]
        run_capture_worker(worker_argv)
        return
    app = DesktopAgentApp()
    app.run()


if __name__ == "__main__":
    main()
