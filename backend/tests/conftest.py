"""Shared fixtures for the whole test suite.

DB-backed tests run against a DEDICATED test database (``<database>_test``,
see ``tests/dbsupport.py``), never against the DSN configured in ``.env``:
the fixtures drop and recreate every table, and doing that on the
developer's database wiped real ingested data once (2026-10-05). Tests skip
cleanly when Postgres is unreachable; a misconfigured database URL raises
instead of skipping, so it cannot hide behind a skip.

In CI, ``TESTS_REQUIRE_POSTGRES=1`` (set in .github/workflows/ci.yml) turns
that skip into a hard failure: CI provisions a real postgres:16 service, so
an unreachable database there means the pipeline is broken and must never
report green while the database tests were skipped.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from tests.dbsupport import (
    DevDatabaseRefusedError,
    create_test_engine,
    postgres_required,
)

POSTGRES_HINT = (
    "Postgres unreachable; DB-touching tests require the compose Postgres: "
    "docker compose -f infra/docker-compose.yml --env-file .env up -d postgres"
)

POSTGRES_REQUIRED_MESSAGE = (
    "Postgres is unreachable but TESTS_REQUIRE_POSTGRES=1 is set, so the "
    "database tests must not be skipped: a green run that skipped them has "
    "approved changes they never exercised. Start the compose Postgres "
    "(docker compose -f infra/docker-compose.yml --env-file .env up -d "
    "postgres) or fix the CI postgres service."
)


def unreachable_postgres() -> None:
    """React to an unreachable Postgres: fail hard in CI, skip otherwise.

    Called from the ``db_engine`` fixture's ``except`` branch. Kept as a
    plain function (not inline) so tests can pin both branches by
    monkeypatching ``pytest.fail``/``pytest.skip`` instead of staging a
    real outage.
    """
    if postgres_required():
        pytest.fail(POSTGRES_REQUIRED_MESSAGE)
    pytest.skip(POSTGRES_HINT)


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
        unreachable_postgres()

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
