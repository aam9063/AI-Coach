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

import pytest

# ``test_database_url`` is imported under an alias on purpose: a name starting
# with ``test_`` would be collected by pytest as a test function in this module
# (it returns a string, which pytest reports as PytestReturnNotNoneWarning).
from tests.dbsupport import (
    ALLOW_DEV_DATABASE_ENV,
    DevDatabaseRefusedError,
    assert_is_test_database,
    database_name,
)
from tests.dbsupport import (
    test_database_url as derive_test_database_url,
)

DEV = "postgresql+asyncpg://tri_coach:devpass@localhost:5432/tri_coach"
ALREADY_TEST = "postgresql+asyncpg://tri_coach:devpass@localhost:5432/tri_coach_test"


def test_database_name_reads_the_path_component() -> None:
    assert database_name(DEV) == "tri_coach"


def test_test_database_is_derived_without_touching_credentials() -> None:
    url = derive_test_database_url(DEV)

    assert url == "postgresql+asyncpg://tri_coach:devpass@localhost:5432/tri_coach_test"


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
