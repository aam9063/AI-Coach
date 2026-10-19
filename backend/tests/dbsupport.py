"""Test-database isolation helpers.

Real incident (2026-10-05): the ingest and db conftests ran
``create_all``/``drop_all`` against the DSN configured in ``.env`` — the
developer's own database — so a plain ``pytest`` run wiped the ingested
activities. DB-backed tests now run against a dedicated
``<database>_test`` database that these helpers derive and create on
demand.

Safety rules enforced here:

- credentials, host and port always come from the configured DSN;
- the derived database must differ from the configured one; otherwise the
  helper refuses loudly unless ``TESTS_ALLOW_DEV_DATABASE=1`` is set
  explicitly by an operator who really means it;
- the database name is interpolated into DDL, so it must be a plain
  identifier (``[A-Za-z0-9_]+``);
- the derived database name is UNIQUE PER PROCESS
  (``<db>_test_<YYYYMMDD>_<pid>_<hex>``): two concurrent suite runs on one
  shared Postgres (two worktrees, a writer and an eval run) each get their
  own database, so neither writer's ``drop_all``/``create_all`` nor its
  in-flight turns can land rows or schema changes inside the other's
  freshly recreated schema. The 2026-10-19 intermittent single-victim
  DB-test failure was exactly that cross-writer race on the one shared
  ``<db>_test`` name: a foreign commit from a concurrent writer landed in
  the victim test's fresh schema and row-count assertions saw phantom
  rows. The session-scoped cleanup fixture drops the run's own database
  at the end; ``drop_stale_test_databases`` reclaims databases of crashed
  runs (embedded date older than 24 h — never a fresh run, never an
  unparseable name).

``pytest.skip`` stays in the fixture layer (tests/conftest.py), not here:
these functions raise, so a broken configuration fails loudly instead of
being mistaken for "Postgres unavailable". The same layer decides what an
unreachable Postgres means: a plain skip for developers, or a hard failure
when ``TESTS_REQUIRE_POSTGRES=1`` (CI) — see ``postgres_required``.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.settings import get_settings

ALLOW_DEV_DATABASE_ENV = "TESTS_ALLOW_DEV_DATABASE"
REQUIRE_POSTGRES_ENV = "TESTS_REQUIRE_POSTGRES"
DEFAULT_MAINTENANCE_DATABASE = "postgres"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

# The per-run suffix embedded in a derived test database name:
# ``<YYYYMMDD>_<pid>_<8 hex chars>``. Strict on purpose: the janitor drops
# ONLY names matching this pattern, so nothing else is ever a candidate.
_RUN_SUFFIX = re.compile(r"^\d{8}_\d+_[0-9a-f]{8}$")

# One run, one database: the suffix is computed once per process so every
# ``test_database_url()`` call in this run resolves to the SAME database.
_RUN_ID: str | None = None

# Names this process actually ensured (created or found) via
# ``ensure_database`` — the session cleanup drops exactly these, so a run
# without Postgres (or a session that never touched the DB) is a silent no-op.
_CREATED: set[str] = set()


def _run_id() -> str:
    global _RUN_ID
    if _RUN_ID is None:
        _RUN_ID = f"{datetime.now(UTC):%Y%m%d}_{os.getpid()}_{secrets.token_hex(4)}"
    return _RUN_ID


def is_test_database_name(name: str) -> bool:
    """Whether ``name`` is a dedicated test database this helper derives.

    True for the plain ``<db>_test`` form and for the per-run
    ``<db>_test_<run id>`` form; false for anything else, so a development
    database whose name merely CONTAINS "test" is still refused.
    """
    if name.endswith("_test"):
        return True
    stem, sep, suffix = name.rpartition("_test_")
    return bool(sep) and stem != "" and _RUN_SUFFIX.match(suffix) is not None


class DevDatabaseRefusedError(RuntimeError):
    """The resolved test database IS the configured one; refusing to run."""


def postgres_required() -> bool:
    """Whether an unreachable Postgres must FAIL instead of skip.

    ``TESTS_REQUIRE_POSTGRES=1`` is how CI (.github/workflows/ci.yml)
    declares that the postgres service is supposed to be there: when it is
    not reachable the pipeline is broken, and a silent skip would let the
    runner approve changes the database tests never exercised (it once
    reported "1213 passed" while 138 database tests were skipped). Without
    the variable — a developer machine — this is False and the fixture
    layer skips with its usual hint.
    """
    return os.environ.get(REQUIRE_POSTGRES_ENV) == "1"


def database_name(database_url: str) -> str:
    """Return the database name of a SQLAlchemy URL, validating it."""
    name = urlsplit(database_url).path.lstrip("/")
    if not name:
        raise ValueError(f"database URL has no database name: {database_url!r}")
    if not _IDENTIFIER.match(name):
        raise ValueError(f"unsafe database identifier: {name!r}")
    return name


def _replace_database(database_url: str, database: str) -> str:
    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path=f"/{database}"))


def test_database_url(database_url: str | None = None) -> str:
    """Derive the dedicated test database URL from the configured one.

    Appends ``_test`` to the database name. If the configured database
    already ends in ``_test`` (or the derived URL would otherwise equal the
    configured one) the helper refuses unless ``TESTS_ALLOW_DEV_DATABASE=1``
    is set, because the fixtures drop and recreate every table.
    """
    configured = database_url or get_settings().database_url
    name = database_name(configured)

    candidate = (
        configured
        if name.endswith("_test")
        else _replace_database(configured, f"{name}_test_{_run_id()}")
    )

    if candidate == configured and os.environ.get(ALLOW_DEV_DATABASE_ENV) != "1":
        raise DevDatabaseRefusedError(
            "refusing to run DB tests against the configured database "
            f"{name!r}: the fixtures drop and recreate every table. Point "
            "DATABASE_URL at a dedicated test database, or set "
            f"{ALLOW_DEV_DATABASE_ENV}=1 if you really mean it."
        )
    return candidate


def assert_is_test_database(database_url: str) -> str:
    """Fail closed unless ``database_url`` is a dedicated test database.

    Used by infrastructure that mutates schema (migration round-trips,
    ``create_all``/``drop_all`` fixtures): the name must be a derived test
    database (plain ``<db>_test`` or the per-run form) unless the operator
    explicitly opts in with ``TESTS_ALLOW_DEV_DATABASE=1``. Returning the
    URL makes it usable inline, e.g.
    ``cfg.set_main_option("sqlalchemy.url", assert_is_test_database(url))``.
    """
    name = database_name(database_url)
    if is_test_database_name(name) or os.environ.get(ALLOW_DEV_DATABASE_ENV) == "1":
        return database_url
    raise DevDatabaseRefusedError(
        f"refusing to run schema-mutating test infrastructure against {name!r}: "
        "it is not a dedicated test database. Set "
        f"{ALLOW_DEV_DATABASE_ENV}=1 only if you really mean it."
    )


def maintenance_url(database_url: str) -> str:
    """URL of the maintenance database used only to CREATE the test one."""
    return _replace_database(database_url, DEFAULT_MAINTENANCE_DATABASE)


async def ensure_database(database_url: str) -> None:
    """Create the database in ``database_url`` if it does not exist yet."""
    name = database_name(database_url)
    engine = create_async_engine(
        maintenance_url(database_url), isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("select 1 from pg_database where datname = :name"),
                {"name": name},
            )
            if not exists:
                # Reclaim crash leftovers BEFORE creating this run's database:
                # the janitor only drops per-run names whose embedded date is
                # older than the cutoff, so a concurrent writer's database is
                # never a candidate.
                await drop_stale_test_databases(database_url)
                await conn.execute(text(f'create database "{name}"'))
        _CREATED.add(name)
    finally:
        await engine.dispose()


def created_test_databases() -> set[str]:
    """Test database names this process actually ensured (see ``_CREATED``)."""
    return set(_CREATED)


async def drop_test_database(database_url: str) -> None:
    """Drop the EXACT database named in ``database_url`` if it exists.

    Used by the session-scoped cleanup fixture to reclaim this run's own
    per-run database. The name is validated like every name interpolated
    into DDL here, and the drop is ``IF EXISTS`` so a skipped run (or a
    second cleanup) is a no-op.
    """
    name = database_name(database_url)
    engine = create_async_engine(
        maintenance_url(database_url), isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'drop database if exists "{name}" with (force)'))
    finally:
        await engine.dispose()


async def drop_stale_test_databases(
    database_url: str, *, max_age_hours: float = 24.0
) -> None:
    """Drop per-run test databases of crashed runs older than the cutoff.

    A crashed run cannot run its session cleanup, so its per-run database
    would otherwise accumulate forever. Only databases whose name matches
    the strict ``<db>_test_<YYYYMMDD>_<pid>_<hex>`` pattern AND whose
    embedded date is older than ``max_age_hours`` are dropped: a concurrent
    writer's fresh database and every unparseable name (including the
    plain ``<db>_test`` of older branches) are never candidates.
    """
    name = database_name(database_url)
    stem, sep, _ = name.rpartition("_test_")
    if not sep:
        stem, sep, _ = name.rpartition("_test")
    if not sep or not stem:
        return
    prefix = f"{stem}_test_"
    cutoff = datetime.now(UTC).timestamp() - max_age_hours * 3600

    engine = create_async_engine(
        maintenance_url(database_url), isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "select datname from pg_database "
                    "where datname like :prefix and datname <> :self"
                ),
                {"prefix": prefix + "%", "self": name},
            )
            for (candidate,) in rows:
                suffix = candidate[len(prefix):]
                if _RUN_SUFFIX.match(suffix) is None:
                    continue
                embedded = datetime.strptime(suffix.split("_", 1)[0], "%Y%m%d")
                if embedded.timestamp() > cutoff:
                    continue
                await conn.execute(
                    text(f'drop database if exists "{candidate}" with (force)')
                )
    finally:
        await engine.dispose()


async def create_test_engine() -> AsyncEngine:
    """Ensure the test database exists and return an engine bound to it."""
    url = test_database_url()
    await ensure_database(url)
    engine = create_async_engine(url)
    # Feature 9 (EV-4): the pgvector extension must exist before the
    # create_all fixtures build ``evidence_chunk``. Idempotent; the compose
    # image (pgvector/pgvector:pg16) ships the extension files. Runs on the
    # dedicated test database only.
    async with engine.begin() as conn:
        await conn.execute(text("create extension if not exists vector"))
    return engine
