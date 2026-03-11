"use client";
/* eslint-disable react-hooks/set-state-in-effect */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type HistoryMode = "focused" | "full" | "stateless";

type SessionSummary = {
  id: string;
  title: string;
  context: string;
  provider: string;
  model: string;
  history_mode: HistoryMode;
  history_lines: number;
  created_at: string;
  updated_at: string;
  transcript_count?: number;
  suggestion_count?: number;
};

type LiveSessionSummary = SessionSummary & {
  socket_count?: number;
  capture_socket_count?: number;
};

type TranscriptEvent = {
  type: "transcript";
  id?: number;
  session_id: string;
  source: string;
  text: string;
  created_at: string;
};

type SuggestionEvent = {
  type: "suggestion";
  id?: number;
  session_id: string;
  provider: string;
  model: string;
  latency_ms: number;
  text: string;
  created_at: string;
};

type TimelineEvent = TranscriptEvent | SuggestionEvent;

type WsStatus = "disconnected" | "connecting" | "connected";

type AudioDevice = {
  id: string;
  name: string;
  hostapi: string;
  max_input_channels: number;
  max_output_channels: number;
  default_sample_rate: number;
  is_default_input: boolean;
  is_default_output: boolean;
  is_stereo_mix_like: boolean;
};

type AudioDevicesResponse = {
  available: boolean;
  error: string;
  devices?: AudioDevice[];
  mic_devices?: AudioDevice[];
  system_devices?: AudioDevice[];
  all_devices?: AudioDevice[];
  suggested: {
    mic_device: string;
    system_device: string;
  };
};

const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/$/, "");
const HELPER_AGENT_BASE = (
  process.env.NEXT_PUBLIC_HELPER_AGENT_BASE_URL || "http://127.0.0.1:8765"
).replace(/\/$/, "");

function wsUrlForSession(sessionId: string): string {
  const url = new URL(API_BASE);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `/ws/${encodeURIComponent(sessionId)}`;
  return url.toString();
}

function apiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

function helperUrl(path: string): string {
  return `${HELPER_AGENT_BASE}${path}`;
}

async function readJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

async function readHelperJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(helperUrl(path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Helper request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

function prettyTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function HomePage() {
  const wsRef = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<WsStatus>("disconnected");
  const [statusText, setStatusText] = useState("Idle");
  const [errorText, setErrorText] = useState("");

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [liveSessions, setLiveSessions] = useState<LiveSessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState("");
  const [events, setEvents] = useState<TimelineEvent[]>([]);

  const [provider, setProvider] = useState("openai");
  const [model, setModel] = useState("gpt-4o-mini");
  const [context, setContext] = useState("");
  const [historyMode, setHistoryMode] = useState<HistoryMode>("focused");
  const [historyLines, setHistoryLines] = useState(10);
  const [sessionTitle, setSessionTitle] = useState("");

  const [manualSource, setManualSource] = useState<"mic" | "system">("mic");
  const [manualText, setManualText] = useState("");
  const [allAudioDevices, setAllAudioDevices] = useState<AudioDevice[]>([]);
  const [micAudioDevices, setMicAudioDevices] = useState<AudioDevice[]>([]);
  const [systemAudioDevices, setSystemAudioDevices] = useState<AudioDevice[]>([]);
  const [audioDeviceError, setAudioDeviceError] = useState("");
  const [selectedMicDevice, setSelectedMicDevice] = useState("");
  const [selectedSystemDevice, setSelectedSystemDevice] = useState("");
  const [showAllDevices, setShowAllDevices] = useState(false);
  const [agentOnline, setAgentOnline] = useState(false);
  const [captureRunning, setCaptureRunning] = useState(false);
  const [captureLogs, setCaptureLogs] = useState<string[]>([]);
  const [autoStopOnTabClose, setAutoStopOnTabClose] = useState(true);
  const [sessionFromUrl, setSessionFromUrl] = useState("");
  const [urlSessionApplied, setUrlSessionApplied] = useState("");

  const latestSuggestion = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i -= 1) {
      if (events[i].type === "suggestion") return events[i] as SuggestionEvent;
    }
    return null;
  }, [events]);

  const micDevices = useMemo(() => {
    if (showAllDevices) {
      return allAudioDevices.filter((item) => item.max_input_channels > 0);
    }
    return micAudioDevices;
  }, [allAudioDevices, micAudioDevices, showAllDevices]);
  const systemDevices = useMemo(() => {
    if (showAllDevices) {
      return allAudioDevices.filter((item) => item.max_input_channels > 0);
    }
    return systemAudioDevices;
  }, [allAudioDevices, showAllDevices, systemAudioDevices]);

  const helperCommand = useMemo(() => {
    const parts = [
      "python",
      "helper\\audio_capture_windows.py",
      "--session-id",
      activeSessionId || "<SESSION_ID>",
      "--provider",
      provider,
      "--model",
      model,
      "--history-mode",
      historyMode,
    ];
    if (selectedMicDevice) {
      parts.push("--mic-device", selectedMicDevice);
    }
    if (selectedSystemDevice) {
      parts.push("--system-device", selectedSystemDevice);
    }
    return parts.join(" ");
  }, [activeSessionId, historyMode, model, provider, selectedMicDevice, selectedSystemDevice]);

  const agentStartCommand = useMemo(
    () => ".venv\\Scripts\\python.exe -m helper.ui_agent",
    []
  );

  const fetchSessions = useCallback(async () => {
    try {
      const data = await readJson<{ items: SessionSummary[] }>("/api/sessions");
      setSessions(data.items);
      if (!activeSessionId && data.items.length > 0) {
        const hasUrlSession = Boolean(
          sessionFromUrl && data.items.some((item) => item.id === sessionFromUrl)
        );
        const nextId = hasUrlSession ? sessionFromUrl : data.items[0].id;
        setActiveSessionId(nextId);
        if (hasUrlSession) {
          setUrlSessionApplied(sessionFromUrl);
          setStatusText(`Opened session from URL: ${sessionFromUrl}`);
        }
      }
    } catch (err) {
      setErrorText(String(err));
    }
  }, [activeSessionId, sessionFromUrl]);

  const fetchLiveSessions = useCallback(async () => {
    try {
      const data = await readJson<{ items: LiveSessionSummary[] }>("/api/live-sessions");
      setLiveSessions(data.items);
    } catch (err) {
      setErrorText(String(err));
    }
  }, []);

  const loadSession = useCallback(async (sessionId: string) => {
    try {
      const data = await readJson<{ session: SessionSummary; events: TimelineEvent[] }>(
        `/api/sessions/${encodeURIComponent(sessionId)}/events?limit=500`
      );
      setEvents(data.events);
      setSessionTitle(data.session.title);
      setProvider(data.session.provider);
      setModel(data.session.model);
      setContext(data.session.context);
      setHistoryMode(data.session.history_mode);
      setHistoryLines(data.session.history_lines);
      setErrorText("");
    } catch (err) {
      setErrorText(String(err));
    }
  }, []);

  const fetchAudioDevices = useCallback(async () => {
    const applyDevicePayload = (data: AudioDevicesResponse) => {
      const all = data.all_devices ?? data.devices ?? [];
      const mic = data.mic_devices ?? all.filter((item) => item.max_input_channels > 0);
      const sys = data.system_devices ?? all.filter((item) => item.max_input_channels > 0);
      setAllAudioDevices(all);
      setMicAudioDevices(mic);
      setSystemAudioDevices(sys);
      if (!selectedMicDevice && data.suggested.mic_device) {
        setSelectedMicDevice(data.suggested.mic_device);
      }
      if (!selectedSystemDevice && data.suggested.system_device) {
        setSelectedSystemDevice(data.suggested.system_device);
      }
    };

    try {
      const helperData = await readHelperJson<AudioDevicesResponse>("/api/devices");
      if (helperData.available) {
        applyDevicePayload(helperData);
        setAgentOnline(true);
        setAudioDeviceError("");
        return;
      }
      setAudioDeviceError(helperData.error || "Helper agent device list unavailable.");
      setAgentOnline(false);
    } catch {
      setAgentOnline(false);
    }

    try {
      const data = await readJson<AudioDevicesResponse>("/api/audio/devices");
      if (!data.available) {
        setAudioDeviceError(data.error || "Audio device list unavailable.");
        setAllAudioDevices([]);
        setMicAudioDevices([]);
        setSystemAudioDevices([]);
        return;
      }
      applyDevicePayload(data);
      setAudioDeviceError("");
    } catch (err) {
      setAudioDeviceError(String(err));
    }
  }, [selectedMicDevice, selectedSystemDevice]);

  const pollCaptureStatus = useCallback(async () => {
    if (!agentOnline) return;
    try {
      const statusPayload = await readHelperJson<{
        running: boolean;
        logs: string[];
      }>("/api/capture/status");
      setCaptureRunning(Boolean(statusPayload.running));
      setCaptureLogs(statusPayload.logs || []);
    } catch {
      setAgentOnline(false);
      setCaptureRunning(false);
    }
  }, [agentOnline]);

  const wsServerBase = useMemo(() => {
    const url = new URL(API_BASE);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = "";
    return `${url.protocol}//${url.host}`;
  }, []);

  const startLocalCapture = async () => {
    if (!activeSessionId) {
      setErrorText("Select a session first.");
      return;
    }
    try {
      await readHelperJson("/api/capture/start", {
        method: "POST",
        body: JSON.stringify({
          session_id: activeSessionId,
          server: wsServerBase,
          provider,
          model,
          history_mode: historyMode,
          context,
          mic_device: selectedMicDevice,
          system_device: selectedSystemDevice,
          disable_mic: false,
          disable_system: false,
        }),
      });
      setCaptureRunning(true);
      setStatusText("Started local capture via Helper Agent.");
      await pollCaptureStatus();
    } catch (err) {
      setErrorText(String(err));
    }
  };

  const stopLocalCapture = async () => {
    if (!agentOnline) return;
    try {
      await readHelperJson("/api/capture/stop", { method: "POST" });
      setCaptureRunning(false);
      setStatusText("Stopped local capture.");
      await pollCaptureStatus();
    } catch (err) {
      setErrorText(String(err));
    }
  };

  useEffect(() => {
    void fetchSessions();
    void fetchLiveSessions();
    const timer = setInterval(() => {
      void fetchSessions();
      void fetchLiveSessions();
    }, 8000);
    return () => clearInterval(timer);
  }, [fetchLiveSessions, fetchSessions]);

  useEffect(() => {
    void fetchAudioDevices();
  }, [fetchAudioDevices]);

  useEffect(() => {
    if (!agentOnline) {
      setCaptureRunning(false);
      setCaptureLogs([]);
      return;
    }
    void pollCaptureStatus();
    const timer = setInterval(() => {
      void pollCaptureStatus();
    }, 2000);
    return () => clearInterval(timer);
  }, [agentOnline, pollCaptureStatus]);

  useEffect(() => {
    if (!autoStopOnTabClose) return;
    const handleBeforeUnload = () => {
      if (!captureRunning) return;
      try {
        navigator.sendBeacon(helperUrl("/api/capture/stop"), "");
      } catch {
        // best effort
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [autoStopOnTabClose, captureRunning]);

  useEffect(() => {
    if (!activeSessionId) return;
    void loadSession(activeSessionId);
  }, [activeSessionId, loadSession]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    setSessionFromUrl((params.get("session") || "").trim());
  }, []);

  useEffect(() => {
    if (!sessionFromUrl) return;
    if (urlSessionApplied === sessionFromUrl) return;
    const exists = sessions.some((item) => item.id === sessionFromUrl);
    if (!exists) return;
    if (activeSessionId !== sessionFromUrl) {
      setActiveSessionId(sessionFromUrl);
    }
    setUrlSessionApplied(sessionFromUrl);
    setStatusText(`Opened session from URL: ${sessionFromUrl}`);
  }, [activeSessionId, sessionFromUrl, sessions, urlSessionApplied]);

  useEffect(() => {
    if (!activeSessionId) return;
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setStatus("connecting");
    setStatusText(`Connecting to ${activeSessionId}...`);
    const ws = new WebSocket(wsUrlForSession(activeSessionId));
    wsRef.current = ws;

    ws.onopen = () => {
      setStatus("connected");
      setStatusText(`Connected: ${activeSessionId}`);
    };

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as Record<string, unknown>;
        const type = String(payload.type || "");
        if (type === "status") {
          setStatusText(String(payload.message || "status"));
          return;
        }
        if (type === "error") {
          setErrorText(String(payload.message || "Unknown error"));
          return;
        }
        if (type === "session_meta") {
          if (typeof payload.provider === "string") setProvider(payload.provider);
          if (typeof payload.model === "string") setModel(payload.model);
          if (typeof payload.context === "string") setContext(payload.context);
          if (typeof payload.history_mode === "string") {
            const mode = payload.history_mode as HistoryMode;
            setHistoryMode(mode);
          }
          if (typeof payload.history_lines === "number") {
            setHistoryLines(payload.history_lines);
          }
          return;
        }
        if (type === "transcript") {
          const item: TranscriptEvent = {
            type: "transcript",
            session_id: String(payload.session_id || activeSessionId),
            source: String(payload.source || "unknown"),
            text: String(payload.text || ""),
            created_at: String(payload.created_at || new Date().toISOString()),
          };
          setEvents((prev) => [...prev, item]);
          return;
        }
        if (type === "suggestion") {
          const item: SuggestionEvent = {
            type: "suggestion",
            session_id: String(payload.session_id || activeSessionId),
            provider: String(payload.provider || "unknown"),
            model: String(payload.model || "unknown"),
            latency_ms: Number(payload.latency_ms || 0),
            text: String(payload.text || ""),
            created_at: new Date().toISOString(),
          };
          setEvents((prev) => [...prev, item]);
        }
      } catch (err) {
        setErrorText(`Failed to decode websocket message: ${String(err)}`);
      }
    };

    ws.onclose = () => {
      setStatus("disconnected");
      setStatusText("Disconnected");
    };
    ws.onerror = () => {
      setStatus("disconnected");
      setStatusText("WebSocket error");
    };

    return () => {
      ws.close();
    };
  }, [activeSessionId]);

  const handleCreateSession = async () => {
    try {
      const created = await readJson<SessionSummary>("/api/sessions", {
        method: "POST",
        body: JSON.stringify({
          title: sessionTitle || "Untitled Session",
          context,
          provider,
          model,
          history_mode: historyMode,
          history_lines: historyLines,
        }),
      });
      await fetchSessions();
      await fetchLiveSessions();
      setActiveSessionId(created.id);
    } catch (err) {
      setErrorText(String(err));
    }
  };

  const handleSaveConfig = async () => {
    if (!activeSessionId) return;
    try {
      await readJson(`/api/sessions/${encodeURIComponent(activeSessionId)}/config`, {
        method: "PATCH",
        body: JSON.stringify({
          title: sessionTitle,
          context,
          provider,
          model,
          history_mode: historyMode,
          history_lines: historyLines,
        }),
      });
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({
            type: "configure",
            client_role: "viewer",
            provider,
            model,
            context,
            history_mode: historyMode,
            history_lines: historyLines,
          })
        );
      }
      await fetchSessions();
      setErrorText("");
    } catch (err) {
      setErrorText(String(err));
    }
  };

  const handleDeleteSession = async (sessionId: string) => {
    const target = sessions.find((item) => item.id === sessionId);
    const label = target?.title || sessionId;
    if (!window.confirm(`Delete session "${label}"? This cannot be undone.`)) {
      return;
    }
    try {
      await readJson(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        method: "DELETE",
      });
      if (activeSessionId === sessionId) {
        setActiveSessionId("");
        setEvents([]);
      }
      await fetchSessions();
      await fetchLiveSessions();
      setStatusText(`Deleted session: ${sessionId}`);
      setErrorText("");
    } catch (err) {
      setErrorText(String(err));
    }
  };

  const sendManualTranscript = () => {
    if (!manualText.trim()) return;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setErrorText("Not connected to websocket.");
      return;
    }
    ws.send(
      JSON.stringify({
        type: "transcript",
        source: manualSource,
        text: manualText.trim(),
        final: true,
      })
    );
    setManualText("");
  };

  const copyActiveSessionId = async () => {
    if (!activeSessionId) return;
    try {
      await navigator.clipboard.writeText(activeSessionId);
      setStatusText(`Copied session id: ${activeSessionId}`);
    } catch {
      setStatusText(`Session id: ${activeSessionId}`);
    }
  };

  const copyHelperCommand = async () => {
    try {
      await navigator.clipboard.writeText(helperCommand);
      setStatusText("Copied helper command.");
    } catch {
      setStatusText(helperCommand);
    }
  };

  const copyAgentStartCommand = async () => {
    try {
      await navigator.clipboard.writeText(agentStartCommand);
      setStatusText("Copied agent start command.");
    } catch {
      setStatusText(agentStartCommand);
    }
  };

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_10%_0%,#d0f4ea_0%,#f4f8ff_35%,#fcfcff_100%)] px-4 py-6 text-slate-900 md:px-8">
      <main className="mx-auto grid w-full max-w-[1400px] gap-4 lg:grid-cols-[300px_1fr]">
        <section className="rounded-2xl border border-white/70 bg-white/80 p-4 shadow-[0_14px_60px_-28px_rgba(9,30,66,0.35)] backdrop-blur">
          <h1 className="font-['Space_Grotesk',sans-serif] text-xl font-semibold">
            Meeting Assistant
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Sessions are persisted on backend DB. Choose one to watch live transcript and suggestions.
          </p>

          <div className="mt-4 space-y-2">
            <label className="text-xs font-medium uppercase tracking-wide text-slate-500">
              New Session Title
            </label>
            <input
              className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-teal-500"
              value={sessionTitle}
              onChange={(e) => setSessionTitle(e.target.value)}
              placeholder="Interview prep"
            />
            <button
              className="w-full rounded-lg bg-teal-700 px-3 py-2 text-sm font-semibold text-white transition hover:bg-teal-800"
              onClick={handleCreateSession}
            >
              Create Session
            </button>
          </div>

          <div className="mt-4">
            <div className="mb-4">
              <div className="mb-2 flex items-center justify-between">
                <h2 className="text-sm font-semibold">Live Sessions</h2>
                <button
                  className="rounded-md border border-slate-200 px-2 py-1 text-xs"
                  onClick={() => void fetchLiveSessions()}
                >
                  Refresh
                </button>
              </div>
              <div className="max-h-[22vh] space-y-2 overflow-auto pr-1">
                {liveSessions.map((item) => (
                  <button
                    key={`live-${item.id}`}
                    onClick={() => setActiveSessionId(item.id)}
                    className={`w-full rounded-lg border px-3 py-2 text-left transition ${
                      activeSessionId === item.id
                        ? "border-teal-600 bg-teal-50"
                        : "border-slate-200 bg-white hover:border-slate-300"
                    }`}
                  >
                    <div className="truncate text-sm font-semibold">{item.title || "Untitled Session"}</div>
                    <div className="mt-1 text-xs text-slate-600">
                      capture: {item.capture_socket_count ?? 0} | sockets: {item.socket_count ?? 0} |{" "}
                      {item.provider}/{item.model}
                    </div>
                    <div className="mt-1 truncate font-mono text-[11px] text-slate-500">{item.id}</div>
                  </button>
                ))}
                {liveSessions.length === 0 && (
                  <p className="rounded-lg border border-dashed border-slate-300 p-3 text-xs text-slate-500">
                    No live sessions running.
                  </p>
                )}
              </div>
            </div>
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-sm font-semibold">Saved Sessions</h2>
              <button
                className="rounded-md border border-slate-200 px-2 py-1 text-xs"
                onClick={() => void fetchSessions()}
              >
                Refresh
              </button>
            </div>
            <div className="max-h-[55vh] space-y-2 overflow-auto pr-1">
              {sessions.map((item) => (
                <div
                  key={item.id}
                  className={`rounded-lg border px-3 py-2 transition ${
                    activeSessionId === item.id
                      ? "border-teal-600 bg-teal-50"
                      : "border-slate-200 bg-white hover:border-slate-300"
                  }`}
                >
                  <button className="w-full text-left" onClick={() => setActiveSessionId(item.id)}>
                    <div className="truncate text-sm font-semibold">{item.title || "Untitled Session"}</div>
                    <div className="mt-1 text-xs text-slate-600">
                      {item.provider}/{item.model}
                    </div>
                    <div className="mt-1 truncate font-mono text-[11px] text-slate-500">{item.id}</div>
                    <div className="mt-1 text-xs text-slate-500">
                      {item.transcript_count ?? 0} transcript, {item.suggestion_count ?? 0} suggestions
                    </div>
                  </button>
                  <div className="mt-2 flex justify-end">
                    <button
                      className="rounded border border-rose-300 px-2 py-1 text-[11px] text-rose-700 hover:bg-rose-50"
                      onClick={() => void handleDeleteSession(item.id)}
                    >
                      Delete
                    </button>
                  </div>
                </div>
              ))}
              {sessions.length === 0 && (
                <p className="rounded-lg border border-dashed border-slate-300 p-3 text-xs text-slate-500">
                  No sessions yet.
                </p>
              )}
            </div>
          </div>
        </section>

        <section className="grid gap-4">
          <div className="rounded-2xl border border-white/70 bg-white/80 p-4 shadow-[0_14px_60px_-28px_rgba(9,30,66,0.35)] backdrop-blur">
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
              <div className="xl:col-span-2">
                <label className="text-xs font-medium uppercase tracking-wide text-slate-500">Provider</label>
                <select
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                >
                  <option value="openai">openai</option>
                  <option value="anthropic">anthropic</option>
                  <option value="mock">mock</option>
                </select>
              </div>
              <div className="xl:col-span-2">
                <label className="text-xs font-medium uppercase tracking-wide text-slate-500">Model</label>
                <input
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                />
              </div>
              <div>
                <label className="text-xs font-medium uppercase tracking-wide text-slate-500">History Mode</label>
                <select
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                  value={historyMode}
                  onChange={(e) => setHistoryMode(e.target.value as HistoryMode)}
                >
                  <option value="focused">focused</option>
                  <option value="full">full</option>
                  <option value="stateless">stateless</option>
                </select>
              </div>
              <div>
                <label className="text-xs font-medium uppercase tracking-wide text-slate-500">History Lines</label>
                <input
                  type="number"
                  min={1}
                  max={40}
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                  value={historyLines}
                  onChange={(e) => setHistoryLines(Number(e.target.value))}
                />
              </div>
            </div>
            <div className="mt-3">
              <label className="text-xs font-medium uppercase tracking-wide text-slate-500">Session Context</label>
              <textarea
                className="mt-1 h-20 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                value={context}
                onChange={(e) => setContext(e.target.value)}
                placeholder="Role, goal, agenda, constraints..."
              />
            </div>
            <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
              <div className="mb-3 rounded-lg border border-slate-200 bg-white p-3">
                <h4 className="text-sm font-semibold">Run Options</h4>
                <p className="mt-1 text-xs text-slate-600">
                  You do not need VSCode open. Use one of these run modes.
                </p>
                <div className="mt-2 grid gap-2 md:grid-cols-2">
                  <div className="rounded-md border border-slate-200 bg-slate-50 p-2">
                    <div className="text-xs font-semibold uppercase tracking-wide text-slate-600">
                      1) Local Python (.venv)
                    </div>
                    <code className="mt-1 block rounded bg-white p-2 text-[11px]">
                      {agentStartCommand}
                    </code>
                    <button
                      className="mt-2 rounded border border-slate-300 px-2 py-1 text-xs"
                      onClick={copyAgentStartCommand}
                    >
                      Copy Command
                    </button>
                  </div>
                  <div className="rounded-md border border-slate-200 bg-slate-50 p-2">
                    <div className="text-xs font-semibold uppercase tracking-wide text-slate-600">
                      2) Desktop Agent App
                    </div>
                    <p className="mt-1 text-[11px] text-slate-600">
                      Standalone packaged app with mic/system selectors and start/stop capture.
                    </p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      <a
                        className="rounded border border-slate-300 px-2 py-1 text-xs"
                        href="/downloads/MeetingAssistantDesktopAgent-standalone.exe?v=20260311c"
                        download
                      >
                        Download Windows .exe
                      </a>
                    </div>
                  </div>
                </div>
                <label className="mt-2 flex items-center gap-2 text-xs text-slate-700">
                  <input
                    type="checkbox"
                    checked={autoStopOnTabClose}
                    onChange={(e) => setAutoStopOnTabClose(e.target.checked)}
                  />
                  Auto-stop capture when this browser tab closes
                </label>
              </div>
              <div className="mb-2 flex items-center justify-between">
                <h4 className="text-sm font-semibold">Audio Source Selection (Local Helper)</h4>
                <div className="flex items-center gap-2">
                  <span
                    className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase ${
                      agentOnline ? "bg-emerald-100 text-emerald-800" : "bg-slate-200 text-slate-700"
                    }`}
                  >
                    helper {agentOnline ? "online" : "offline"}
                  </span>
                  <button
                    className="rounded-md border border-slate-300 px-2 py-1 text-xs"
                    onClick={() => void fetchAudioDevices()}
                  >
                    Refresh Devices
                  </button>
                </div>
              </div>
              <p className="mb-2 text-xs text-slate-600">
                By default this shows a curated short list (WASAPI/stereo-mix preferred) so it is closer to meeting app settings.
              </p>
              <label className="mb-2 flex items-center gap-2 text-xs text-slate-700">
                <input
                  type="checkbox"
                  checked={showAllDevices}
                  onChange={(e) => setShowAllDevices(e.target.checked)}
                />
                Show full raw device list
              </label>
              <div className="grid gap-2 md:grid-cols-2">
                <div>
                  <label className="text-xs font-medium uppercase tracking-wide text-slate-500">
                    Mic Device
                  </label>
                  <select
                    className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                    value={selectedMicDevice}
                    onChange={(e) => setSelectedMicDevice(e.target.value)}
                  >
                    <option value="">auto</option>
                    {micDevices.map((item) => (
                      <option key={`mic-${item.id}`} value={item.id}>
                        {item.id} - {item.name} ({item.hostapi})
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="text-xs font-medium uppercase tracking-wide text-slate-500">
                    System Device
                  </label>
                  <select
                    className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                    value={selectedSystemDevice}
                    onChange={(e) => setSelectedSystemDevice(e.target.value)}
                  >
                    <option value="">auto</option>
                    {systemDevices.map((item) => (
                      <option key={`sys-${item.id}`} value={item.id}>
                        {item.id} - {item.name} ({item.hostapi})
                        {item.is_stereo_mix_like ? " [recommended]" : ""}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              {audioDeviceError && (
                <p className="mt-2 text-xs text-rose-700">
                  Device list unavailable: {audioDeviceError}
                </p>
              )}
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  className="rounded-lg bg-teal-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
                  onClick={startLocalCapture}
                  disabled={!agentOnline || !activeSessionId}
                >
                  Start Capture
                </button>
                <button
                  className="rounded-lg border border-slate-400 px-3 py-2 text-xs font-semibold"
                  onClick={stopLocalCapture}
                  disabled={!agentOnline || !captureRunning}
                >
                  Stop Capture
                </button>
                <span
                  className={`rounded-full px-2 py-1 text-[10px] font-semibold uppercase ${
                    captureRunning ? "bg-emerald-100 text-emerald-800" : "bg-slate-200 text-slate-700"
                  }`}
                >
                  capture {captureRunning ? "running" : "stopped"}
                </span>
              </div>
              <div className="mt-3">
                <label className="text-xs font-medium uppercase tracking-wide text-slate-500">
                  Helper Command
                </label>
                <div className="mt-1 flex flex-wrap items-center gap-2">
                  <code className="max-w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] leading-5">
                    {helperCommand}
                  </code>
                  <button
                    className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold"
                    onClick={copyHelperCommand}
                  >
                    Copy Command
                  </button>
                </div>
              </div>
              {captureLogs.length > 0 && (
                <div className="mt-3">
                  <label className="text-xs font-medium uppercase tracking-wide text-slate-500">
                    Helper Logs
                  </label>
                  <pre className="mt-1 max-h-32 overflow-auto rounded-lg border border-slate-200 bg-white p-2 text-[11px] leading-5 text-slate-700">
{captureLogs.slice(-20).join("\n")}
                  </pre>
                </div>
              )}
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button
                className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold"
                onClick={copyActiveSessionId}
                disabled={!activeSessionId}
              >
                Copy Session ID
              </button>
              <button
                className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white"
                onClick={handleSaveConfig}
              >
                Save Config
              </button>
              <span
                className={`rounded-full px-3 py-1 text-xs font-medium ${
                  status === "connected"
                    ? "bg-emerald-100 text-emerald-800"
                    : status === "connecting"
                      ? "bg-amber-100 text-amber-800"
                      : "bg-slate-100 text-slate-700"
                }`}
              >
                {status}
              </span>
              <span className="text-xs text-slate-500">{statusText}</span>
              {errorText && <span className="text-xs text-rose-700">Error: {errorText}</span>}
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-[1fr_1fr]">
            <div className="rounded-2xl border border-white/70 bg-white/80 p-4 shadow-[0_14px_60px_-28px_rgba(9,30,66,0.35)] backdrop-blur">
              <h3 className="font-['Space_Grotesk',sans-serif] text-lg font-semibold">Live Transcript</h3>
              <div className="mt-3 max-h-[48vh] space-y-2 overflow-auto pr-1">
                {events
                  .filter((event) => event.type === "transcript")
                  .map((event, idx) => {
                    const item = event as TranscriptEvent;
                    return (
                      <div key={`${item.created_at}-${idx}`} className="rounded-lg border border-slate-200 bg-white p-2">
                        <div className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
                          {item.source} - {prettyTime(item.created_at)}
                        </div>
                        <div className="mt-1 text-sm">{item.text}</div>
                      </div>
                    );
                  })}
              </div>
            </div>

            <div className="rounded-2xl border border-white/70 bg-white/80 p-4 shadow-[0_14px_60px_-28px_rgba(9,30,66,0.35)] backdrop-blur">
              <h3 className="font-['Space_Grotesk',sans-serif] text-lg font-semibold">Latest Suggestion</h3>
              <div className="mt-3 rounded-lg border border-teal-100 bg-teal-50/40 p-3">
                {latestSuggestion ? (
                  <>
                    <div className="text-[11px] font-medium uppercase tracking-wide text-teal-700">
                      {latestSuggestion.provider}/{latestSuggestion.model} - {latestSuggestion.latency_ms}ms
                    </div>
                    <div className="mt-2 whitespace-pre-wrap text-sm">{latestSuggestion.text}</div>
                  </>
                ) : (
                  <div className="text-sm text-slate-500">No suggestion yet.</div>
                )}
              </div>
              <h4 className="mt-4 text-sm font-semibold">Manual Test Input</h4>
              <div className="mt-2 grid grid-cols-[130px_1fr_auto] gap-2">
                <select
                  className="rounded-lg border border-slate-200 px-2 py-2 text-sm"
                  value={manualSource}
                  onChange={(e) => setManualSource(e.target.value as "mic" | "system")}
                >
                  <option value="mic">mic</option>
                  <option value="system">system</option>
                </select>
                <input
                  className="rounded-lg border border-slate-200 px-3 py-2 text-sm"
                  value={manualText}
                  onChange={(e) => setManualText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      sendManualTranscript();
                    }
                  }}
                  placeholder="Type a transcript line and send..."
                />
                <button
                  className="rounded-lg bg-teal-700 px-3 py-2 text-sm font-semibold text-white"
                  onClick={sendManualTranscript}
                >
                  Send
                </button>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
