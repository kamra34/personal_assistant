# Web Frontend

## Local setup

```powershell
npm install
Copy-Item .env.example .env.local
npm run dev
```

Optional desktop agent window (Windows/macOS script mode):

```powershell
.venv\Scripts\Activate.ps1
python helper\desktop_agent.py
```

In Desktop Agent, set `OPENAI_API_KEY` in the app before starting capture if it is not already in your environment.

## Required env

- `NEXT_PUBLIC_API_BASE_URL`:
  - Local: `http://127.0.0.1:8000`
  - Production: Railway backend URL
- `NEXT_PUBLIC_HELPER_AGENT_BASE_URL`:
  - Local: `http://127.0.0.1:8765`
  - Used for local device list + start/stop capture from web UI
