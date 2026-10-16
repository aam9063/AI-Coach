"""The readiness recompute scheduled job — the fix for the recorded gap.

The live run of the stack (SCH-10, recorded in the ODD task) proved the
gap: the beat declares sync, load recompute, brief, review and weekly
report, but NOTHING computes readiness, so ``readiness_snapshot`` has no
row for today and the morning brief can never get past its own gather —
it skips with "no readiness snapshot for ...". These tests are the
executable specification of the fix:

- **Entry**: a sixth beat entry, ``readiness-recompute``, firing at
  :28/:58 — AFTER the :25/:55 ``daily-load-recompute`` entry it depends on
  (readiness reads the ``daily_load`` ``combined`` TSB rows) and BEFORE
  the 07:30 brief that consumes the snapshot. A SEPARATE entry, not an
  extension of the load task: nesting readiness inside the load task body
  would couple a readiness failure into the load task's state (or demand
  a silent swallow the failure contract forbids). Beat entries are
  independent; each failure surfaces as its own Celery ``FAILURE``.
- **Same service seam as the tool path**: the task body dispatches to
  :func:`app.services.readiness.recompute_readiness` — the EXACT function
  the ``log_subjective`` tool path and the engine-outputs CLI call. No
  second readiness pipeline exists.
- **Bounded**: the entry pins ``days: 30`` — the same trailing window as
  the sync and the load recompute, never years of reprocessing. A
  non-positive window fails the task loudly (the service's contract).
- **Idempotent**: the service upserts one snapshot per (athlete, date);
  running the task twice against the same data leaves the same rows with
  the same content — proven against the dedicated test database through
  the eager Celery task, the public interface the beat actually runs.
- **Offline tests**: the task body tests inject fakes into the
  ``_recompute``/``session_factory`` seams; only the idempotency proof
  touches the (local, dedicated) test database. No network anywhere.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.readiness as readiness_service
from app.db.models import ReadinessSnapshotRow
from app.db.repository import upsert_daily_load, upsert_wellness
from app.scheduler import readiness_tasks
from app.scheduler.celery_app import BEAT_SCHEDULE, TASK_MODULES, celery_app
from app.services.readiness import ReadinessReport, SkippedDay

TASK_NAME = "app.scheduler.readiness_tasks.recompute_readiness"
ENTRY_NAME = "readiness-recompute"

DAY = dt.date(2026, 7, 1)
COMPUTED_AT = dt.datetime(2026, 7, 1, 6, 0, tzinfo=dt.UTC)


def _report(**overrides: Any) -> ReadinessReport:
    values: dict[str, Any] = {
        "window_start": DAY,
        "window_end": DAY,
        "engine_version": "test-0.0.0",
        "days_considered": 30,
        "rows_upserted": 28,
        "skipped": (
            SkippedDay(date=DAY, reason="no daily_load combined row for this date"),
        ),
    }
    values.update(overrides)
    return ReadinessReport(**values)


class _Sentinel:
    """Opaque injected session factory: callable, yielding an opaque
    session-like async context (the runner opens a session around the
    service call, exactly as it does with a real factory)."""

    def __call__(self) -> _SentinelSessionContext:
        return _SentinelSessionContext()


class _SentinelSession:
    """Opaque session stand-in; only the (faked) service and the runner's
    single commit touch it."""

    async def commit(self) -> None:
        return None


class _SentinelSessionContext:
    async def __aenter__(self) -> object:
        return _SentinelSession()

    async def __aexit__(self, *exc: object) -> None:
        return None


def _fake_recompute(
    calls: list[dict[str, Any]], report: ReadinessReport | None = None
) -> Any:
    """Async stand-in for the readiness service: records the call."""

    async def _run(session: Any, **kwargs: Any) -> ReadinessReport:
        calls.append({"session": session, **kwargs})
        return report if report is not None else _report()

    return _run


@pytest.fixture
def recompute_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the readiness service seam with a recording fake."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(readiness_tasks, "_recompute", _fake_recompute(calls))
    return calls


# ---------------------------------------------------------------------------
# Registration and wiring
# ---------------------------------------------------------------------------


def test_task_is_registered_on_the_app() -> None:
    """Importing the task module registers the task under its full name."""
    assert TASK_NAME in celery_app.tasks


def test_task_dispatches_to_the_same_service_seam_as_the_tool_path() -> None:
    """The task's runner IS the readiness service the tool path uses.

    ``log_subjective`` (and the engine-outputs CLI) recompute readiness
    through :func:`app.services.readiness.recompute_readiness`; the
    scheduled job must call the SAME function — no second pipeline.
    """
    assert readiness_tasks._recompute is readiness_service.recompute_readiness


def test_beat_entry_exists_after_the_load_recompute_and_before_the_brief() -> None:
    """The sixth entry exists, at :28/:58 — after the :25/:55 load
    recompute (whose TSB rows readiness reads) and before the 07:30 brief
    (which consumes the snapshot)."""
    entry = dict(BEAT_SCHEDULE[ENTRY_NAME])
    assert entry["task"] == TASK_NAME
    cron = entry["schedule"]
    assert set(cron.minute) == {28, 58}
    assert set(cron.hour) == set(range(24))
    assert set(cron.day_of_week) == set(range(7))


def test_beat_entry_is_a_registered_task_module_and_task() -> None:
    """The worker can actually run what beat sends: the module is in the
    explicit include and the task name resolves on the app."""
    assert ENTRY_NAME in BEAT_SCHEDULE
    assert "app.scheduler.readiness_tasks" in TASK_MODULES
    assert TASK_NAME in celery_app.tasks
    assert readiness_tasks.recompute_readiness.name == TASK_NAME


def test_beat_entry_pins_the_bounded_window_and_athlete() -> None:
    """The entry pins ``days: 30`` — the documented trailing window, the
    same bound as the sync and the load recompute; never years."""
    entry = BEAT_SCHEDULE[ENTRY_NAME]
    assert entry["kwargs"] == {"days": 30, "athlete_id": 1}
    assert readiness_tasks.recompute_readiness.run.__defaults__[0] == 30


# ---------------------------------------------------------------------------
# Task body (eager mode, offline via the injected seams)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("eager_celery")
def test_task_returns_json_summary(recompute_calls: list[dict[str, Any]]) -> None:
    """The task returns a JSON-serializable summary of the report,
    including the full skipped accounting (never silent)."""
    session_factory = _Sentinel()
    result = celery_app.tasks[TASK_NAME].apply(
        kwargs={"days": 30, "athlete_id": 1, "session_factory": session_factory}
    )

    assert result.state == "SUCCESS"
    summary = result.get()
    json.dumps(summary)  # must be JSON-serializable
    assert summary == {
        "days": 30,
        "athlete_id": 1,
        "window_start": "2026-07-01",
        "window_end": "2026-07-01",
        "engine_version": "test-0.0.0",
        "days_considered": 30,
        "rows_upserted": 28,
        "skipped": [
            {"date": "2026-07-01", "reason": "no daily_load combined row for this date"}
        ],
    }


@pytest.mark.usefixtures("eager_celery")
def test_task_forwards_seams_and_defaults(
    recompute_calls: list[dict[str, Any]],
) -> None:
    """days/athlete/window/seams reach the service; window defaults to
    today (UTC) and the window to the documented 30 days."""
    session_factory = _Sentinel()
    readiness_tasks.recompute_readiness.run(
        session_factory=session_factory
    )

    assert len(recompute_calls) == 1
    call = recompute_calls[0]
    assert call["days"] == 30
    assert call["athlete_id"] == 1
    # No explicit window: the task uses today (UTC), like the CLI default.
    assert call["window_end"] == dt.datetime.now(dt.UTC).date()
    # The engine constants come from Settings, never re-hardcoded here.
    assert call["engine_version"]  # sourced from settings.engine_version


@pytest.mark.usefixtures("eager_celery")
def test_task_parses_explicit_window_end(
    recompute_calls: list[dict[str, Any]],
) -> None:
    """A string window_end is converted to a date before the service sees it."""
    readiness_tasks.recompute_readiness.run(
        window_end="2026-07-01", session_factory=_Sentinel()
    )

    assert recompute_calls[0]["window_end"] == DAY


@pytest.mark.usefixtures("eager_celery")
def test_unexpected_exception_fails_the_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception surfaces as a Celery FAILURE state — the
    failure is loud and INDEPENDENT of the load recompute entry."""

    async def _boom(session: Any, **kwargs: Any) -> ReadinessReport:
        raise RuntimeError("db down")

    monkeypatch.setattr(readiness_tasks, "_recompute", _boom)
    result = celery_app.tasks[TASK_NAME].apply(
        kwargs={"days": 1, "session_factory": _Sentinel()}
    )

    assert result.state == "FAILURE"
    assert isinstance(result.result, RuntimeError)


@pytest.mark.usefixtures("eager_celery")
def test_non_positive_window_fails_the_task() -> None:
    """Boundedness: a non-positive window is rejected by the service's own
    contract and surfaces as a Celery FAILURE — the schedule can never be
    reconfigured into reprocessing unbounded windows silently."""
    result = celery_app.tasks[TASK_NAME].apply(
        kwargs={"days": 0, "session_factory": _Sentinel()}
    )

    assert result.state == "FAILURE"
    assert isinstance(result.result, ValueError)
    assert "days must be positive" in str(result.result)


# ---------------------------------------------------------------------------
# Idempotency through the real task body against the dedicated test database
# (no network: the readiness recompute only reads/writes the local DB)
# ---------------------------------------------------------------------------

HRV_SERIES_DAYS = 67  # engine defaults: hrv_window 7 + baseline 60
ATHLETE_ID = 1


async def _seed(db_engine: Any, *, window_end: dt.date, days: int) -> None:
    """Wellness for the longest series the signals need, plus the
    ``daily_load`` combined rows (the TSB input) for the window itself."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        first = window_end - dt.timedelta(days=HRV_SERIES_DAYS - 1)
        day = first
        while day <= window_end:
            await upsert_wellness(
                session,
                athlete_id=ATHLETE_ID,
                date=day,
                hrv=65.0,
                ln_hrv=4.174387269895637,
                resting_hr=50.0,
                sleep_minutes=480,
            )
            day += dt.timedelta(days=1)
        for offset in range(days):
            await upsert_daily_load(
                session,
                athlete_id=ATHLETE_ID,
                date=window_end - dt.timedelta(days=offset),
                sport="combined",
                tss=0.0,
                ctl=40.0,
                atl=30.0,
                tsb=1.0,
                engine_version="test-0.0.0",
                computed_at=COMPUTED_AT,
            )
        await session.commit()


def _task_body(window_end: dt.date, days: int) -> dict[str, Any]:
    """Run the real task body in a worker-like thread (Celery workers run
    the task in their own thread; ``run_readiness_recompute_sync`` uses
    ``asyncio.run``, which must not see the test's running loop). The task
    gets its own NullPool engine on the DEDICATED test database — the
    ``db_engine`` fixture's asyncpg pool belongs to the test's event loop
    and must not be shared with the task's internal one."""
    from sqlalchemy.pool import NullPool

    from tests import dbsupport

    engine = create_async_engine(dbsupport.test_database_url(), poolclass=NullPool)
    try:
        summary: dict[str, Any] = celery_app.tasks[TASK_NAME].run(
            days=days,
            athlete_id=ATHLETE_ID,
            window_end=window_end.isoformat(),
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
        )
        return summary
    finally:
        engine.sync_engine.dispose()


@pytest.mark.anyio
@pytest.mark.usefixtures("eager_celery")
async def test_task_run_twice_is_idempotent(
    db_engine: Any,
) -> None:
    """Running the ACTUAL task twice against the same fixture upserts the
    same snapshots: same row count, same content — no duplicates, the
    scheduler-level proof of the (athlete, date) upsert anchor."""
    window_end = DAY
    days = 2
    await _seed(db_engine, window_end=window_end, days=days)

    def _snapshots() -> list[tuple[str, str]]:
        return asyncio.run(_read_snapshots(window_end))

    first = await asyncio.to_thread(_task_body, window_end, days)
    assert first["rows_upserted"] == days
    assert first["skipped"] == []
    payloads_after_first = await _read_snapshots(db_engine)

    second = await asyncio.to_thread(_task_body, window_end, days)
    assert second["rows_upserted"] == days
    assert second["skipped"] == []

    # No duplicates, and the second run changed NOTHING in the database.
    assert await _read_snapshots(db_engine) == payloads_after_first
    assert len(payloads_after_first) == days


async def _read_snapshots(db_engine: Any) -> list[tuple[str, str]]:
    """The persisted (date, signals-JSON) pairs, in a stable order."""
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        rows = (
            (await session.execute(select(ReadinessSnapshotRow))).scalars().all()
        )
    return sorted(
        (row.date.isoformat(), json.dumps(row.signals, sort_keys=True))
        for row in rows
    )
