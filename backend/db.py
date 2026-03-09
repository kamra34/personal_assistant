from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .models import Base


def _normalized_database_url(url: str) -> str:
    clean = url.strip()
    if clean.startswith("postgres://"):
        return "postgresql+psycopg://" + clean[len("postgres://") :]
    if clean.startswith("postgresql://"):
        return "postgresql+psycopg://" + clean[len("postgresql://") :]
    return clean


engine = create_async_engine(
    _normalized_database_url(settings.database_url),
    pool_pre_ping=True,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

