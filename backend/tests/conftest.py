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

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from tests.dbsupport import (
    DevDatabaseRefusedError,
    create_test_engine,
    created_test_databases,
    database_name,
    drop_test_database,
    is_test_database_name,
    postgres_required,
    test_database_url,
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


@pytest.fixture(autouse=True)
def _reset_process_level_agent_state() -> Iterator[None]:
    """No test inherits another test's process-level agent wiring.

    ``handle_inbound_message`` deliberately caches the engine, the model, the
    Twilio client and the session repository for the life of the process: a
    Celery worker runs one turn per task, and building them per turn leaked a
    connection pool, an HTTP client and a ``DbSessionRepository`` thread with its
    own event loop for every WhatsApp message. The tool registry keeps a provider
    of its own. Both are process-level state, so a test that sets them and never
    resets them would silently leave the next test talking to a different
    database -- the class of silent contamination that cost a whole
    investigation on 2026-10-19. Reset BEFORE and after every test, so neither a
    leaky setup nor a leaky teardown can survive.
    """
    from app.agent import tools

    def _reset() -> None:
        # ONLY the tool registry is process-level state. The pipeline's
        # dependencies are deliberately per turn: caching them across turns broke
        # the SECOND WhatsApp message in production (2026-10-21), because each
        # task runs its own asyncio.run and asyncpg connections belong to the
        # loop that created them.
        tools.reset_session_factory_provider()
        tools.reset_settings_override()

    _reset()
    yield
    _reset()


@pytest.fixture(scope="session", autouse=True)
def _drop_per_run_test_database() -> Iterator[None]:
    """Drop this run's own per-run test database at session end.

    ``test_database_url()`` derives a UNIQUE name per process
    (``<db>_test_<run id>``), so two concurrent runs on one shared Postgres
    can no longer race each other's ``drop_all``/``create_all`` and turn
    commits (the 2026-10-19 single-victim DB-test flake). Dropping the run's
    own database at session end keeps the server from accumulating one
    database per run. Crash leftovers are reclaimed by
    ``drop_stale_test_databases`` on the NEXT run's first ``ensure_database``.

    Only databases this process actually ensured are dropped, so sessions
    that never reached Postgres stay a silent no-op, and the operator
    opt-in pass-through (``TESTS_ALLOW_DEV_DATABASE=1`` with the configured
    database itself) is never dropped.
    """
    from app.core.settings import get_settings

    try:
        configured_name = database_name(get_settings().database_url)
    except Exception:
        configured_name = None
    yield
    for name in sorted(created_test_databases()):
        if not is_test_database_name(name) or name == configured_name:
            continue
        try:
            asyncio.run(
                drop_test_database(
                    test_database_url().rsplit("/", 1)[0] + "/" + name
                )
            )
        except Exception as exc:  # best-effort: never fail the session here
            print(
                f"warning: could not drop per-run test database {name!r}: {exc}"
            )


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
