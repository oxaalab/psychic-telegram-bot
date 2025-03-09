from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None


class _SessionFactory:
    """
    Allows `SessionLocal()` to work after runtime init via init_db().
    """

    def __init__(self) -> None:
        self._maker: async_sessionmaker[AsyncSession] | None = None

    def configure(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker

    def __call__(self, *args, **kwargs) -> AsyncSession:
        if self._maker is None:
            raise RuntimeError("Database is not initialized; call init_db() first.")
        return self._maker(*args, **kwargs)


SessionLocal = _SessionFactory()


def init_db(database_url: str) -> AsyncEngine:
    if not database_url:
        raise RuntimeError("DATABASE_URL is empty; set it in environment.")
    global _engine
    _engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )
    SessionLocal.configure(async_sessionmaker(_engine, expire_on_commit=False))
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("DB engine is not initialized; call init_db() first.")
    return _engine


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
