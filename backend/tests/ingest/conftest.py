"""Shared fixtures for ingest tests.

DB-touching tests (project convention) run against the real compose Postgres
with a fresh schema per test (create_all/drop_all) and SKIP cleanly when
Postgres is unreachable. SQLite is deliberately not used.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

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


@pytest.fixture(autouse=True)
def _tmp_fit_storage_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Redirect the default raw FIT storage root (ING-6) to a temp dir.

    Syncs that do not pass explicit settings build the default storage from
    ``get_settings()``; without this fixture those tests would write FIT
    files into ``backend/data/fit``. The settings cache is cleared so the
    env var is picked up, and again on teardown so later tests see the
    original environment.
    """
    from app.core.settings import get_settings

    monkeypatch.setenv("INGEST_STORAGE_ROOT", str(tmp_path / "fit"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
