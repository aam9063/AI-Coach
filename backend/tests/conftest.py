"""Shared fixtures for the whole test suite.

DB-backed tests run against a DEDICATED test database (``<database>_test``,
see ``tests/dbsupport.py``), never against the DSN configured in ``.env``:
the fixtures drop and recreate every table, and doing that on the
developer's database wiped real ingested data once (2026-10-05). Tests skip
cleanly when Postgres is unreachable; a misconfigured database URL raises
instead of skipping, so it cannot hide behind a skip.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from tests.dbsupport import DevDatabaseRefusedError, create_test_engine

POSTGRES_HINT = (
    "Postgres unreachable; DB-touching tests require the compose Postgres: "
    "docker compose -f infra/docker-compose.yml --env-file .env up -d postgres"
)


@pytest.fixture
def anyio_backend() -> str:
    """Backend for the anyio pytest plugin (async tests)."""
    return "asyncio"


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Engine against the dedicated test database, fresh schema per test."""
    try:
        engine = await create_test_engine()
    except DevDatabaseRefusedError:
        raise
    except Exception:
        pytest.skip(POSTGRES_HINT)

    try:
        from app.db.models import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session bound to the fresh-schema test engine."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session
