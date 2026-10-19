"""Tests for the DB-backed test isolation helpers.

Regression guard for a real incident: the ingest/db conftests used to run
``create_all``/``drop_all`` against the DSN in ``.env`` — the developer's
own database — so every ``pytest`` run wiped ingested data. Tests now run
against a dedicated ``<database>_test`` database, and these tests pin the
two properties that make that safe:

- the test DSN is derived from the configured one without touching its
  credentials, host or port;
- the helpers refuse to run against whatever ``DATABASE_URL`` points at
  unless the operator explicitly opts in.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

# ``test_database_url`` is imported under an alias on purpose: a name starting
# with ``test_`` would be collected by pytest as a test function in this module
# (it returns a string, which pytest reports as PytestReturnNotNoneWarning).
from tests.dbsupport import (
    ALLOW_DEV_DATABASE_ENV,
    REQUIRE_POSTGRES_ENV,
    DevDatabaseRefusedError,
    assert_is_test_database,
    database_name,
    is_test_database_name,
    postgres_required,
)
from tests.dbsupport import (
    test_database_url as derive_test_database_url,
)

DEV = "postgresql+asyncpg://tri_coach:devpass@localhost:5432/tri_coach"
ALREADY_TEST = "postgresql+asyncpg://tri_coach:devpass@localhost:5432/tri_coach_test"

# The per-run name shape: ``<db>_test_<YYYYMMDD>_<pid>_<hex>`` (the run id is
# embedded so stale databases from crashed runs can be identified and dropped).
RUN_NAME = re.compile(r"^tri_coach_test_\d{8}_\d+_[0-9a-f]{8}$")


# --- TESTS_REQUIRE_POSTGRES: CI must fail loudly, developers still skip ---


def test_postgres_required_is_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the variable (a developer machine) skips stay available."""
    monkeypatch.delenv(REQUIRE_POSTGRES_ENV, raising=False)

    assert postgres_required() is False


def test_postgres_required_when_the_opt_in_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REQUIRE_POSTGRES_ENV, "1")

    assert postgres_required() is True


def test_unreachable_postgres_fails_hard_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the opt-in, an unreachable Postgres must FAIL, never skip.

    CI once reported 1213 passed while 138 database tests were silently
    skipped; the runner approved changes it had not exercised. The failure
    message must name the variable so whoever reads the log knows what to do.
    """
    monkeypatch.setenv(REQUIRE_POSTGRES_ENV, "1")
    failures: list[str] = []
    skips: list[str] = []

    class FailCalledError(Exception):
        pass

    def fake_fail(message: str = "") -> None:
        # The real pytest.fail raises; mimic that so the branch cannot fall
        # through to the skip.
        failures.append(message)
        raise FailCalledError(message)

    monkeypatch.setattr(pytest, "fail", fake_fail)
    monkeypatch.setattr(pytest, "skip", lambda message="": skips.append(message))

    from tests import conftest

    with pytest.raises(FailCalledError):
        conftest.unreachable_postgres()

    assert skips == []
    assert len(failures) == 1
    assert REQUIRE_POSTGRES_ENV in failures[0]


def test_unreachable_postgres_still_skips_for_developers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the variable today's developer experience is unchanged."""
    monkeypatch.delenv(REQUIRE_POSTGRES_ENV, raising=False)
    failures: list[str] = []
    skips: list[str] = []
    monkeypatch.setattr(pytest, "fail", lambda message="": failures.append(message))
    monkeypatch.setattr(pytest, "skip", lambda message="": skips.append(message))

    from tests import conftest

    conftest.unreachable_postgres()

    assert failures == []
    assert skips == [conftest.POSTGRES_HINT]


def test_database_name_reads_the_path_component() -> None:
    assert database_name(DEV) == "tri_coach"


def test_test_database_is_derived_without_touching_credentials() -> None:
    """The derived URL keeps the configured credentials, host and port.

    The database name is UNIQUE PER PROCESS (``<db>_test_<run id>``): two
    concurrent suite runs on one shared Postgres (two worktrees, a writer
    and an eval run) each get their own database, so neither writer's
    ``drop_all``/``create_all`` can land rows or schema changes inside the
    other's freshly recreated schema — the 2026-10-19 intermittent
    single-victim DB-test failure was exactly that cross-writer race on
    the one shared ``tri_coach_test`` name.
    """
    url = derive_test_database_url(DEV)

    name = database_name(url)
    assert RUN_NAME.match(name), f"not a per-run test database name: {name!r}"
    # Credentials, host and port always come from the configured DSN.
    assert url.startswith(
        "postgresql+asyncpg://tri_coach:devpass@localhost:5432/"
    )


def test_derived_name_is_stable_within_one_process() -> None:
    """One run, one database: every derivation in the process agrees."""
    first = derive_test_database_url(DEV)
    second = derive_test_database_url(DEV)

    assert first == second


def test_refuses_when_the_configured_database_is_already_the_test_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No opt-in means the helper must not silently drop the real schema."""
    monkeypatch.delenv(ALLOW_DEV_DATABASE_ENV, raising=False)

    with pytest.raises(DevDatabaseRefusedError):
        derive_test_database_url(ALREADY_TEST)


def test_explicit_opt_in_allows_a_configured_test_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_DEV_DATABASE_ENV, "1")

    assert derive_test_database_url(ALREADY_TEST) == ALREADY_TEST


def test_guard_never_returns_the_configured_url_by_accident() -> None:
    assert derive_test_database_url(DEV) != DEV


def test_rejects_an_unsafe_database_identifier() -> None:
    """The name is interpolated into DDL, so it must be a plain identifier."""
    with pytest.raises(ValueError):
        database_name("postgresql+asyncpg://u:p@h:5432/tri_coach;drop")


def test_assert_is_test_database_accepts_a_test_database() -> None:
    assert assert_is_test_database(ALREADY_TEST) == ALREADY_TEST


def test_is_test_database_name_accepts_the_plain_test_name() -> None:
    assert is_test_database_name("tri_coach_test") is True


def test_is_test_database_name_accepts_a_per_run_name() -> None:
    """assert_is_test_database must accept the per-run derived names too
    (migration round-trips run against them)."""
    assert is_test_database_name("tri_coach_test_20261019_1234_ab12cd34") is True


def test_is_test_database_name_refuses_development_names() -> None:
    assert is_test_database_name("tri_coach") is False
    assert is_test_database_name("tri_coach_test_old_backup") is False
    assert is_test_database_name("my_test_database") is False
    assert is_test_database_name("postgres") is False


def test_assert_is_test_database_refuses_a_development_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration round-trips mutate schema, so this must fail closed."""
    monkeypatch.delenv(ALLOW_DEV_DATABASE_ENV, raising=False)

    with pytest.raises(DevDatabaseRefusedError):
        assert_is_test_database(DEV)


def test_assert_is_test_database_honours_the_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOW_DEV_DATABASE_ENV, "1")

    assert assert_is_test_database(DEV) == DEV


# --- Per-run databases: janitor and drop (need the compose Postgres) -------


async def _maintenance_connect(
    database_url: str,
) -> tuple[Any, Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.dbsupport import maintenance_url

    engine = create_async_engine(
        maintenance_url(database_url), isolation_level="AUTOCOMMIT"
    )
    try:
        conn = await engine.connect()
        return engine, conn
    except Exception:
        await engine.dispose()
        raise


async def _db_exists(conn: Any, name: str) -> bool:
    from sqlalchemy import text

    return bool(
        await conn.scalar(
            text("select 1 from pg_database where datname = :name"), {"name": name}
        )
    )


def _configured_url() -> str:
    """The DSN this process is actually configured with -- never a literal.

    These two tests connect for real, so a hardcoded developer DSN made them
    authenticate with a local password CI does not have
    (``InvalidPasswordError`` for user ``tri_coach``) and CI failed on exactly
    these two tests. The pure URL-derivation tests above keep their fixed DSN
    on purpose: they only manipulate strings and never connect.
    """
    from app.core.settings import get_settings

    return get_settings().database_url


def _scratch_url(name: str) -> str:
    from tests.dbsupport import _replace_database

    return _replace_database(_configured_url(), name)


@pytest.mark.anyio
async def test_drop_stale_test_databases_removes_only_expired_run_databases() -> None:
    """The janitor reclaims databases of crashed runs: only names matching
    the per-run pattern whose embedded date is older than the cutoff are
    dropped — never the plain ``<db>_test``, never fresh runs (another
    writer may be using one right now), never unparseable names."""
    from sqlalchemy import text

    from tests.dbsupport import drop_stale_test_databases, ensure_database

    url = derive_test_database_url(_configured_url())
    expired = _scratch_url("tri_coach_test_20200101_999999_aabbccdd")
    fresh = _scratch_url("tri_coach_test_20990101_999999_aabbccdd")
    unparseable = _scratch_url("tri_coach_test_old_backup")
    # The plain ``<db>_test`` name is what the janitor must NEVER drop. It may
    # not exist in every environment (CI creates only what a run needs), so the
    # test materialises it first instead of assuming a developer's leftovers.
    plain = _scratch_url("tri_coach_test")
    try:
        await ensure_database(expired)
        await ensure_database(fresh)
        await ensure_database(unparseable)
        await ensure_database(plain)

        await drop_stale_test_databases(url, max_age_hours=24)

        engine, conn = await _maintenance_connect(url)
        try:
            expired_name = "tri_coach_test_20200101_999999_aabbccdd"
            fresh_name = "tri_coach_test_20990101_999999_aabbccdd"
            assert await _db_exists(conn, expired_name) is False
            assert await _db_exists(conn, fresh_name) is True
            assert await _db_exists(conn, "tri_coach_test_old_backup") is True
            assert await _db_exists(conn, "tri_coach_test") is True
        finally:
            await conn.execute(
                text(
                    'drop database if exists '
                    '"tri_coach_test_20990101_999999_aabbccdd" with (force)'
                )
            )
            await conn.execute(
                text(
                    'drop database if exists "tri_coach_test_old_backup" with (force)'
                )
            )
            await conn.execute(
                text('drop database if exists "tri_coach_test" with (force)')
            )
            await conn.close()
            await engine.dispose()
    except Exception:
        from tests import conftest

        conftest.unreachable_postgres()


@pytest.mark.anyio
async def test_drop_test_database_drops_exactly_the_given_database() -> None:
    """The session cleanup drops its OWN per-run database — by exact name."""
    from tests.dbsupport import drop_test_database, ensure_database

    scratch = _scratch_url("tri_coach_test_20200101_123456_11223344")
    # Same guard as the janitor test above: without a configured, reachable
    # database this test must follow the suite's skip/strict rule instead of
    # failing with a raw authentication error. An AssertionError is re-raised so
    # a real regression still fails rather than hiding behind the guard.
    try:
        await ensure_database(scratch)
        engine, conn = await _maintenance_connect(scratch)
        try:
            assert await _db_exists(conn, "tri_coach_test_20200101_123456_11223344") is True
        finally:
            await conn.close()
            await engine.dispose()

        await drop_test_database(scratch)

        engine, conn = await _maintenance_connect(scratch)
        try:
            assert await _db_exists(conn, "tri_coach_test_20200101_123456_11223344") is False
        finally:
            await conn.close()
            await engine.dispose()
    except AssertionError:
        raise
    except Exception:
        from tests import conftest

        conftest.unreachable_postgres()
