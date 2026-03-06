# Meeting Assistant MVP (Web + Tiny Local Helper)

This is a starter implementation for your preferred approach:
- Web app UI for session/model/context control and live suggestions
- Small local helper process that pushes transcript chunks
- FastAPI websocket backend with provider routing (`mock`, `openai`, `anthropic`)
- Windows audio helper that captures mic + system loopback and auto-transcribes chunks

## 1) Environment setup (use your existing `.venv`)

```powershell
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
Copy-Item .env.example .env
```

## 2) Run the backend

```powershell
.venv\Scripts\Activate.ps1
python -m backend.main
```

Open: `http://127.0.0.1:8000`

## 3) Run the tiny local helper (mock transcript mode)

```powershell
.venv\Scripts\Activate.ps1
python helper\local_helper.py --session-id default-room --provider mock --model gpt-4o-mini
```

Type lines like:
- `system: We need a timeline for the migration.`
- `mic: We can deliver phase one by end of quarter.`

You should see live suggestions in both helper terminal and web UI.

## 4) Switch to real model providers (manual transcript mode)

1. Put keys in `.env`:
   - `OPENAI_API_KEY=...`
   - `ANTHROPIC_API_KEY=...`
2. Restart backend.
3. Select provider/model in UI or helper arguments.
4. `History mode` options:
   - `focused` (recommended): answer latest utterance, small rolling context
   - `full`: use full rolling transcript context
   - `stateless`: answer latest utterance without transcript history

## 5) Windows live audio mode (mic + system audio)

1. Ensure `.env` includes:
   - `OPENAI_API_KEY=...`
   - Optional STT settings:
     - `STT_PROVIDER=openai`
     - `STT_MODEL=whisper-1`
     - `STT_LANGUAGE=en`
     - `AUDIO_CHUNK_SECONDS=4`
     - `AUDIO_MIN_RMS=220`

2. List devices and note IDs:
```powershell
.venv\Scripts\Activate.ps1
python helper\audio_capture_windows.py --list-devices
```

3. Run live capture:
```powershell
.venv\Scripts\Activate.ps1
python helper\audio_capture_windows.py --session-id default-room --provider openai --model gpt-4o-mini
```

Optional device pinning:
```powershell
python helper\audio_capture_windows.py --session-id default-room --provider openai --model gpt-4o-mini --mic-device 15 --system-device 22
```

Notes:
- The helper first tries a `Stereo Mix`-like input device for system audio (recommended when available).
- Some `sounddevice`/PortAudio builds do not support direct WASAPI output loopback; in that case use `--system-device` with a system input (typically `Stereo Mix`).
- Use `--list-devices` to find valid IDs for `--mic-device` and `--system-device`.
- Some devices only support specific channel counts. The helper now auto-negotiates a valid sample-rate/channel format (for example your WASAPI mic may require 4 channels).
- If transcription is too noisy, increase:
  - `AUDIO_SPEECH_START_RMS`
  - `AUDIO_SPEECH_END_RMS`
  - `AUDIO_END_SILENCE_SECONDS`
- If replies are delayed too much, decrease `AUDIO_END_SILENCE_SECONDS` and `AUDIO_MIN_UTTERANCE_SECONDS`.
- For interview-style behavior, keep `--history-mode focused` so answers stay on the latest question without forgetting recent context.

## Current scope

- Implemented:
  - websocket sessioning
  - context + transcript aggregation
  - provider abstraction and live suggestion loop
  - Windows mic + system audio capture (Stereo Mix input and WASAPI loopback when supported)
  - chunked speech-to-text forwarding to websocket
- Not implemented yet:
  - diarization
  - smarter VAD/turn detection
  - transcript deduplication and confidence filtering
