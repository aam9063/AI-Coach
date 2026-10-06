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
  identifier (``[A-Za-z0-9_]+``).

``pytest.skip`` stays in the fixture layer (tests/conftest.py), not here:
these functions raise, so a broken configuration fails loudly instead of
being mistaken for "Postgres unavailable".
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.settings import get_settings

ALLOW_DEV_DATABASE_ENV = "TESTS_ALLOW_DEV_DATABASE"
DEFAULT_MAINTENANCE_DATABASE = "postgres"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


class DevDatabaseRefusedError(RuntimeError):
    """The resolved test database IS the configured one; refusing to run."""


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
        else _replace_database(configured, f"{name}_test")
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
    ``create_all``/``drop_all`` fixtures): the database name must end with
    ``_test`` unless the operator explicitly opts in with
    ``TESTS_ALLOW_DEV_DATABASE=1``. Returning the URL makes it usable inline,
    e.g. ``cfg.set_main_option("sqlalchemy.url", assert_is_test_database(url))``.
    """
    name = database_name(database_url)
    if name.endswith("_test") or os.environ.get(ALLOW_DEV_DATABASE_ENV) == "1":
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
                await conn.execute(text(f'create database "{name}"'))
    finally:
        await engine.dispose()


async def create_test_engine() -> AsyncEngine:
    """Ensure the test database exists and return an engine bound to it."""
    url = test_database_url()
    await ensure_database(url)
    return create_async_engine(url)
