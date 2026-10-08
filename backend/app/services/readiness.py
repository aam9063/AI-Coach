"""Readiness persistence service (RID-10) — the §6 integration layer.

Thin, typed bridge between the stored readiness inputs and the pure engine
(``app.engine.readiness``). Lives OUTSIDE ``app/engine`` because it does
I/O: the engine never imports DB or service code (§6 purity rule).

Pipeline of :func:`recompute_readiness` over a trailing window of ``days``
calendar days ending at ``window_end``:

1. Read the athlete's ``wellness`` rows covering the window PLUS the
   longest lookback the signals need (HRV: ``hrv_window_days +
   hrv_baseline_days`` series days; resting HR / sleep:
   ``*_baseline_days + 1``). Missing days and missing measurements become
   explicit ``None`` entries — the engine's daily-series convention
   (missing days must be present as ``None``, never omitted).
2. Read the TSB context from the ``daily_load`` ``combined`` rows (the
   load model's output, Feature 3). A window day WITHOUT a TSB row is
   reported in :attr:`ReadinessReport.skipped` and gets NO readiness row:
   TSB is a required assessment input and is never fabricated.
3. Per window day, build the pure-engine inputs: the ln(rMSSD) series
   uses the stored ``wellness.ln_hrv`` and falls back to ``ln(hrv)`` only
   when the stored ln value is missing but the rMSSD is present; sleep is
   passed in MINUTES (the wellness storage unit — the engine is
   unit-agnostic as long as the series is consistent); resting HR in bpm.
   The three signals are computed and combined by
   :func:`app.engine.readiness.readiness_assessment`. The subjective-
   fatigue context signal comes from the ``subjective_log`` row of that
   day (WA-6): a reported fatigue level (any value on the documented
   1-10 scale) reaches the assessment as ``subjective_fatigue_reported
   = True`` — the engine consumes a BOOL (§7.4), so the LEVEL is
   recorded but the VERDICT only sees "reported"; a day without a
   subjective-log row (or with fatigue NULL) passes ``None``/``False`` =
   not reported. ACWR is context-only and not persisted on
   ``daily_load`` — §7.2.
4. Upsert one ``readiness_snapshot`` row per (athlete, day) via
   :func:`app.db.repository.upsert_readiness_snapshot`, stamped with
   ``engine_version`` (§6) and ``computed_at``.

``insufficient_data`` contract (ODD decision): a signal the engine
reported as ``insufficient_data`` is persisted inside the JSONB
``signals`` with ``status == "insufficient_data"`` and NULL observed /
baseline fields — the missing measurement is reported AS missing, never
substituted with a default or zero (§7.4; the owner's wellness rows are
partly unpopulated, and those days MUST stay visibly insufficient).

All writes flush without committing; the caller owns the transaction.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyLoadRow, SubjectiveLogRow, WellnessRow
from app.db.repository import upsert_readiness_snapshot
from app.engine.readiness import (
    DEFAULT_HRV_BAND_SD,
    DEFAULT_HRV_BASELINE_DAYS,
    DEFAULT_HRV_MIN_BASELINE_VALID_DAYS,
    DEFAULT_HRV_WINDOW_DAYS,
    DEFAULT_RHR_BAND_SD,
    DEFAULT_RHR_BASELINE_DAYS,
    DEFAULT_RHR_MIN_BASELINE_VALID_DAYS,
    DEFAULT_SLEEP_BAND_SD,
    DEFAULT_SLEEP_BASELINE_DAYS,
    DEFAULT_SLEEP_MIN_BASELINE_VALID_DAYS,
    DEFAULT_TSB_VERY_NEGATIVE,
    ReadinessAssessment,
    hrv_readiness,
    readiness_assessment,
    resting_hr_readiness,
    sleep_readiness,
)

__all__ = [
    "ReadinessReport",
    "SkippedDay",
    "recompute_readiness",
]


@dataclass(frozen=True)
class SkippedDay:
    """One window day that got NO readiness row, with the concrete reason.

    Reporting is the contract: nothing is ever dropped silently (the
    daily_load precedent, LOAD-10).
    """

    date: dt.date
    reason: str


@dataclass(frozen=True)
class ReadinessReport:
    """Outcome of one :func:`recompute_readiness` run (human + test facing).

    ``days_considered`` is the window length; ``rows_upserted`` the number
    of persisted ``readiness_snapshot`` rows; ``skipped`` lists every
    window day that could not be assessed, with its reason.
    """

    window_start: dt.date
    window_end: dt.date
    engine_version: str
    days_considered: int
    rows_upserted: int
    skipped: tuple[SkippedDay, ...] = field(default_factory=tuple)


def _ln_hrv_value(row: WellnessRow) -> float | None:
    """The daily ln(rMSSD) value: the stored ``ln_hrv``, or ``ln(hrv)``
    computed from the stored rMSSD when only the rMSSD was stored.

    A non-positive rMSSD is treated as a missing measurement (``None``) —
    the engine requires positive values and a broken value must not raise
    the whole window's recompute.
    """
    if row.ln_hrv is not None:
        return row.ln_hrv
    if row.hrv is not None and row.hrv > 0.0:
        return math.log(row.hrv)
    return None


def _slice_series(
    full: dict[dt.date, float | None], *, last_day: dt.date, length: int
) -> dict[dt.date, float | None]:
    """The trailing ``length``-day slice of a contiguous series ending
    (inclusive) at ``last_day`` — the engine's per-as-of-day input shape.
    """
    first = last_day - dt.timedelta(days=length - 1)
    return {day: full[day] for day in (first + dt.timedelta(days=i) for i in range(length))}


async def recompute_readiness(
    session: AsyncSession,
    *,
    window_end: dt.date,
    days: int,
    engine_version: str,
    athlete_id: int = 1,
    hrv_window_days: int = DEFAULT_HRV_WINDOW_DAYS,
    hrv_baseline_days: int = DEFAULT_HRV_BASELINE_DAYS,
    hrv_band_sd: float = DEFAULT_HRV_BAND_SD,
    hrv_min_baseline_valid_days: int = DEFAULT_HRV_MIN_BASELINE_VALID_DAYS,
    min_window_valid_fraction: float = 1.0,
    rhr_baseline_days: int = DEFAULT_RHR_BASELINE_DAYS,
    rhr_band_sd: float = DEFAULT_RHR_BAND_SD,
    rhr_min_baseline_valid_days: int = DEFAULT_RHR_MIN_BASELINE_VALID_DAYS,
    sleep_baseline_days: int = DEFAULT_SLEEP_BASELINE_DAYS,
    sleep_band_sd: float = DEFAULT_SLEEP_BAND_SD,
    sleep_min_baseline_valid_days: int = DEFAULT_SLEEP_MIN_BASELINE_VALID_DAYS,
    tsb_very_negative_below: float = DEFAULT_TSB_VERY_NEGATIVE,
) -> ReadinessReport:
    """Recompute and persist the readiness snapshots for a trailing window.

    See the module docstring for the pipeline and the insufficient-data
    contract. ``days`` must be positive; the window is the ``days``
    calendar days ending at (and including) ``window_end``. The signal
    parameters are explicit keywords (§14) whose defaults are the pure
    engine's documented constants — the CLI supplies the effective values
    from Settings via :func:`app.db.engine_readiness_config.
    readiness_constants_from_settings`, they are never re-hardcoded here.

    Upserts are idempotent per ``(athlete_id, date)``; flushes without
    committing.
    """
    if days <= 0:
        raise ValueError(f"days must be positive, got {days!r}")
    window_start = window_end - dt.timedelta(days=days - 1)
    window_dates = [window_start + dt.timedelta(days=i) for i in range(days)]

    # Longest series the per-day inputs need (HRV: rolling window +
    # baseline; resting HR / sleep: baseline plus the as-of day).
    series_length = max(
        hrv_window_days + hrv_baseline_days,
        rhr_baseline_days + 1,
        sleep_baseline_days + 1,
    )
    series_start = window_start - dt.timedelta(days=series_length - 1)

    wellness_rows = (
        (
            await session.execute(
                select(WellnessRow).where(
                    WellnessRow.athlete_id == athlete_id,
                    WellnessRow.date >= series_start,
                    WellnessRow.date <= window_end,
                )
            )
        )
        .scalars()
        .all()
    )
    # Contiguous daily mappings: a missing wellness DAY is an explicit
    # None (the engine's daily-series convention), never a calendar gap.
    ln_hrv: dict[dt.date, float | None] = {}
    resting_hr: dict[dt.date, float | None] = {}
    sleep_minutes: dict[dt.date, float | None] = {}
    day = series_start
    while day <= window_end:
        ln_hrv[day] = None
        resting_hr[day] = None
        sleep_minutes[day] = None
        day += dt.timedelta(days=1)
    for row in wellness_rows:
        ln_hrv[row.date] = _ln_hrv_value(row)
        resting_hr[row.date] = row.resting_hr
        sleep_minutes[row.date] = (
            float(row.sleep_minutes) if row.sleep_minutes is not None else None
        )

    tsb_rows = (
        (
            await session.execute(
                select(DailyLoadRow).where(
                    DailyLoadRow.athlete_id == athlete_id,
                    DailyLoadRow.date >= window_start,
                    DailyLoadRow.date <= window_end,
                    DailyLoadRow.sport == "combined",
                )
            )
        )
        .scalars()
        .all()
    )
    tsb_by_date = {row.date: row.tsb for row in tsb_rows}

    # The subjective-fatigue context signal (§7.4, WA-6): the owner's own
    # report for each window day, from ``subjective_log``. The engine
    # consumes a BOOL ("fatigue reported today"); any reported level
    # counts, a day without a report (or with fatigue NULL) does not.
    subjective_rows = (
        (
            await session.execute(
                select(SubjectiveLogRow).where(
                    SubjectiveLogRow.athlete_id == athlete_id,
                    SubjectiveLogRow.date >= window_start,
                    SubjectiveLogRow.date <= window_end,
                )
            )
        )
        .scalars()
        .all()
    )
    fatigue_reported_by_date: dict[dt.date, bool] = {
        row.date: row.fatigue is not None for row in subjective_rows
    }

    computed_at = dt.datetime.now(dt.UTC)
    skipped: list[SkippedDay] = []
    rows_upserted = 0

    for as_of in window_dates:
        tsb = tsb_by_date.get(as_of)
        if tsb is None:
            skipped.append(
                SkippedDay(
                    date=as_of,
                    reason=(
                        "no daily_load combined row for this date: the TSB "
                        "context input is unavailable (run "
                        "python -m app.db.daily_load first); never fabricated"
                    ),
                )
            )
            continue
        signals = [
            hrv_readiness(
                _slice_series(
                    ln_hrv, last_day=as_of, length=hrv_window_days + hrv_baseline_days
                ),
                window_days=hrv_window_days,
                baseline_days=hrv_baseline_days,
                band_sd=hrv_band_sd,
                min_window_valid_fraction=min_window_valid_fraction,
                min_baseline_valid_days=hrv_min_baseline_valid_days,
            ),
            resting_hr_readiness(
                _slice_series(resting_hr, last_day=as_of, length=rhr_baseline_days + 1),
                baseline_days=rhr_baseline_days,
                band_sd=rhr_band_sd,
                min_baseline_valid_days=rhr_min_baseline_valid_days,
            ),
            sleep_readiness(
                _slice_series(
                    sleep_minutes, last_day=as_of, length=sleep_baseline_days + 1
                ),
                baseline_days=sleep_baseline_days,
                band_sd=sleep_band_sd,
                min_baseline_valid_days=sleep_min_baseline_valid_days,
            ),
        ]
        assessment: ReadinessAssessment = readiness_assessment(
            signals,
            tsb=tsb,
            subjective_fatigue_reported=fatigue_reported_by_date.get(as_of),
            acwr=None,
            tsb_very_negative_below=tsb_very_negative_below,
        )
        await upsert_readiness_snapshot(
            session,
            athlete_id=athlete_id,
            date=as_of,
            signals=[asdict(signal) for signal in assessment.signals],
            adverse_signal_keys=assessment.adverse_signal_keys,
            agreement_count=assessment.agreement_count,
            suggest_reduce_intensity=assessment.suggest_reduce_intensity,
            suggestion=assessment.suggestion,
            reasons=assessment.reasons,
            tsb=assessment.tsb,
            tsb_very_negative_below=assessment.tsb_very_negative_below,
            subjective_fatigue_reported=assessment.subjective_fatigue_reported,
            acwr=assessment.acwr,
            engine_version=engine_version,
            computed_at=computed_at,
        )
        rows_upserted += 1

    return ReadinessReport(
        window_start=window_start,
        window_end=window_end,
        engine_version=engine_version,
        days_considered=days,
        rows_upserted=rows_upserted,
        skipped=tuple(skipped),
    )
