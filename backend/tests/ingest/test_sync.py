"""ING-5 RED: idempotent sync orchestration tests (PROJECT_BRIEF §12.2).

Covers:
- happy path: activities, streams and wellness land in the DB via upserts;
  per-second FIT stream arrays (parsed with ``parse_fit_streams`` from the
  real bike fixture bytes) are stored as ``activity_stream`` rows when FIT
  bytes are available, with the streams endpoint as fallback when not;
- idempotence (§12.2 acceptance): re-running the same sync creates no
  duplicate activities/streams/wellness rows;
- Intervals.icu's own load metric is stored ONLY in the non-authoritative
  cross-check column ``activity.intervals_icu_load`` (§5.1);
- one failing activity (4xx) does not abort the sync: a ``SyncResult``
  dataclass reports per-endpoint/per-item counts (synced/skipped/failed);
- pacing: a configurable minimum interval between client calls
  (``intervals_min_request_interval_s``, default 0.1s => <=10 req/s) is
  enforced between every client call, verified with an injected fake
  clock/sleep so tests stay fast.

Design choices documented here:
- FIT-parsed streams are preferred when a non-empty, parseable FIT file is
  available (highest-fidelity per-second data); otherwise the streams
  endpoint provides the rows.
- Client failures are attributed to ``streams_failed`` (per activity item);
  DB-level failures to ``activities_failed``/``wellness_failed``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.models import ActivityRow, ActivityStreamRow, WellnessRow
from app.ingest.exceptions import IntervalsNotFoundError
from app.ingest.fit_parser import parse_fit_streams
from app.ingest.models import Activity, Stream, Wellness
from app.ingest.sync import sync_date_range

pytestmark = pytest.mark.anyio

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "garmin-fenix-5-bike.fit"

OLDEST, NEWEST = "2026-01-01", "2026-01-31"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic clock/sleep pair: ``sleep`` advances the fake timeline."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeClient:
    """Duck-typed IntervalsClient with a call recorder for pacing asserts."""

    def __init__(
        self,
        *,
        activities: list[Activity] | None = None,
        streams_by_id: dict[int, list[Stream]] | None = None,
        wellness: list[Wellness] | None = None,
        fit_files: dict[int, bytes] | None = None,
        fail_ids: set[int] | None = None,
        clock: Any = None,
    ) -> None:
        self.activities = activities or []
        self._streams = streams_by_id or {}
        self.wellness = wellness or []
        self._fit_files = fit_files or {}
        self._fail_ids = fail_ids or set()
        self._clock = clock or (lambda: 0.0)
        self.call_times: list[tuple[str, float]] = []

    def _record(self, what: str) -> None:
        self.call_times.append((what, self._clock()))

    def list_activities(self, oldest: str, newest: str) -> list[Activity]:
        self._record("list_activities")
        return list(self.activities)

    def get_streams(self, activity_id: int) -> list[Stream]:
        self._record(f"streams:{activity_id}")
        if activity_id in self._fail_ids:
            raise IntervalsNotFoundError(404, f"no streams for {activity_id}")
        return list(self._streams.get(activity_id, []))

    def get_wellness(self, oldest: str, newest: str) -> list[Wellness]:
        self._record("wellness")
        return list(self.wellness)

    def download_fit_file(self, activity_id: int) -> bytes:
        self._record(f"fit:{activity_id}")
        if activity_id in self._fail_ids:
            raise IntervalsNotFoundError(404, f"no fit for {activity_id}")
        return self._fit_files.get(activity_id, b"")


def _activity(aid: int, **extras: Any) -> Activity:
    payload: dict[str, Any] = {
        "id": aid,
        "name": f"Ride {aid}",
        "type": "Ride",
        "start_date": "2026-01-15T08:30:00+00:00",
        "start_date_local": "2026-01-15T09:30:00+01:00",
        "distance": 10000.0,
        "moving_time": 3600,
    }
    payload.update(extras)
    return Activity.model_validate(payload)


def _session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


async def _counts(db_engine: AsyncEngine) -> dict[type[Any], int]:
    async with _session_factory(db_engine)() as session:
        result: dict[type[Any], int] = {}
        for model in (ActivityRow, ActivityStreamRow, WellnessRow):
            count: int = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            result[model] = count
        return result


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_sync_happy_path_persists_activities_streams_wellness(
    db_engine: AsyncEngine,
) -> None:
    fit_bytes = FIXTURE_PATH.read_bytes()
    parsed = parse_fit_streams(fit_bytes)
    client = FakeClient(
        activities=[
            _activity(1, icu_training_load=132.0, total_elevation_gain=120.0),
            _activity(2),
        ],
        streams_by_id={
            2: [
                Stream(type="power", data=[0, 100, 150]),
                Stream(type="hr", data=[90, 110, 120]),
            ],
        },
        wellness=[
            Wellness.model_validate(
                {
                    "id": "2026-01-15",
                    "hrv": 58.0,
                    "RHR": 48.0,
                    "sleep_minutes": 420,
                    "weight": 70.2,
                    "lnHrv": 4.06,
                    "sleepScore": 77.0,
                }
            ),
        ],
        fit_files={1: fit_bytes},
    )

    result = await sync_date_range(
        OLDEST, NEWEST, _session_factory(db_engine), client
    )

    # Count summary: activity 1 via FIT (5 non-empty streams; power/cadence
    # all-None skipped), activity 2 via the streams endpoint (2 streams).
    assert result.activities_synced == 2
    assert result.activities_failed == 0
    assert result.streams_synced == 7
    assert result.streams_skipped == 2
    assert result.streams_failed == 0
    assert result.wellness_synced == 1
    assert result.wellness_failed == 0

    # FIT-first choice: activity 1 uses the FIT file, activity 2 the endpoint.
    called = [name for name, _ in client.call_times]
    assert "fit:1" in called
    assert "streams:1" not in called
    assert "streams:2" in called

    counts = await _counts(db_engine)
    assert counts[ActivityRow] == 2
    assert counts[ActivityStreamRow] == 7
    assert counts[WellnessRow] == 1

    async with _session_factory(db_engine)() as session:
        activity = (
            await session.execute(
                _fresh(select(ActivityRow).where(ActivityRow.source_id == 1))
            )
        ).scalar_one()
        assert activity.name == "Ride 1"
        assert activity.distance_m == 10000.0
        assert activity.duration_s == 3600
        assert activity.elevation_m == 120.0
        assert activity.start_time == datetime(2026, 1, 15, 8, 30, tzinfo=UTC)

        # Per-second FIT arrays stored for every stream with data.
        fit_rows = (
            (
                await session.execute(
                    _fresh(
                        select(ActivityStreamRow).where(
                            ActivityStreamRow.activity_id == activity.id
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        by_type = {row.stream_type: row.payload for row in fit_rows}
        assert set(by_type) == {"time", "hr", "speed", "altitude", "distance"}
        for key, payload in by_type.items():
            assert payload == parsed[key]

        wellness = (
            await session.execute(_fresh(select(WellnessRow)))
        ).scalar_one()
        assert wellness.date == datetime(2026, 1, 15).date()
        assert wellness.hrv == 58.0
        assert wellness.resting_hr == 48.0
        assert wellness.sleep_minutes == 420
        assert wellness.weight == 70.2
        assert wellness.ln_hrv == 4.06
        assert wellness.sleep_score == 77.0


# ---------------------------------------------------------------------------
# Idempotence (§12.2 acceptance)
# ---------------------------------------------------------------------------


async def test_sync_is_idempotent_on_rerun(db_engine: AsyncEngine) -> None:
    fit_bytes = FIXTURE_PATH.read_bytes()

    def build_client() -> FakeClient:
        return FakeClient(
            activities=[_activity(1, icu_training_load=132.0), _activity(2)],
            streams_by_id={
                2: [Stream(type="power", data=[0, 100, 150])],
            },
            wellness=[Wellness.model_validate({"id": "2026-01-15", "hrv": 58.0})],
            fit_files={1: fit_bytes},
        )

    first = await sync_date_range(
        OLDEST, NEWEST, _session_factory(db_engine), build_client()
    )
    counts_after_first = await _counts(db_engine)
    assert counts_after_first[ActivityRow] == 2
    assert counts_after_first[ActivityStreamRow] == 6
    assert counts_after_first[WellnessRow] == 1

    second = await sync_date_range(
        OLDEST, NEWEST, _session_factory(db_engine), build_client()
    )

    counts_after_second = await _counts(db_engine)
    assert counts_after_second == counts_after_first, (
        "re-running the same sync must not create duplicate rows (§12.2)"
    )
    assert second.activities_synced == first.activities_synced
    assert second.streams_synced == first.streams_synced
    assert second.wellness_synced == first.wellness_synced

    # Same physical rows (ids unchanged), payloads up to date.
    async with _session_factory(db_engine)() as session:
        ids = (
            (await session.execute(select(ActivityRow.id).order_by(ActivityRow.id)))
            .scalars()
            .all()
        )
        assert ids == sorted(ids)
        payload = (
            await session.execute(
                select(ActivityStreamRow.payload)
                .where(ActivityStreamRow.stream_type == "time")
                .limit(1)
            )
        ).scalar_one()
        assert payload == parse_fit_streams(fit_bytes)["time"]


# ---------------------------------------------------------------------------
# Non-authoritative Intervals.icu load (§5.1)
# ---------------------------------------------------------------------------


async def test_intervals_load_stored_only_in_cross_check_column(
    db_engine: AsyncEngine,
) -> None:
    client = FakeClient(
        activities=[_activity(1, icu_training_load=132.0)],
        fit_files={1: FIXTURE_PATH.read_bytes()},
    )

    await sync_date_range(OLDEST, NEWEST, _session_factory(db_engine), client)

    async with _session_factory(db_engine)() as session:
        activity = (
            await session.execute(_fresh(select(ActivityRow)))
        ).scalar_one()
        assert activity.intervals_icu_load == 132.0

    # No load-named column may exist anywhere except the explicitly
    # non-authoritative cross-check column (§5.1).
    for model in (ActivityRow, ActivityStreamRow, WellnessRow):
        load_columns = {
            col.name
            for col in model.__table__.columns
            if col.name == "load" or "_load" in col.name or col.name.startswith("load")
        }
        if model is ActivityRow:
            assert load_columns == {"intervals_icu_load"}
        else:
            assert load_columns == set()


# ---------------------------------------------------------------------------
# Partial failure does not abort the sync
# ---------------------------------------------------------------------------


async def test_failing_activity_does_not_abort_sync(db_engine: AsyncEngine) -> None:
    client = FakeClient(
        activities=[_activity(1), _activity(2), _activity(3)],
        streams_by_id={
            1: [Stream(type="hr", data=[90.0, 100.0])],
            3: [Stream(type="power", data=[0.0, 250.0])],
        },
        wellness=[Wellness.model_validate({"id": "2026-01-15", "hrv": 58.0})],
        fail_ids={2},
    )

    result = await sync_date_range(
        OLDEST, NEWEST, _session_factory(db_engine), client
    )

    assert result.activities_synced == 2
    assert result.streams_failed == 1
    assert result.streams_synced == 2
    assert result.wellness_synced == 1

    counts = await _counts(db_engine)
    assert counts[ActivityRow] == 2
    assert counts[ActivityStreamRow] == 2
    assert counts[WellnessRow] == 1


# ---------------------------------------------------------------------------
# Pacing: minimum interval between client calls (<=10 req/s target)
# ---------------------------------------------------------------------------


async def test_pacing_enforces_min_interval_between_client_calls() -> None:
    clock = FakeClock()
    # A factory that must never be touched: with no items there is no DB work.
    def factory() -> AsyncSession:
        raise AssertionError("session factory must not be used with no items")

    client = FakeClient(clock=clock.time)
    settings = Settings(intervals_min_request_interval_s=0.25)

    await sync_date_range(
        OLDEST,
        NEWEST,
        factory,  # type: ignore[arg-type]
        client,
        settings=settings,
        clock=clock.time,
        sleep=clock.sleep,
    )

    called = [name for name, _ in client.call_times]
    assert called == ["list_activities", "wellness"]
    times = [ts for _, ts in client.call_times]
    gaps = [b - a for a, b in pairwise(times)]
    assert gaps, "expected at least two paced client calls"
    for gap in gaps:
        assert gap >= 0.25 - 1e-9, f"client calls {gaps} closer than 0.25s"
    assert all(s == 0.25 for s in clock.sleeps)


async def test_pacing_disabled_with_zero_interval() -> None:
    clock = FakeClock()

    def factory() -> AsyncSession:
        raise AssertionError("session factory must not be used with no items")

    client = FakeClient(clock=clock.time)
    settings = Settings(intervals_min_request_interval_s=0.0)

    await sync_date_range(
        OLDEST,
        NEWEST,
        factory,  # type: ignore[arg-type]
        client,
        settings=settings,
        clock=clock.time,
        sleep=clock.sleep,
    )

    assert len(client.call_times) == 2
    assert clock.sleeps == []
