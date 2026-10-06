"""Session-durability persistence service (RID-10) — the §6 integration.

Thin, typed bridge between the stored activity streams and the pure engine
(``app.engine.durability``). Lives OUTSIDE ``app/engine`` because it does
I/O (§6 purity rule).

Pipeline of :func:`recompute_durability` over a trailing window of
``days`` calendar days ending at ``window_end``:

1. Read the window's activities with their streams (shared loading logic
   with the daily-load service, including the local session date rule).
2. Map each activity type onto a durability sport (``bike``/``run`` — the
   engine's :data:`app.engine.durability.DURABILITY_SPORT_KEYS`) with the
   same Strava-style type families the load engine documents. Anything
   else (swims, strength, walks) is reported in
   :attr:`DurabilityReport.skipped` — §7.6's durability metrics are
   defined for bike (NP) and run (NGS) only.
3. Derive the engine inputs: bike intensity = the ``power`` samples, run
   intensity = the ``speed`` samples (NGS applies the grade model from
   the ``distance``/``altitude`` streams when stored); the ``hr`` stream
   is required (EF divides by mean HR) or the activity is skipped with a
   reason.
4. Run :func:`app.engine.durability.session_decoupling` with the
   caller-supplied reference band, steadiness drift limit and NP window
   (settings via :func:`app.db.engine_readiness_config.
   durability_constants_from_settings`).
5. Upsert one ``session_durability`` row per activity via
   :func:`app.db.repository.upsert_session_durability`, stamped with
   ``engine_version`` (§6) and ``computed_at``.

``not_steady`` contract (ODD decision): a session the steadiness guard
rejected is PERSISTED as a row with ``status = "not_steady"``, NULL
EF/decoupling fields and the measured intensity drift plus the engine
``detail`` — the rejection is evidence about the session, never a
fabricated decoupling value. An engine ``ValueError`` (session too short
to split, no usable HR, misaligned streams) is NOT persisted: the row
identity is the durability RESULT, and no result exists — the activity is
reported in ``skipped`` with the engine's reason instead.

The §7.6 durability TREND (RID-9) is deliberately not persisted here: it
is recomputable from the persisted per-session ``decoupling`` values
(joined to ``activity`` for the eligibility date/duration), so a stored
trend row would be a redundant copy.

All writes flush without committing; the caller owns the transaction.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import upsert_session_durability
from app.engine.durability import (
    DEFAULT_DECOUPLING_REFERENCE_BAND,
    DEFAULT_MAX_HALF_INTENSITY_DRIFT,
    SessionDecoupling,
    session_decoupling,
)
from app.services.daily_load import (
    _load_window_activities,
)

__all__ = [
    "DurabilityReport",
    "SkippedActivity",
    "recompute_durability",
]


_CYCLING_ACTIVITY_TYPES: Final[frozenset[str]] = frozenset(
    {"ride", "virtualride", "gravelride", "mountainbikeride", "ebikeride"}
)
_RUNNING_ACTIVITY_TYPES: Final[frozenset[str]] = frozenset(
    {"run", "trailrun", "treadmillrun", "virtualrun"}
)


@dataclass(frozen=True)
class SkippedActivity:
    """One activity with no persisted durability row, with the reason."""

    activity_id: int
    sport: str
    reason: str


@dataclass(frozen=True)
class DurabilityReport:
    """Outcome of one :func:`recompute_durability` run (human + test facing).

    ``rows_upserted`` counts persisted rows (``ok`` AND ``not_steady``);
    ``not_steady_rows`` the rejected-but-persisted rows; ``skipped`` lists
    every activity with NO row, with its reason.
    """

    window_start: dt.date
    window_end: dt.date
    engine_version: str
    activities_considered: int
    rows_upserted: int
    not_steady_rows: int
    skipped: tuple[SkippedActivity, ...] = field(default_factory=tuple)


def _durability_sport(activity_type: str) -> str | None:
    """The engine durability sport of a stored activity type, or ``None``."""
    normalized = activity_type.strip().lower()
    if normalized in _CYCLING_ACTIVITY_TYPES:
        return "bike"
    if normalized in _RUNNING_ACTIVITY_TYPES:
        return "run"
    return None


async def recompute_durability(
    session: AsyncSession,
    *,
    window_end: dt.date,
    days: int,
    engine_version: str,
    athlete_id: int = 1,
    reference_band: float = DEFAULT_DECOUPLING_REFERENCE_BAND,
    max_intensity_drift: float = DEFAULT_MAX_HALF_INTENSITY_DRIFT,
    np_window_samples: int = 30,
    np_min_valid_fraction: float = 1.0,
) -> DurabilityReport:
    """Recompute and persist one ``session_durability`` row per window
    activity that has a decoupling result.

    See the module docstring for the pipeline and the not_steady/skipped
    contracts. ``days`` must be positive; the window is the ``days``
    calendar days ending at (and including) ``window_end``. The reference
    band, drift limit and NP parameters are explicit keywords (§14) whose
    defaults are the pure engine's documented constants — the CLI
    supplies the effective values from Settings via
    :func:`app.db.engine_readiness_config.durability_constants_from_settings`,
    they are never re-hardcoded here.

    Upserts are idempotent per ``activity_id``; flushes without
    committing. ``athlete_id`` is accepted for interface symmetry with the
    other recompute services (the durability row identity is the
    activity, which belongs to the single-athlete system).
    """
    if days <= 0:
        raise ValueError(f"days must be positive, got {days!r}")
    window_start = window_end - dt.timedelta(days=days - 1)

    candidates = await _load_window_activities(
        session, window_start=window_start, window_end=window_end
    )

    computed_at = dt.datetime.now(dt.UTC)
    rows_upserted = 0
    not_steady_rows = 0
    skipped: list[SkippedActivity] = []

    for activity, streams in candidates:
        sport = _durability_sport(activity.type)
        if sport is None:
            skipped.append(
                SkippedActivity(
                    activity_id=activity.id,
                    sport=activity.type,
                    reason=(
                        f"{activity.type!r} is not a §7.6 durability sport: "
                        "EF/decoupling are defined for bike (NP) and run "
                        "(NGS) only"
                    ),
                )
            )
            continue
        intensity_stream = "power" if sport == "bike" else "speed"
        if intensity_stream not in streams:
            skipped.append(
                SkippedActivity(
                    activity_id=activity.id,
                    sport=activity.type,
                    reason=(
                        f"no {intensity_stream!r} stream: the "
                        f"{'NP' if sport == 'bike' else 'NGS'} intensity "
                        "measure cannot be computed"
                    ),
                )
            )
            continue
        if "hr" not in streams:
            skipped.append(
                SkippedActivity(
                    activity_id=activity.id,
                    sport=activity.type,
                    reason=(
                        "no 'hr' stream: EF divides by mean HR, so no "
                        "durability value can exist"
                    ),
                )
            )
            continue
        try:
            result: SessionDecoupling = session_decoupling(
                sport,  # type: ignore[arg-type]
                list(streams[intensity_stream]),
                list(streams["hr"]),
                distance_samples=(
                    list(streams["distance"])
                    if sport == "run" and "distance" in streams
                    else None
                ),
                altitude_samples=(
                    list(streams["altitude"])
                    if sport == "run" and "altitude" in streams
                    else None
                ),
                reference_band=reference_band,
                max_intensity_drift=max_intensity_drift,
                np_window_samples=np_window_samples,
                np_min_valid_fraction=np_min_valid_fraction,
            )
        except ValueError as exc:
            # No durability RESULT exists for this activity: nothing is
            # persisted, the reason is reported (never silent).
            skipped.append(
                SkippedActivity(
                    activity_id=activity.id,
                    sport=activity.type,
                    reason=str(exc),
                )
            )
            continue
        await upsert_session_durability(
            session,
            activity_id=activity.id,
            sport=result.sport,
            status=result.status,
            ef_first_half=result.ef_first_half,
            ef_second_half=result.ef_second_half,
            decoupling=result.decoupling,
            decoupling_pct=result.decoupling_pct,
            within_reference_band=result.within_reference_band,
            reference_band=result.reference_band,
            intensity_first_half=result.intensity_first_half,
            intensity_second_half=result.intensity_second_half,
            intensity_drift=result.intensity_drift,
            max_intensity_drift=result.max_intensity_drift,
            n_samples=result.n_samples,
            n_first_half=result.n_first_half,
            n_second_half=result.n_second_half,
            detail=result.detail,
            engine_version=engine_version,
            computed_at=computed_at,
        )
        rows_upserted += 1
        if result.status == "not_steady":
            not_steady_rows += 1

    return DurabilityReport(
        window_start=window_start,
        window_end=window_end,
        engine_version=engine_version,
        activities_considered=len(candidates),
        rows_upserted=rows_upserted,
        not_steady_rows=not_steady_rows,
        skipped=tuple(skipped),
    )
