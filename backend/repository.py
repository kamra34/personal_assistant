from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import SessionRecord, SuggestionRecord, TranscriptRecord


def now_utc() -> datetime:
    return datetime.now(UTC)


async def get_or_create_session(
    db: AsyncSession,
    session_id: str,
) -> SessionRecord:
    existing = await db.get(SessionRecord, session_id)
    if existing is not None:
        return existing
    created = SessionRecord(id=session_id)
    db.add(created)
    await db.commit()
    await db.refresh(created)
    return created


async def create_session(
    db: AsyncSession,
    *,
    title: str | None,
    context: str,
    provider: str,
    model: str,
    history_mode: str,
    history_lines: int,
) -> SessionRecord:
    session_id = uuid.uuid4().hex[:16]
    record = SessionRecord(
        id=session_id,
        title=(title or "Untitled Session").strip()[:240],
        context=context.strip(),
        provider=provider.strip() or "mock",
        model=model.strip() or "gpt-4o-mini",
        history_mode=history_mode.strip() or "focused",
        history_lines=max(1, min(history_lines, 40)),
        updated_at=now_utc(),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def update_session_config(
    db: AsyncSession,
    *,
    session_id: str,
    provider: str,
    model: str,
    context: str,
    history_mode: str,
    history_lines: int,
) -> SessionRecord:
    record = await get_or_create_session(db, session_id=session_id)
    record.provider = provider
    record.model = model
    record.context = context
    record.history_mode = history_mode
    record.history_lines = history_lines
    record.updated_at = now_utc()
    await db.commit()
    await db.refresh(record)
    return record


async def add_transcript(
    db: AsyncSession,
    *,
    session_id: str,
    source: str,
    text: str,
) -> None:
    await get_or_create_session(db, session_id)
    db.add(
        TranscriptRecord(
            session_id=session_id,
            source=source,
            text=text,
        )
    )
    session_obj = await db.get(SessionRecord, session_id)
    if session_obj:
        session_obj.updated_at = now_utc()
    await db.commit()


async def add_suggestion(
    db: AsyncSession,
    *,
    session_id: str,
    provider: str,
    model: str,
    latency_ms: int,
    text: str,
) -> None:
    await get_or_create_session(db, session_id)
    db.add(
        SuggestionRecord(
            session_id=session_id,
            provider=provider,
            model=model,
            latency_ms=latency_ms,
            text=text,
        )
    )
    session_obj = await db.get(SessionRecord, session_id)
    if session_obj:
        session_obj.updated_at = now_utc()
    await db.commit()


async def list_sessions(db: AsyncSession, limit: int = 50) -> list[dict[str, Any]]:
    stmt: Select[tuple[SessionRecord]] = (
        select(SessionRecord)
        .order_by(desc(SessionRecord.updated_at))
        .limit(max(1, min(limit, 200)))
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        t_count = await db.scalar(
            select(func.count(TranscriptRecord.id)).where(TranscriptRecord.session_id == row.id)
        )
        s_count = await db.scalar(
            select(func.count(SuggestionRecord.id)).where(SuggestionRecord.session_id == row.id)
        )
        out.append(
            {
                "id": row.id,
                "title": row.title,
                "context": row.context,
                "provider": row.provider,
                "model": row.model,
                "history_mode": row.history_mode,
                "history_lines": row.history_lines,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
                "transcript_count": int(t_count or 0),
                "suggestion_count": int(s_count or 0),
            }
        )
    return out


async def get_session(db: AsyncSession, session_id: str) -> dict[str, Any] | None:
    row = await db.get(SessionRecord, session_id)
    if row is None:
        return None
    return {
        "id": row.id,
        "title": row.title,
        "context": row.context,
        "provider": row.provider,
        "model": row.model,
        "history_mode": row.history_mode,
        "history_lines": row.history_lines,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


async def get_events(
    db: AsyncSession,
    *,
    session_id: str,
    limit: int = 300,
) -> list[dict[str, Any]]:
    capped = max(1, min(limit, 1000))
    transcripts = (
        await db.execute(
            select(TranscriptRecord)
            .where(TranscriptRecord.session_id == session_id)
            .order_by(desc(TranscriptRecord.created_at))
            .limit(capped)
        )
    ).scalars().all()
    suggestions = (
        await db.execute(
            select(SuggestionRecord)
            .where(SuggestionRecord.session_id == session_id)
            .order_by(desc(SuggestionRecord.created_at))
            .limit(capped)
        )
    ).scalars().all()

    events: list[dict[str, Any]] = []
    for row in transcripts:
        events.append(
            {
                "type": "transcript",
                "id": row.id,
                "session_id": row.session_id,
                "source": row.source,
                "text": row.text,
                "created_at": row.created_at.isoformat(),
            }
        )
    for row in suggestions:
        events.append(
            {
                "type": "suggestion",
                "id": row.id,
                "session_id": row.session_id,
                "provider": row.provider,
                "model": row.model,
                "latency_ms": row.latency_ms,
                "text": row.text,
                "created_at": row.created_at.isoformat(),
            }
        )
    events.sort(key=lambda x: x["created_at"])
    if len(events) > capped:
        events = events[-capped:]
    return events

