"""Shared fixtures for ingest tests.

DB-touching tests (project convention) run against the real compose Postgres
with a fresh schema per test (create_all/drop_all) and SKIP cleanly when
Postgres is unreachable. SQLite is deliberately not used.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.settings import get_settings


@pytest.fixture
def anyio_backend() -> str:
    """Backend for the anyio pytest plugin (async tests)."""
    return "asyncio"


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Engine against the configured Postgres with a fresh schema per test."""
    engine = create_async_engine(get_settings().database_url)
    try:
        try:
            async with engine.connect():
                pass
        except Exception:
            pytest.skip(
                "Postgres unreachable; DB-touching tests require the compose "
                "Postgres: docker compose -f infra/docker-compose.yml "
                "--env-file .env up -d postgres"
            )
        from app.db.models import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session bound to the fresh-schema engine."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session
