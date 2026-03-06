from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .session import LiveSession

BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Meeting Assistant MVP")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
sessions: dict[str, LiveSession] = {}


def get_or_create_session(session_id: str) -> LiveSession:
    if session_id not in sessions:
        sessions[session_id] = LiveSession(session_id=session_id)
    return sessions[session_id]


async def broadcast(session: LiveSession, payload: dict[str, Any]) -> None:
    stale: list[WebSocket] = []
    for ws in session.sockets:
        try:
            await ws.send_json(payload)
        except RuntimeError:
            stale.append(ws)
    for ws in stale:
        session.sockets.discard(ws)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.websocket("/ws/{session_id}")
async def session_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    session = get_or_create_session(session_id)
    session.sockets.add(websocket)
    await websocket.send_json(
        {
            "type": "status",
            "session_id": session_id,
            "message": f"Connected to session '{session_id}'.",
        }
    )
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = (data.get("type") or "").strip().lower()
            if msg_type == "configure":
                session.configure(data)
                await broadcast(
                    session,
                    {
                        "type": "status",
                        "session_id": session_id,
                        "message": (
                            f"Configured provider={session.provider_name} "
                            f"model={session.model} "
                            f"history_mode={session.history_mode}"
                        ),
                    },
                )
                continue
            if msg_type == "transcript":
                text = (data.get("text") or "").strip()
                if not text:
                    continue
                source = (data.get("source") or "unknown").strip()
                final = bool(data.get("final", True))
                session.add_transcript(source=source, text=text)
                if final:
                    try:
                        suggestion = await session.generate_suggestion(
                            latest_source=source,
                            latest_text=text,
                        )
                        await broadcast(session, suggestion)
                    except Exception as exc:  # pragma: no cover - best effort reporting
                        await websocket.send_json(
                            {"type": "error", "message": f"Generation failed: {exc}"}
                        )
                continue
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            await websocket.send_json({"type": "error", "message": "Unknown message type."})
    except WebSocketDisconnect:
        session.sockets.discard(websocket)


def run() -> None:
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )


if __name__ == "__main__":
    run()
