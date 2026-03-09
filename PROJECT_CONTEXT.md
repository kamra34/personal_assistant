# Project Context And Contributor Runbook

This file is intended for new AI coding sessions to onboard quickly and make safe, useful changes.

## 1) What This Project Is

Meeting Assistant MVP:
- Live meeting helper that ingests transcript events (`mic` + `system`) and returns short suggested responses.
- Web dashboard for session management, transcript/suggestion timeline, and helper controls.
- Local Windows capture tooling for mic + system audio with STT, then transcript streaming to backend.

Current stack:
- Backend: FastAPI + WebSocket + SQLAlchemy async
- Frontend: Next.js App Router (`web/`)
- Local helper: Python scripts (`helper/`)
- DB: SQLite local by default, Postgres intended in production (Railway)

## 2) Current Product Scope

Implemented:
- Persistent sessions/transcripts/suggestions
- Live websocket suggestion loop
- Provider routing: `mock`, `openai`, `anthropic`
- Helper Agent HTTP API for device list + capture start/stop/status
- Desktop Agent app (native UI) with:
  - mic/system device selection
  - session/provider/model/history settings
  - local OpenAI key input for STT
  - optional local key/settings persistence

Not implemented yet:
- Auth/login
- User ownership / multi-tenant security model
- Per-user encrypted provider key vault in backend DB
- Backend-side audio transcription pipeline (currently STT is local in helper)

## 3) Repository Map

- `backend/`
  - `main.py`: REST + WS endpoints
  - `session.py`: prompt/context/suggestion orchestration
  - `providers.py`: model provider adapters
  - `db.py`, `models.py`, `repository.py`: persistence layer
- `helper/`
  - `ui_agent.py`: local helper API (`/api/devices`, `/api/capture/*`)
  - `desktop_agent.py`: native desktop control app
  - `audio_capture_windows.py`: audio capture + STT + WS transcript sender
  - `audio_devices.py`: curated/normalized device list logic
  - `packaging/build_windows_desktop_agent.ps1`: Desktop Agent build script
- `web/`
  - `src/app/page.tsx`: main dashboard page
  - `public/downloads/`: downloadable desktop agent artifacts

## 4) Local Development Runbook (Windows)

Backend + helper setup:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Frontend setup:

```powershell
cd web
npm install
Copy-Item .env.example .env.local
```

Run:

```powershell
# Terminal A
.venv\Scripts\Activate.ps1
python -m backend.main

# Terminal B
cd web
npm run dev

# Terminal C (optional helper agent)
.venv\Scripts\Activate.ps1
python -m helper.ui_agent
```

## 5) Ports And URLs

- Backend API/WS: `http://127.0.0.1:8000`
- Frontend: `http://localhost:3000`
- Helper Agent: `http://127.0.0.1:8765`

## 6) Environment Variables

Backend (`.env`):
- `ASSISTANT_HOST`, `ASSISTANT_PORT`
- `DATABASE_URL`
- `CORS_ORIGINS`
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`
- `OPENAI_BASE_URL`
- STT tuning vars (see `.env.example`)

Frontend (`web/.env.local`):
- `NEXT_PUBLIC_API_BASE_URL`
- `NEXT_PUBLIC_HELPER_AGENT_BASE_URL`

## 7) Desktop Agent Notes

- Download artifact used by UI: `web/public/downloads/MeetingAssistantDesktopAgent-standalone.exe`
- First launch may be slower due one-file extraction + endpoint security scans.
- Desktop settings file:
  - Windows: `%APPDATA%\MeetingAssistant\desktop_agent.json`
  - Other OS: `~/.meeting_assistant_desktop_agent.json`
- STT currently requires OpenAI key for local transcription when `STT_PROVIDER=openai`.

## 8) Current Architectural Constraint (Important)

Per-user backend API keys are not complete yet because auth/user model is not implemented.

If you add backend key storage:
- Do not expose raw keys without auth + authorization.
- Prefer encrypted-at-rest storage with server-side decryption only when needed.
- For true keyless desktop flow, move STT call server-side or proxy STT through backend.

## 9) Coding Guidelines For Contributions

- Keep changes minimal and targeted.
- Preserve existing API contracts unless coordinated frontend/backend updates are included.
- Prefer adding small utilities over duplicating logic (example: shared audio device logic).
- Validate changes:
  - `python -m compileall helper backend`
  - `cd web && npm run lint`
  - `cd web && npm run build` for significant frontend changes

## 10) Suggested Next Milestones

1. Add auth and user table.
2. Add per-user encrypted provider key storage.
3. Route generation provider keys via user context instead of global `.env`.
4. Decide STT strategy:
   - keep local with remembered key
   - or move STT server-side for keyless local app
5. Improve endpointing/VAD and transcript quality.
