from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import SessionLocal, init_db
from .repository import (
    add_suggestion,
    add_transcript,
    create_session,
    get_events,
    get_or_create_session as db_get_or_create_session,
    get_session,
    list_sessions,
    update_session_config,
)
from .session import LiveSession
from .audio_devices import list_audio_devices

BASE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Meeting Assistant MVP")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
sessions: dict[str, LiveSession] = {}


class SessionCreateIn(BaseModel):
    title: str | None = None
    context: str = ""
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    history_mode: Literal["focused", "full", "stateless"] = "focused"
    history_lines: int = Field(default=10, ge=1, le=40)


class SessionConfigIn(BaseModel):
    context: str | None = None
    provider: str | None = None
    model: str | None = None
    history_mode: Literal["focused", "full", "stateless"] | None = None
    history_lines: int | None = Field(default=None, ge=1, le=40)
    title: str | None = None


async def get_or_create_live_session(session_id: str) -> LiveSession:
    existing = sessions.get(session_id)
    if existing is not None:
        return existing

    async with SessionLocal() as db:
        record = await db_get_or_create_session(db, session_id)

    live = LiveSession(session_id=session_id)
    live.configure(
        {
            "context": record.context,
            "provider": record.provider,
            "model": record.model,
            "history_mode": record.history_mode,
            "history_lines": record.history_lines,
        }
    )
    sessions[session_id] = live
    return live


async def broadcast(session: LiveSession, payload: dict[str, Any]) -> None:
    stale: list[WebSocket] = []
    for ws in session.sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            stale.append(ws)
    for ws in stale:
        session.sockets.discard(ws)


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/audio/devices")
def api_audio_devices() -> dict[str, Any]:
    return list_audio_devices()


@app.get("/api/sessions")
async def api_list_sessions(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    async with SessionLocal() as db:
        items = await list_sessions(db, limit=limit)
    return {"items": items}


@app.post("/api/sessions")
async def api_create_session(payload: SessionCreateIn) -> dict[str, Any]:
    async with SessionLocal() as db:
        record = await create_session(
            db,
            title=payload.title,
            context=payload.context,
            provider=payload.provider,
            model=payload.model,
            history_mode=payload.history_mode,
            history_lines=payload.history_lines,
        )
        item = await get_session(db, record.id)
    if item is None:
        raise HTTPException(status_code=500, detail="Failed to create session.")
    return item


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str) -> dict[str, Any]:
    async with SessionLocal() as db:
        item = await get_session(db, session_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return item


@app.patch("/api/sessions/{session_id}/config")
async def api_update_session_config(session_id: str, payload: SessionConfigIn) -> dict[str, Any]:
    async with SessionLocal() as db:
        existing = await db_get_or_create_session(db, session_id)
        if payload.title is not None:
            existing.title = payload.title.strip()[:240] or existing.title
        updated = await update_session_config(
            db,
            session_id=session_id,
            provider=(payload.provider or existing.provider).strip(),
            model=(payload.model or existing.model).strip(),
            context=payload.context if payload.context is not None else existing.context,
            history_mode=payload.history_mode or existing.history_mode,
            history_lines=payload.history_lines or existing.history_lines,
        )
        item = {
            "id": updated.id,
            "title": existing.title,
            "context": updated.context,
            "provider": updated.provider,
            "model": updated.model,
            "history_mode": updated.history_mode,
            "history_lines": updated.history_lines,
            "created_at": updated.created_at.isoformat(),
            "updated_at": updated.updated_at.isoformat(),
        }

    live = sessions.get(session_id)
    if live is not None:
        live.configure(
            {
                "context": item["context"],
                "provider": item["provider"],
                "model": item["model"],
                "history_mode": item["history_mode"],
                "history_lines": item["history_lines"],
            }
        )
    return item


@app.get("/api/sessions/{session_id}/events")
async def api_get_events(
    session_id: str,
    limit: int = Query(default=300, ge=1, le=1000),
) -> dict[str, Any]:
    async with SessionLocal() as db:
        item = await get_session(db, session_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        events = await get_events(db, session_id=session_id, limit=limit)
    return {"session": item, "events": events}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.websocket("/ws/{session_id}")
async def session_ws(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    session = await get_or_create_live_session(session_id)
    session.sockets.add(websocket)
    await websocket.send_json(
        {
            "type": "status",
            "session_id": session_id,
            "message": f"Connected to session '{session_id}'.",
        }
    )
    await websocket.send_json(
        {
            "type": "session_meta",
            "session_id": session_id,
            "provider": session.provider_name,
            "model": session.model,
            "context": session.context,
            "history_mode": session.history_mode,
            "history_lines": session.history_lines,
        }
    )
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = (data.get("type") or "").strip().lower()
            if msg_type == "configure":
                session.configure(data)
                async with SessionLocal() as db:
                    await update_session_config(
                        db,
                        session_id=session_id,
                        provider=session.provider_name,
                        model=session.model,
                        context=session.context,
                        history_mode=session.history_mode,
                        history_lines=session.history_lines,
                    )
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
                async with SessionLocal() as db:
                    await add_transcript(
                        db,
                        session_id=session_id,
                        source=source,
                        text=text,
                    )
                await broadcast(
                    session,
                    {
                        "type": "transcript",
                        "session_id": session_id,
                        "source": source,
                        "text": text,
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                )
                if final:
                    try:
                        suggestion = await session.generate_suggestion(
                            latest_source=source,
                            latest_text=text,
                        )
                        async with SessionLocal() as db:
                            await add_suggestion(
                                db,
                                session_id=session_id,
                                provider=suggestion["provider"],
                                model=suggestion["model"],
                                latency_ms=int(suggestion["latency_ms"]),
                                text=suggestion["text"],
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
