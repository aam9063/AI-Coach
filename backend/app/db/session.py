"""Async engine and session factory (SQLAlchemy 2 async stack, brief §6).

The DSN comes from environment-driven ``Settings.database_url`` (§14) so no
connection string lives in code.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.settings import get_settings


def create_db_engine(database_url: str | None = None) -> AsyncEngine:
    """Create the async engine for the configured (or given) DSN."""
    return create_async_engine(database_url or get_settings().database_url)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory; ``expire_on_commit=False`` so ORM objects survive commits."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session per request."""
    engine = create_db_engine()
    factory = make_session_factory(engine)
    async with factory() as session:
        yield session
    await engine.dispose()
