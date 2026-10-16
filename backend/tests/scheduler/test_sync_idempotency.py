"""SCH-3: ``sync_intervals`` is idempotent — run twice, no duplicates.

The proof the ODD task asks for (brief §11, §12.10): running the ACTUAL
Celery task — the same body the beat entry executes, through
``run_sync`` → ``backfill`` → ``sync_date_range`` — twice against the SAME
fixture creates NO duplicate rows. Feature 2's upsert anchors (activities
keyed by ``(source, source_id)``, streams by ``(activity_id, stream_type)``,
wellness by ``(athlete_id, date)``) are the mechanism; this test is the
proof at the scheduler's public interface.

The existing ``tests/ingest/test_sync.py::test_sync_is_idempotent_on_rerun``
proves the orchestrator (``sync_date_range``) is idempotent; it does NOT
exercise the Celery task wrapper (``sync_intervals``) with its day-window
backfill walk, which is what the beat entry actually runs every 30 minutes
(SCH-2). This module closes that gap.

Zero network: the Intervals client is a fake; the database is the dedicated
test database (``tests/conftest.py::db_engine``). No migration is needed —
this test uses the existing tables only.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.settings import Settings
from app.db.models import ActivityRow, ActivityStreamRow, WellnessRow
from app.ingest.models import Activity, Stream, Wellness
from app.scheduler import tasks as scheduler_tasks
from app.scheduler.celery_app import celery_app
from tests import dbsupport

pytestmark = pytest.mark.anyio

TASK_NAME = "app.scheduler.tasks.sync_intervals"
OLDEST, NEWEST = "2026-01-30", "2026-01-31"


def make_task_settings() -> Settings:
    """Settings for the task run: pacing disabled, no credentials used."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        intervals_min_request_interval_s=0.0,
        openai_api_key="",
        twilio_account_sid="",
        twilio_auth_token="",
    )


def _activity(aid: str, **extras: Any) -> Activity:
    payload: dict[str, Any] = {
        "id": aid,
        "name": f"Ride {aid}",
        "type": "Ride",
        "start_date": "2026-01-31T08:30:00+00:00",
        "start_date_local": "2026-01-31T09:30:00+01:00",
        "distance": 10000.0,
        "moving_time": 3600,
    }
    payload.update(extras)
    return Activity.model_validate(payload)


class FakeClient:
    """Duck-typed IntervalsClient returning the SAME fixture every run."""

    def __init__(self) -> None:
        self.activities = [
            _activity("i163428838", icu_training_load=132.0),
            _activity("i163419945"),
        ]
        self.streams_by_id = {
            "i163419945": [
                Stream(type="power", data=[0, 100, 150]),
                Stream(type="hr", data=[90, 110, 120]),
            ],
        }
        self.wellness = [
            Wellness.model_validate({"id": "2026-01-31", "hrv": 58.0}),
        ]

    def list_activities(self, oldest: str, newest: str) -> list[Activity]:
        return list(self.activities)

    def get_streams(self, activity_id: str) -> list[Stream]:
        return list(self.streams_by_id.get(activity_id, []))

    def get_wellness(self, oldest: str, newest: str) -> list[Wellness]:
        return list(self.wellness)

    def download_fit_file(self, activity_id: str) -> bytes:
        return b""  # no FIT: the streams endpoint provides the rows


async def _counts(db_engine: AsyncEngine) -> dict[str, int]:
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        result: dict[str, int] = {}
        for model in (ActivityRow, ActivityStreamRow, WellnessRow):
            count: int = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            result[model.__name__] = count
        return result


async def _row_keys(db_engine: AsyncEngine) -> tuple[list[Any], list[Any], list[Any]]:
    """Identity of every row: activity ids, (activity, type) streams, dates."""
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        activity_ids = (
            (await session.execute(select(ActivityRow.id).order_by(ActivityRow.id)))
            .scalars()
            .all()
        )
        stream_keys = (
            (
                await session.execute(
                    select(
                        ActivityStreamRow.activity_id, ActivityStreamRow.stream_type
                    ).order_by(
                        ActivityStreamRow.activity_id, ActivityStreamRow.stream_type
                    )
                )
            )
            .all()
        )
        wellness_dates = (
            (await session.execute(select(WellnessRow.date).order_by(WellnessRow.date)))
            .scalars()
            .all()
        )
    return list(activity_ids), [tuple(k) for k in stream_keys], list(wellness_dates)


@pytest.fixture
def sync_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Point the task's ``get_settings()`` at offline test settings."""
    settings = make_task_settings()
    monkeypatch.setattr(scheduler_tasks, "get_settings", lambda: settings)
    return settings


def _task_body() -> dict[str, Any]:
    """Run the real task body in a worker-like thread (Celery workers run
    the sync task in their own thread; ``run_sync`` uses ``asyncio.run``,
    which must not see the test's running loop). The task gets its own
    NullPool engine on the DEDICATED test database — the ``db_engine``
    fixture's asyncpg pool belongs to the test's event loop and must not
    be shared with the task's internal one."""
    engine = create_async_engine(dbsupport.test_database_url(), poolclass=NullPool)
    try:
        summary: dict[str, Any] = celery_app.tasks[TASK_NAME].run(
            days=2,
            end_date=NEWEST,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            client=FakeClient(),
        )
        return summary
    finally:
        engine.sync_engine.dispose()


async def test_sync_intervals_run_twice_creates_no_duplicates(
    db_engine: AsyncEngine, sync_settings: Settings
) -> None:
    """Two full task runs against the same fixture: identical DB state."""
    first_summary = await asyncio.to_thread(_task_body)
    counts_after_first = await _counts(db_engine)
    keys_after_first = await _row_keys(db_engine)

    second_summary = await asyncio.to_thread(_task_body)

    # No duplicates: the second run changed NOTHING in the database.
    counts_after_second = await _counts(db_engine)
    assert counts_after_second == counts_after_first, (
        "re-running sync_intervals must not create duplicate rows (§11/§12.10)"
    )
    assert counts_after_second[ActivityRow.__name__] == 2
    assert counts_after_second[ActivityStreamRow.__name__] == 2
    assert counts_after_second[WellnessRow.__name__] == 1
    assert await _row_keys(db_engine) == keys_after_first

    # Both runs report success and JSON-serializable summaries. The counts
    # count UPSERTS, not new rows: two day-windows x the same two fixture
    # activities = 4 upserted activities (re-upserts included), while the
    # DATABASE state above proves no duplicates were created.
    for summary in (first_summary, second_summary):
        assert summary["ok"] is True
        assert summary["activities_synced"] == 4
        assert summary["streams_synced"] == 4
        assert summary["wellness_synced"] == 2
        assert summary["failed_windows"] == []
        json.dumps(summary)
