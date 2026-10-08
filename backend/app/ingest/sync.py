"""Idempotent sync orchestration (ODD task ING-5, PROJECT_BRIEF §5.1/§12.2).

``sync_date_range`` orchestrates the Intervals.icu client and the idempotent
upsert repositories for one date range:

- activities, streams and wellness land in PostgreSQL via
  ``app.db.repository`` upserts keyed by the (source, source_id) /
  (activity_id, stream_type) / (athlete_id, date) unique anchors, so
  re-running the same sync creates no duplicates (§12.2 acceptance);
- per-second FIT stream arrays are parsed with ``parse_fit_streams`` when
  FIT bytes are available (preferred, highest-fidelity per-second data) and
  fall back to the streams endpoint otherwise;
- Intervals.icu's own load metric, when present in the activity payload, is
  stored ONLY in the explicitly non-authoritative cross-check column
  ``activity.intervals_icu_load`` (§5.1) — it is never engine truth;
- the owner-entered session RPE (``icu_rpe``/``session_rpe``/
  ``perceived_exertion``, LOAD-12) IS stored as input data in
  ``activity.rpe``: unlike the cross-check value it is consumed by the
  engine (sRPE method for strength sports). Values outside the 1-10 scale
  or non-numeric are rejected with a reportable reason (``SyncResult.
  rpe_rejected``/``rpe_rejection_reasons``), never stored silently; a
  missing or null RPE stays ``None``;
- a configurable minimum interval between client calls
  (``Settings.intervals_min_request_interval_s``, default 0.1s => at most
  10 requests/second) is enforced via an injected clock/sleep pair so the
  pacing is testable without real delays;
- raw FIT bytes are archived through the injected
  :class:`app.ingest.storage.RawFileStorage` (ING-6, §5.2: raw files are
  kept so the engine can be re-run when formulas change) and the relative
  path is stored on ``activity.raw_file_path``. The default storage is
  built from settings (``ingest_storage_enabled``/``ingest_storage_root``);
  archived bytes are best-effort — an I/O failure degrades to no stored
  path instead of failing the activity.

Failures are per-item: one failing activity (e.g. an HTTP 4xx on its
streams/FIT download) is counted in the returned :class:`SyncResult` and
does not abort the remaining items. Client failures surface as
``streams_failed`` (per-activity item); DB-level failures as
``activities_failed``/``wellness_failed``.

The client is consumed through the duck-typed :class:`IntervalsClientProtocol`
so tests can inject fakes; this module performs I/O and DB work only and
never imports or calls anything from ``app.engine`` (§6).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings, get_settings
from app.db import repository
from app.ingest.exceptions import IntervalsHTTPError
from app.ingest.fit_parser import parse_fit_streams
from app.ingest.models import Activity, Wellness
from app.ingest.storage import RawFileStorage, storage_from_settings

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class IntervalsClientProtocol(Protocol):
    """Duck-typed subset of ``IntervalsClient`` used by the orchestrator."""

    def list_activities(self, oldest: str, newest: str) -> list[Activity]: ...

    def get_streams(self, activity_id: str) -> list[Any]: ...

    def get_wellness(self, oldest: str, newest: str) -> list[Wellness]: ...

    def download_fit_file(self, activity_id: str) -> bytes: ...


RPE_ALIASES: tuple[str, ...] = ("icu_rpe", "session_rpe", "perceived_exertion")
"""Activity-payload fields carrying the owner-entered session RPE (LOAD-12).

``icu_rpe`` is the canonical Intervals.icu field (integer scale 1-10,
editable on the activity page); ``session_rpe`` and ``perceived_exertion``
are accepted aliases. The first alias with a non-null value wins. The
separate ``feel`` field (1-5) is returned INVERTED by the API and is
deliberately IGNORED here.
"""


def extract_activity_rpe(activity: Activity) -> tuple[float | None, str | None]:
    """Extract the owner-entered session RPE from an activity payload.

    Returns ``(rpe, None)`` when a valid value is present, ``(None, None)``
    when no alias carries a value (missing or null stays None — never
    invented), or ``(None, reason)`` when a value is present but INVALID:
    non-numeric or outside the 1-10 scale. A rejected value is NEVER
    stored silently — the caller reports the reason (LOAD-12 contract).
    """
    extras = activity.model_extra or {}
    for key in RPE_ALIASES:
        raw = extras.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, (
                f"owner-entered RPE from payload field {key!r} is not a "
                f"number ({raw!r}); rejected — nothing stored (valid scale: "
                "1-10)"
            )
        value = float(raw)
        if not 1.0 <= value <= 10.0:
            return None, (
                f"owner-entered RPE from payload field {key!r} is outside "
                f"the 1-10 scale ({value!r}); rejected — nothing stored"
            )
        return value, None
    return None, None


@dataclass
class SyncResult:
    """Per-endpoint/per-item counts for one sync run.

    ``*_failed`` counts items whose fetch or persistence failed; the sync
    itself raises nothing for per-item failures — callers inspect the counts
    (partial-result summary; documented ING-5 choice).
    """

    activities_synced: int = 0
    activities_failed: int = 0
    streams_synced: int = 0
    streams_skipped: int = 0
    streams_failed: int = 0
    wellness_synced: int = 0
    wellness_failed: int = 0
    # LOAD-12: owner-entered RPE values rejected at ingest (outside the
    # 1-10 scale or non-numeric). The value is never stored silently —
    # each rejection carries a reportable reason naming the bad value.
    rpe_rejected: int = 0
    rpe_rejection_reasons: list[str] = field(default_factory=list)


class Pacer:
    """Enforce a minimum interval between successive client calls."""

    def __init__(
        self,
        min_interval_s: float,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_call = clock()

    async def before_call(self) -> None:
        """Sleep out the remainder of the interval, then stamp this call."""
        if self._min_interval_s <= 0:
            return
        elapsed = self._clock() - self._last_call
        remaining = self._min_interval_s - elapsed
        if remaining > 0:
            await self._sleep(remaining)
        self._last_call = self._clock()


async def sync_date_range(
    oldest: str,
    newest: str,
    session_factory: async_sessionmaker[AsyncSession],
    client: IntervalsClientProtocol,
    *,
    settings: Settings | None = None,
    storage: RawFileStorage | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> SyncResult:
    """Sync one date range idempotently; return per-item counts.

    Sessions are opened lazily: with no items to persist, the database is
    never touched (so pacing can be verified without one).
    """
    config = settings or get_settings()
    pacer = Pacer(config.intervals_min_request_interval_s, clock=clock, sleep=sleep)
    file_storage = storage or storage_from_settings(config)
    result = SyncResult()

    # --- Activities ---------------------------------------------------------
    await pacer.before_call()
    activities = client.list_activities(oldest, newest)

    # --- Wellness -----------------------------------------------------------
    await pacer.before_call()
    try:
        wellness_records = client.get_wellness(oldest, newest)
    except IntervalsHTTPError:
        wellness_records = []
        result.wellness_failed += 1
    if wellness_records:
        await _persist_wellness(session_factory, wellness_records, result)

    # --- Per-activity streams -------------------------------------------------
    for activity in activities:
        await _sync_activity(
            session_factory, client, pacer, file_storage, activity, result
        )

    return result


async def _sync_activity(
    session_factory: async_sessionmaker[AsyncSession],
    client: IntervalsClientProtocol,
    pacer: Pacer,
    file_storage: RawFileStorage,
    activity: Activity,
    result: SyncResult,
) -> None:
    """Fetch streams/FIT for one activity, then upsert everything.

    Client calls happen before the session opens so client failures are
    attributable to the item and leave no partial DB state behind.
    """
    activity_id = activity.id

    # OWNER-ENTERED INPUT (LOAD-12): extract before the session opens so a
    # rejected value is attributable to the item and never stored silently.
    rpe, rpe_rejection = extract_activity_rpe(activity)
    if rpe_rejection is not None:
        result.rpe_rejected += 1
        result.rpe_rejection_reasons.append(
            f"activity {activity_id}: {rpe_rejection}"
        )

    # Preferred source: the per-second FIT file (ING-4 parser); fall back to
    # the streams endpoint when no FIT bytes are available/parseable.
    fit_streams: dict[str, list[float | None]] | None = None
    endpoint_streams: list[Any] = []
    raw_file_path: str | None = None
    try:
        await pacer.before_call()
        fit_bytes = client.download_fit_file(activity_id)
    except IntervalsHTTPError:
        fit_bytes = b""
    if fit_bytes:
        try:
            fit_streams = parse_fit_streams(fit_bytes)
        except ValueError:
            fit_streams = None
        # §5.2: archive the raw bytes even when stream parsing fails so the
        # engine can re-run on them later. Best-effort: an I/O failure only
        # degrades to no stored path (ING-6 documented choice).
        try:
            raw_file_path = file_storage.save(activity_id, fit_bytes) or None
        except OSError:
            raw_file_path = None
    if fit_streams is None:
        try:
            await pacer.before_call()
            endpoint_streams = client.get_streams(activity_id)
        except IntervalsHTTPError:
            result.streams_failed += 1
            return

    try:
        async with session_factory() as session:
            row = await repository.upsert_activity(
                session,
                source_id=activity_id,
                type=activity.type,
                name=activity.name,
                start_time=_start_time(activity),
                start_time_local=activity.start_date_local or None,
                distance_m=activity.distance,
                duration_s=activity.moving_time,
                elevation_m=_numeric_extra(activity, "total_elevation_gain"),
                raw_file_path=raw_file_path,
                intervals_icu_load=_load_metric(activity),
                rpe=rpe,
            )
            if fit_streams is not None:
                await _persist_fit_streams(session, row.id, fit_streams, result)
            else:
                await _persist_endpoint_streams(
                    session, row.id, endpoint_streams, result
                )
            await session.commit()
    except Exception:
        result.activities_failed += 1
        return
    result.activities_synced += 1


async def _persist_fit_streams(
    session: AsyncSession,
    activity_row_id: int,
    fit_streams: dict[str, list[float | None]],
    result: SyncResult,
) -> None:
    """Store parsed per-second FIT arrays, skipping all-None streams.

    Mixed arrays keep ``None`` entries: the JSONB ``payload`` column is
    ``list[Any]`` and per-second alignment must not be broken; the repository
    ``list[float]`` hint is narrowed here with a documented cast.
    """
    for stream_type, values in fit_streams.items():
        if not values or all(v is None for v in values):
            result.streams_skipped += 1
            continue
        await repository.upsert_activity_stream(
            session,
            activity_id=activity_row_id,
            stream_type=stream_type,
            data=cast(list[float], values),
        )
        result.streams_synced += 1


async def _persist_endpoint_streams(
    session: AsyncSession,
    activity_row_id: int,
    streams: list[Any],
    result: SyncResult,
) -> None:
    """Store streams-endpoint payloads keyed by the source's stream type."""
    for stream in streams:
        data = [float(v) for v in stream.data]
        if not data:
            result.streams_skipped += 1
            continue
        await repository.upsert_activity_stream(
            session,
            activity_id=activity_row_id,
            stream_type=stream.type,
            data=data,
        )
        result.streams_synced += 1


async def _persist_wellness(
    session_factory: async_sessionmaker[AsyncSession],
    records: list[Wellness],
    result: SyncResult,
) -> None:
    """Upsert one batch of daily wellness records in a single transaction."""
    try:
        async with session_factory() as session:
            for record in records:
                extras = record.model_extra or {}
                await repository.upsert_wellness(
                    session,
                    date=datetime.strptime(record.id, "%Y-%m-%d").date(),
                    hrv=record.hrv,
                    # lnHrv is NOT on the wire (live-verified 2026-10-08):
                    # ln(rMSSD) is derived from the genuine hrv value in
                    # Wellness.ln_hrv — never from hrvSDNN, never NULL
                    # while a positive hrv is present.
                    ln_hrv=record.ln_hrv,
                    resting_hr=record.resting_hr,
                    sleep_minutes=record.sleep_minutes,
                    # sleepScore IS on the wire (live-verified); mapped via
                    # the Wellness field alias, not an extras lookup.
                    sleep_score=record.sleep_score,
                    weight=record.weight,
                    # §5.1: Intervals' own PMC values, stored ONLY in the
                    # explicitly non-authoritative cross-check columns.
                    intervals_icu_ctl=_numeric(extras.get("ctl")),
                    intervals_icu_atl=_numeric(extras.get("atl")),
                )
            await session.commit()
    except Exception:
        result.wellness_failed += len(records)
        return
    result.wellness_synced += len(records)


def _start_time(activity: Activity) -> datetime:
    """Resolve a timezone-aware start time from the activity payload."""
    extras = activity.model_extra or {}
    raw = extras.get("start_date") or activity.start_date_local or ""
    if not raw:
        raise ValueError(f"activity {activity.id} has no start time")
    parsed = datetime.fromisoformat(str(raw))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _load_metric(activity: Activity) -> float | None:
    """Extract Intervals.icu's own load metric as a NON-authoritative
    cross-check value only (§5.1); stored solely in the dedicated column."""
    for key in ("icu_training_load", "load"):
        value = _numeric((activity.model_extra or {}).get(key))
        if value is not None:
            return value
    return None


def _numeric_extra(activity: Activity, key: str) -> float | None:
    return _numeric((activity.model_extra or {}).get(key))


def _numeric(value: object) -> float | None:
    """Coerce a payload value to float, returning None for non-numeric values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


__all__ = [
    "RPE_ALIASES",
    "IntervalsClientProtocol",
    "Pacer",
    "SyncResult",
    "extract_activity_rpe",
    "sync_date_range",
]
