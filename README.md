# Meeting Assistant MVP

Vercel-compatible frontend + Railway-compatible backend for live meeting assistance.

## Architecture

- `web/`:
  - Next.js (App Router) dashboard
  - Session list, persisted timeline, live suggestion panel
  - WebSocket + REST integration with backend
- `backend/`:
  - FastAPI API + WebSocket server
  - Provider routing (`mock`, `openai`, `anthropic`)
  - Persistent storage for sessions, transcripts, and suggestions
- `helper/`:
  - `local_helper.py` for manual transcript testing
  - `ui_agent.py` for local helper API (devices + start/stop capture)
  - `desktop_agent.py` native desktop UI for local capture control
  - `audio_capture_windows.py` for mic + system audio capture and STT
  - `audio_devices.py` shared device ranking/dedup logic

## Local setup

### 1) Python backend and helpers

```powershell
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
Copy-Item .env.example .env
```

### 2) Frontend

```powershell
cd web
npm install
Copy-Item .env.example .env.local
```

## Run locally

### Terminal A: backend

```powershell
.venv\Scripts\Activate.ps1
.venv\Scripts\python -m uvicorn backend.main:app --reload
```

Backend runs at `http://127.0.0.1:8000`.

Alternative (same app entrypoint):

```powershell
python -m backend.main
```

### Terminal B: Next.js web UI

```powershell
cd web
npm run dev
```

Frontend runs at `http://localhost:3000`.

In the web UI:
- See **Live Sessions** (or `No live sessions running.` when none).
- Use **Audio Source Selection (Local Helper)** to pick mic/system devices from Windows device list.
- Start/stop local capture directly if Helper Agent is running.
- Manage **Saved Sessions** including delete.
- Use **Run Options** to either copy the local helper command or download the standalone desktop app.
- Or copy the generated helper command and run it in terminal.

### Terminal C: Desktop Agent (recommended)

```powershell
.venv\Scripts\Activate.ps1
python helper\desktop_agent.py
```

Desktop Agent auto-starts the local Helper Agent in background (when needed), so web capture controls work without manually running `python -m helper.ui_agent`.
Helper Agent endpoint is `http://127.0.0.1:8765` by default.

### Desktop Agent App (downloadable)

From the web UI Run Options, download:

- `MeetingAssistantDesktopAgent-standalone.exe`

Notes:
- First launch can be slower on corporate Windows due one-file extraction + endpoint security scan.
- The app can remember local settings and key on the current device.

### Alternative: Desktop Agent Window (mic/system selector UI)

```powershell
.venv\Scripts\Activate.ps1
python helper\desktop_agent.py
```

This opens a native window to:
- create a new backend session directly (no web-first requirement)
- list and join live sessions from backend
- list, join, and delete saved sessions
- choose mic/system devices
- set session/provider/model/history mode
- set `OPENAI_API_KEY` locally inside the app (for transcription)
- optionally store key/settings locally using `Remember key on this device`
- start/stop capture and view logs
- open web dashboard directly on the same session (`?session=<id>`)
- view live session transcripts/suggestions in the desktop `Conversation` tab

Local settings path:
- Windows: `%APPDATA%\MeetingAssistant\desktop_agent.json`
- Non-Windows: `~/.meeting_assistant_desktop_agent.json`

Note: live capture backend is currently Windows-only in this MVP.

### Terminal C (optional): manual transcript helper

```powershell
.venv\Scripts\Activate.ps1
python helper\local_helper.py --session-id <session-id-from-ui> --provider openai --model gpt-4o-mini --history-mode focused
```

### Terminal C (optional): Windows audio helper

List devices:

```powershell
.venv\Scripts\Activate.ps1
python helper\audio_capture_windows.py --list-devices
```

Run live capture:

```powershell
python helper\audio_capture_windows.py --session-id <session-id-from-ui> --provider openai --model gpt-4o-mini --history-mode focused
```

Note: if backend reloads/restarts during capture (for example local dev with auto-reload), the helper now auto-reconnects with backoff and continues streaming.

If needed, pin devices:

```powershell
python helper\audio_capture_windows.py --session-id <session-id-from-ui> --provider openai --model gpt-4o-mini --history-mode focused --mic-device 15 --system-device 22
```

## Environment variables

### Backend (`.env`)

- `ASSISTANT_HOST=127.0.0.1`
- `ASSISTANT_PORT=8000`
- `DATABASE_URL=sqlite+aiosqlite:///./assistant.db` (local default)
- `CORS_ORIGINS=*` (set explicit frontend domain in production)
- `OPENAI_API_KEY=...`
- `ANTHROPIC_API_KEY=...`
- `OPENAI_BASE_URL=https://api.openai.com/v1`
- STT/audio tuning variables from `.env.example`

Key low-latency VAD controls:
- `AUDIO_END_SILENCE_SECONDS` + `AUDIO_HANGOVER_SECONDS`: controls when an utterance is finalized.
- `AUDIO_MIN_START_SECONDS`: reduces false starts from brief noises.
- `AUDIO_PRE_ROLL_SECONDS`: keeps a small lead-in so sentence starts are not cut.
- `AUDIO_ADAPTIVE_NOISE`, `AUDIO_START_RMS_RATIO`, `AUDIO_END_RMS_RATIO`: adapt thresholds to room noise.
- `AUDIO_QUEUE_SIZE`, `AUDIO_TRANSCRIBE_QUEUE_SIZE`: prevent audio frame drops during slower STT/network periods.

### Frontend (`web/.env.local`)

- `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000` (local)
- `NEXT_PUBLIC_HELPER_AGENT_BASE_URL=http://127.0.0.1:8765` (local)

## Railway + Vercel deployment

### Backend on Railway

1. Deploy repository service using Python.
2. Set start command:
   - `python -m backend.main`
3. Set Railway environment variables:
   - `DATABASE_URL` (Railway Postgres URL)
   - `OPENAI_API_KEY`
   - `CORS_ORIGINS=https://<your-vercel-domain>`

### Frontend on Vercel

1. Import repository in Vercel.
2. Set project root to `web`.
3. Set environment variable:
   - `NEXT_PUBLIC_API_BASE_URL=https://<your-railway-backend-domain>`

## Data model (MVP)

- `sessions`:
  - id, title, context, provider, model, history_mode, history_lines, created_at, updated_at
- `transcripts`:
  - id, session_id, source, text, created_at
- `suggestions`:
  - id, session_id, provider, model, latency_ms, text, created_at

## Audio device API

- `GET /api/audio/devices`
  - Returns local host audio devices (id, name, hostapi, channel capabilities)
  - Used by frontend dropdowns to build helper run command
- `GET http://127.0.0.1:8765/api/devices` (Helper Agent)
  - Returns curated mic/system device lists with de-duplicated host APIs
  - Better match to what users expect from meeting app audio settings

## Desktop App Packaging

Build Windows `.exe`:

```powershell
.venv\Scripts\Activate.ps1
powershell -ExecutionPolicy Bypass -File helper\packaging\build_windows_desktop_agent.ps1
```

Build macOS `.app` (run on macOS):

```bash
chmod +x helper/packaging/build_macos_desktop_agent.sh
helper/packaging/build_macos_desktop_agent.sh
```

Build outputs are copied to `web/public/downloads/` for UI download links.
Current Windows output names:
- `MeetingAssistantDesktopAgent-standalone.exe` (preferred in UI)
- `MeetingAssistantDesktopAgent.exe` (legacy alias)

## AI Contributor Handoff

Use [PROJECT_CONTEXT.md](./PROJECT_CONTEXT.md) when starting a new AI coding tool session.
It contains architecture, runbook, key files, and implementation constraints for faster contribution.

## Next steps

- Add auth and user ownership for sessions
- Add transcript confidence scoring and better endpointing
- Add retrieval over past sessions and tagging/search
