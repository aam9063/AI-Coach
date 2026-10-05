"""PMC cross-check against Intervals.icu (LOAD-10, PROJECT_BRIEF §12.3).

CROSS-CHECK ONLY (§5.1): this tool compares our engine's Performance
Manager values (:func:`app.engine.pmc.compute_pmc`) against the PMC values
Intervals.icu itself computed, stored in the explicitly NON-authoritative
``wellness.intervals_icu_ctl`` / ``wellness.intervals_icu_atl`` columns.
Intervals' values are NEVER authoritative training state — the engine
computes truth from raw streams — and nothing in this tool writes them
anywhere. The output says so on every run.

Like-with-like: the two definitions differ on strength sessions
---------------------------------------------------------------
Established on the owner's real data (177 wellness days, 26 activities of
which 23 carry an ``intervals_icu_load``; all 26 stored inputs equal the
live API values — the INPUTS are not drifting; the DEFINITIONS differ):

1. **Intervals' CTL excludes strength sessions' load; their ATL includes
   it.** Our engine deliberately includes strength load in both (correct
   product behaviour for a triathlete who lifts). Measured consequences
   over the 177-day window: our CTL fed with ALL loads matches theirs on
   only 67/177 days, while our CTL fed with ONLY aerobic loads matches on
   **153/177**. Each divergence point equals the decayed excluded load:
   ``7k = 0.1647`` on 2026-06-17, ``8k`` on 2026-07-04, ``13k`` on
   2026-07-07, with ``k = 1 - e^(-1/42)`` (28 strength load points in
   total). This is a definition difference — never a licence to widen the
   tolerance.
2. Therefore the tool computes and reports TWO comparisons against their
   series: (a) ``all_loads`` — our full definition, strength included, and
   (b) ``aerobic_only`` — their CTL definition (strength excluded, via the
   engine's own classification :func:`app.engine.load.is_strength_sport`,
   imported rather than duplicated so it cannot drift).
3. **The headline verdict is like-for-like**: CTL is judged on the
   ``aerobic_only`` view (their CTL definition) and ATL on the
   ``all_loads`` view (their ATL includes strength). Both tables stay
   fully visible so nothing is hidden.
4. **Intervals-side revisions exist.** Two discontinuities on
   activity-free days were measured: their ATL drops ``0.4462`` on
   2026-07-19 and their CTL drops ``0.4982`` on 2026-08-04 — nothing in
   the activity data explains either. The tool detects such days (their
   day-over-day change deviating from the pure-decay prediction by more
   than :data:`DEFAULT_DISCONTINUITY_THRESHOLD` CTL/ATL points on a day
   with zero activity load) and lists them explicitly as "Intervals-side
   revision (no activity explains it)", excluding them from that series'
   deviation statistics and verdict instead of counting them as our error.
5. **A pure relative tolerance is unusable** for a decaying series near
   zero: on real data ATL once reported a 223% relative deviation on
   values of order ``1e-4``. Hence the hybrid tolerance: a day passes when
   the relative deviation is within ``--tolerance`` (default 0.10) OR the
   absolute deviation is within ``--absolute-tolerance`` (default 0.5
   CTL/ATL points — a documented, configurable bound motivated by the
   decay-tail insensitivity: differences below it are smaller than the
   residual seed/rounding noise of the recursion). Both statistics are
   reported per series and the output says which bound each day satisfied.

Both sides are fed the SAME load inputs: our daily load series is built by
summing the activities' stored ``activity.intervals_icu_load`` column per
day (non-authoritative input values, same day attribution as the
persistence service :func:`app.services.daily_load._activity_date` —
imported, not duplicated, so the attribution cannot drift). With identical
inputs on both sides, only the PMC recursion and the definition split are
under test (§12.3: "PMC values within an agreed tolerance of Intervals.icu
for the same data").

Seed-decay window (why >= 180 days): the PMC recursion starts from the
documented zero seeds (:func:`compute_pmc`), while Intervals' stored values
may come from its own longer history. A fixed seed error is damped by the
recursion as ``e^(-n / tau_ctl)``; with ``tau_ctl = 42`` days
(:data:`app.engine.pmc.DEFAULT_TAU_CTL_DAYS`), 180 days leave
``e^(-180/42) ~= 1.4%`` of the initial seed influence (200 days ~= 0.9%),
so the residual seed error stays far inside the tolerance. Windows shorter
than ~180 days are seed-dominated and will overstate real deviations.

Tolerance: relative deviation ``|ours - theirs| / |theirs|``, default
``±10%`` (:data:`DEFAULT_TOLERANCE`), OR absolute deviation
``|ours - theirs|`` within :data:`DEFAULT_ABSOLUTE_TOLERANCE` (0.5 points).
The boundaries are inclusive. Missing cross-check values (no wellness row,
or a NULL column) are REPORTED per date — never silently skipped and never
invented. The tool is strictly read-only.

Entry points:

- :func:`detect_their_side_revisions` — pure Intervals-side revision
  detection (deterministically tested against synthetic series).
- :func:`compare_pmc_series` — pure comparison logic (deterministically
  tested against synthetic series; the owner's live data is never used in
  tests).
- :func:`run_cross_check` — one async DB pass: builds both daily series,
  runs :func:`compute_pmc` over each, compares.
- :func:`main` — argparse CLI (``python -m app.tools.cross_check_pmc
  --days N [--tolerance 0.10] [--absolute-tolerance 0.5]
  [--end YYYY-MM-DD] [--athlete-id 1]``); exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from datetime import time as dt_time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings, get_settings
from app.db.models import ActivityRow, WellnessRow
from app.db.session import create_db_engine, make_session_factory
from app.engine.load import is_strength_sport
from app.engine.pmc import (
    DEFAULT_TAU_ATL_DAYS,
    DEFAULT_TAU_CTL_DAYS,
    PmcDay,
    compute_pmc,
)
from app.ingest.backfill import today_in_zone

# Day attribution MUST be identical to the persistence service, so the
# function is imported rather than duplicated (it is private by convention;
# the import is the documented coupling point).
from app.services.daily_load import _activity_date

__all__ = [
    "DEFAULT_ABSOLUTE_TOLERANCE",
    "DEFAULT_DISCONTINUITY_THRESHOLD",
    "DEFAULT_TOLERANCE",
    "MIN_CROSS_CHECK_WINDOW_DAYS",
    "CrossCheckRun",
    "MetricComparison",
    "MetricDeviationDay",
    "PmcCrossCheckDay",
    "PmcCrossCheckResult",
    "TheirSideRevision",
    "build_daily_load_series",
    "compare_pmc_series",
    "detect_their_side_revisions",
    "relative_deviation",
    "run_cross_check",
]

DEFAULT_TOLERANCE = 0.10
"""Default relative tolerance: ±10% — OWNER-AGREED (§12.3 "agreed
tolerance"). A day passes when the relative deviation is within this
bound OR the absolute deviation is within
:data:`DEFAULT_ABSOLUTE_TOLERANCE`; the hybrid rule and both defaults
(``--tolerance 0.10``, ``--absolute-tolerance 0.5``) were confirmed by
the owner."""

DEFAULT_ABSOLUTE_TOLERANCE = 0.5
"""Default absolute tolerance in CTL/ATL points: a day also passes when
``|ours - theirs|`` is within this bound. Motivated by the decay-tail
insensitivity: for a decaying series near zero a relative deviation blows
up (223% observed on values of order 1e-4) while the absolute difference
stays in the sub-point noise floor of the recursion."""

DEFAULT_DISCONTINUITY_THRESHOLD = 0.25
"""Their-side revision threshold in CTL/ATL points: on a day with ZERO
activity load, their value must equal the pure-decay prediction
``previous * e^(-1/tau)`` (tau = 42 for CTL, 7 for ATL). A deviation from
that prediction beyond this threshold means Intervals revised its stored
value without any activity explaining it (measured real-data cases: 0.4462
ATL points on 2026-07-19, 0.4982 CTL points on 2026-08-04 — both well above
this threshold, while exact-decay days deviate only by float noise)."""

MIN_CROSS_CHECK_WINDOW_DAYS = 180
"""Recommended minimum window: 180 days leave only e^(-180/42) ~= 1.4% of
the zero-seed influence in the CTL recursion (tau_ctl = 42 days), so the
seed contribution is negligible against the ±10% tolerance. Shorter
windows are seed-dominated and overstate deviations."""


def relative_deviation(ours: float, theirs: float | None) -> float | None:
    """Relative deviation ``|ours - theirs| / |theirs|`` (their baseline).

    Returns ``None`` when the day is NOT comparable: the Intervals value is
    missing (``None``), or it is 0 while ours is not (a relative deviation
    against a zero baseline is undefined — inventing one is forbidden; the
    day is reported as missing/undefined instead). Both zero is exactly 0.0
    (perfect agreement on a rest day).
    """
    if theirs is None:
        return None
    if theirs == 0.0:
        return 0.0 if ours == 0.0 else None
    return abs(ours - theirs) / abs(theirs)


def absolute_deviation(ours: float, theirs: float | None) -> float | None:
    """Absolute deviation ``|ours - theirs|`` in CTL/ATL points.

    ``None`` exactly when the Intervals value is missing (not comparable);
    everything else — including both zero — is a defined distance.
    """
    if theirs is None:
        return None
    return abs(ours - theirs)


@dataclass(frozen=True, slots=True)
class TheirSideRevision:
    """One detected Intervals-side revision (no activity explains it).

    ``expected_value`` is the pure-decay prediction from their own
    previous stored value (``previous * e^(-1/tau)`` for the series' time
    constant); ``deviation`` is ``|actual - expected|``, which exceeded
    the configured threshold on a day with zero activity load.
    """

    date: date_cls
    series: str  # "CTL" or "ATL"
    previous_date: date_cls
    previous_value: float
    expected_value: float
    actual_value: float
    deviation: float


def detect_their_side_revisions(
    daily_loads: Mapping[date_cls, float],
    their_ctl: Mapping[date_cls, float | None],
    their_atl: Mapping[date_cls, float | None],
    *,
    threshold: float = DEFAULT_DISCONTINUITY_THRESHOLD,
) -> tuple[TheirSideRevision, ...]:
    """Detect days where THEIR series moved without any activity explaining it.

    A day ``d`` is a suspected Intervals-side revision for a series when
    ALL of the following hold:

    - our all-loads daily series has ``0.0`` load on ``d`` (no activity
      that day, per the shared day attribution);
    - both ``d - 1`` and ``d`` are present with non-``None`` values in
      that series of theirs (a missing value disables detection — the
      prediction needs their own previous value);
    - their actual value on ``d`` deviates from the pure-decay prediction
      ``their[d-1] * e^(-1/tau)`` by more than ``threshold`` points
      (``tau`` = 42 for CTL, 7 for ATL — the documented time constants).

    A pure-decay day deviates only by float noise, so normal rest days are
    never flagged; both directions (a jump up or an extra drop) count.
    These days are reported as "Intervals-side revision (no activity
    explains it)" and excluded from that series' statistics and verdict —
    their error is not our error.
    """
    if threshold <= 0.0:
        raise ValueError(
            f"threshold must be positive, got {threshold!r}"
        )
    revisions: list[TheirSideRevision] = []
    for day, load in sorted(daily_loads.items()):
        if load != 0.0:
            continue  # activity load that day explains any movement
        previous = day - timedelta(days=1)
        if previous not in daily_loads:
            continue  # window edge: no previous day to predict from
        for series, values, tau in (
            ("CTL", their_ctl, DEFAULT_TAU_CTL_DAYS),
            ("ATL", their_atl, DEFAULT_TAU_ATL_DAYS),
        ):
            previous_value = values.get(previous)
            actual_value = values.get(day)
            if previous_value is None or actual_value is None:
                continue
            expected = previous_value * math.exp(-1.0 / tau)
            deviation = abs(actual_value - expected)
            if deviation > threshold:
                revisions.append(
                    TheirSideRevision(
                        date=day,
                        series=series,
                        previous_date=previous,
                        previous_value=previous_value,
                        expected_value=expected,
                        actual_value=actual_value,
                        deviation=deviation,
                    )
                )
    return tuple(revisions)


@dataclass(frozen=True, slots=True)
class PmcCrossCheckDay:
    """One date of the cross-check: both sides' values in BOTH load views.

    ``our_*_all`` is our engine over the all-loads series (strength
    included); ``our_*_aerobic`` over the aerobic-only series (strength
    excluded — Intervals' CTL definition). The Intervals values are the
    stored cross-check columns; ``None`` means missing and is reported,
    never invented.
    """

    date: date_cls
    tss_all: float
    our_ctl_all: float
    our_ctl_aerobic: float
    our_atl_all: float
    our_atl_aerobic: float
    their_ctl: float | None
    their_atl: float | None


@dataclass(frozen=True, slots=True)
class MetricDeviationDay:
    """One date of ONE metric's comparison in ONE load view.

    ``within_rel``/``within_abs`` say which tolerance bound the day
    satisfied (hybrid tolerance: passing = either). ``passed`` is False
    for a missing Intervals value (nothing verified that day).
    ``their_side_excluded`` marks a detected Intervals-side revision day:
    excluded from the statistics and the verdict (their error, not ours).
    """

    date: date_cls
    our_value: float
    their_value: float | None
    rel_dev: float | None
    abs_dev: float | None
    within_rel: bool
    within_abs: bool
    passed: bool
    their_side_excluded: bool


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """One metric (CTL or ATL) compared in one load view, with statistics.

    Statistics are taken over comparable (Intervals value present),
    non-excluded days only. ``days_within_rel``/``days_within_abs`` count
    which bound each comparable day satisfied; ``failed_dates`` lists the
    non-excluded comparable days that failed BOTH bounds.
    """

    metric: str  # "CTL" or "ATL"
    view: str  # "all_loads" or "aerobic_only"
    days: tuple[MetricDeviationDay, ...]
    median_rel_dev: float | None
    max_rel_dev: float | None
    median_abs_dev: float | None
    max_abs_dev: float | None
    days_within_rel: int
    days_within_abs: int
    days_failed_both: int
    failed_dates: tuple[date_cls, ...]

    @property
    def comparable_days(self) -> int:
        return sum(
            1
            for day in self.days
            if day.their_value is not None and not day.their_side_excluded
        )

    @property
    def passed(self) -> bool:
        """True when at least one day was verified and none failed."""
        return self.comparable_days > 0 and not self.failed_dates


@dataclass(frozen=True, slots=True)
class PmcCrossCheckResult:
    """Aggregated cross-check outcome (§12.3 verdict per date and overall).

    All four metric/view comparisons are present; the headline ``passed``
    is the LIKE-FOR-LIKE conjunction: ``ctl_aerobic_only.passed`` (their
    CTL excludes strength) AND ``atl_all_loads.passed`` (their ATL
    includes strength). A metric passes when at least one comparable day
    exists and every non-excluded comparable day satisfies the hybrid
    tolerance (relative within ``tolerance`` OR absolute within
    ``absolute_tolerance``). Intervals-side revision days are excluded
    from the affected series' statistics and verdict but always reported.
    """

    tolerance: float
    absolute_tolerance: float
    discontinuity_threshold: float
    days: tuple[PmcCrossCheckDay, ...]
    ctl_all_loads: MetricComparison
    ctl_aerobic_only: MetricComparison
    atl_all_loads: MetricComparison
    atl_aerobic_only: MetricComparison
    their_ctl_revisions: tuple[TheirSideRevision, ...]
    their_atl_revisions: tuple[TheirSideRevision, ...]
    missing_ctl_dates: tuple[date_cls, ...]
    missing_atl_dates: tuple[date_cls, ...]

    @property
    def passed(self) -> bool:
        return self.ctl_aerobic_only.passed and self.atl_all_loads.passed

    @property
    def all_revisions(self) -> tuple[TheirSideRevision, ...]:
        return self.their_ctl_revisions + self.their_atl_revisions


def _stats(values: Sequence[float]) -> tuple[float | None, float | None]:
    """(median, max) of already-absolute deviations; ``(None, None)`` when empty."""
    if not values:
        return None, None
    return statistics.median(values), max(values)


def _metric_comparison(
    metric: str,
    view: str,
    our_by_date: Mapping[date_cls, float],
    days: Sequence[PmcCrossCheckDay],
    excluded_dates: frozenset[date_cls],
    *,
    tolerance: float,
    absolute_tolerance: float,
) -> MetricComparison:
    """Build one metric/view comparison over the shared cross-check days."""
    deviation_days: list[MetricDeviationDay] = []
    rel_devs: list[float] = []
    abs_devs: list[float] = []
    within_rel_count = 0
    within_abs_count = 0
    failed_both = 0
    failed_dates: list[date_cls] = []
    for day in days:
        ours = our_by_date[day.date]
        theirs = day.their_ctl if metric == "CTL" else day.their_atl
        rel = relative_deviation(ours, theirs)
        absolute = absolute_deviation(ours, theirs)
        excluded = day.date in excluded_dates
        within_rel = rel is not None and rel <= tolerance
        within_abs = absolute is not None and absolute <= absolute_tolerance
        comparable = theirs is not None
        passed = (within_rel or within_abs) if comparable else False
        if comparable and not excluded:
            if rel is not None:
                rel_devs.append(rel)
            if absolute is not None:
                abs_devs.append(absolute)
            if within_rel:
                within_rel_count += 1
            if within_abs:
                within_abs_count += 1
            if not passed:
                failed_both += 1
                failed_dates.append(day.date)
        deviation_days.append(
            MetricDeviationDay(
                date=day.date,
                our_value=ours,
                their_value=theirs,
                rel_dev=rel,
                abs_dev=absolute,
                within_rel=within_rel,
                within_abs=within_abs,
                passed=passed,
                their_side_excluded=excluded,
            )
        )
    median_rel, max_rel = _stats(rel_devs)
    median_abs, max_abs = _stats(abs_devs)
    return MetricComparison(
        metric=metric,
        view=view,
        days=tuple(deviation_days),
        median_rel_dev=median_rel,
        max_rel_dev=max_rel,
        median_abs_dev=median_abs,
        max_abs_dev=max_abs,
        days_within_rel=within_rel_count,
        days_within_abs=within_abs_count,
        days_failed_both=failed_both,
        failed_dates=tuple(failed_dates),
    )


def compare_pmc_series(
    pmc_days_all: Iterable[PmcDay],
    pmc_days_aerobic: Iterable[PmcDay],
    their_values: Mapping[date_cls, tuple[float | None, float | None]],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    absolute_tolerance: float = DEFAULT_ABSOLUTE_TOLERANCE,
    discontinuity_threshold: float = DEFAULT_DISCONTINUITY_THRESHOLD,
) -> PmcCrossCheckResult:
    """Compare our PMC days (BOTH load views) against Intervals' stored values.

    ``pmc_days_all`` is our engine over the all-loads series (strength
    included), ``pmc_days_aerobic`` over the aerobic-only series; both must
    cover the same dates in the same order (they come from
    :func:`compute_pmc` over the same window). ``their_values`` maps a date
    to ``(intervals_icu_ctl, intervals_icu_atl)``; an absent date or a
    ``None`` component is a missing cross-check value, reported in
    ``missing_ctl_dates``/``missing_atl_dates``, excluded from that
    metric's statistics, and never invented.

    Non-positive ``tolerance``, ``absolute_tolerance`` or
    ``discontinuity_threshold`` raise ``ValueError`` (an "always-pass"
    check would be worse than no check).

    The verdict (``.passed``) is like-for-like: CTL on the
    ``aerobic_only`` view, ATL on the ``all_loads`` view. Days where their
    own series is detected to have been revised with no activity explaining
    it (see :func:`detect_their_side_revisions`) are excluded from the
    affected series' statistics and verdict and reported explicitly.
    """
    if tolerance <= 0.0:
        raise ValueError(f"tolerance must be positive, got {tolerance!r}")
    if absolute_tolerance <= 0.0:
        raise ValueError(
            f"absolute_tolerance must be positive, got {absolute_tolerance!r}"
        )
    if discontinuity_threshold <= 0.0:
        raise ValueError(
            f"discontinuity_threshold must be positive, got {discontinuity_threshold!r}"
        )
    all_days = tuple(pmc_days_all)
    aerobic_days = tuple(pmc_days_aerobic)
    if len(all_days) != len(aerobic_days):
        raise ValueError(
            "the two PMC series must cover the same window: "
            f"{len(all_days)} all-loads days vs {len(aerobic_days)} aerobic days"
        )
    days: list[PmcCrossCheckDay] = []
    daily_loads: dict[date_cls, float] = {}
    their_ctl_map: dict[date_cls, float | None] = {}
    their_atl_map: dict[date_cls, float | None] = {}
    missing_ctl: list[date_cls] = []
    missing_atl: list[date_cls] = []
    for pmc_all, pmc_aerobic in zip(all_days, aerobic_days, strict=True):
        if pmc_all.date != pmc_aerobic.date:
            raise ValueError(
                f"PMC series dates diverge: {pmc_all.date} vs {pmc_aerobic.date}"
            )
        their_ctl, their_atl = their_values.get(pmc_all.date, (None, None))
        if their_ctl is None:
            missing_ctl.append(pmc_all.date)
        if their_atl is None:
            missing_atl.append(pmc_all.date)
        daily_loads[pmc_all.date] = pmc_all.tss
        their_ctl_map[pmc_all.date] = their_ctl
        their_atl_map[pmc_all.date] = their_atl
        days.append(
            PmcCrossCheckDay(
                date=pmc_all.date,
                tss_all=pmc_all.tss,
                our_ctl_all=pmc_all.ctl,
                our_ctl_aerobic=pmc_aerobic.ctl,
                our_atl_all=pmc_all.atl,
                our_atl_aerobic=pmc_aerobic.atl,
                their_ctl=their_ctl,
                their_atl=their_atl,
            )
        )
    revisions = detect_their_side_revisions(
        daily_loads,
        their_ctl_map,
        their_atl_map,
        threshold=discontinuity_threshold,
    )
    ctl_excluded = frozenset(r.date for r in revisions if r.series == "CTL")
    atl_excluded = frozenset(r.date for r in revisions if r.series == "ATL")
    our_all_ctl = {day.date: day.our_ctl_all for day in days}
    our_aerobic_ctl = {day.date: day.our_ctl_aerobic for day in days}
    our_all_atl = {day.date: day.our_atl_all for day in days}
    our_aerobic_atl = {day.date: day.our_atl_aerobic for day in days}
    return PmcCrossCheckResult(
        tolerance=tolerance,
        absolute_tolerance=absolute_tolerance,
        discontinuity_threshold=discontinuity_threshold,
        days=tuple(days),
        ctl_all_loads=_metric_comparison(
            "CTL",
            "all_loads",
            our_all_ctl,
            days,
            ctl_excluded,
            tolerance=tolerance,
            absolute_tolerance=absolute_tolerance,
        ),
        ctl_aerobic_only=_metric_comparison(
            "CTL",
            "aerobic_only",
            our_aerobic_ctl,
            days,
            ctl_excluded,
            tolerance=tolerance,
            absolute_tolerance=absolute_tolerance,
        ),
        atl_all_loads=_metric_comparison(
            "ATL",
            "all_loads",
            our_all_atl,
            days,
            atl_excluded,
            tolerance=tolerance,
            absolute_tolerance=absolute_tolerance,
        ),
        atl_aerobic_only=_metric_comparison(
            "ATL",
            "aerobic_only",
            our_aerobic_atl,
            days,
            atl_excluded,
            tolerance=tolerance,
            absolute_tolerance=absolute_tolerance,
        ),
        their_ctl_revisions=tuple(r for r in revisions if r.series == "CTL"),
        their_atl_revisions=tuple(r for r in revisions if r.series == "ATL"),
        missing_ctl_dates=tuple(missing_ctl),
        missing_atl_dates=tuple(missing_atl),
    )


def build_daily_load_series(
    activities: Iterable[ActivityRow],
    window_start: date_cls,
    window_end: date_cls,
) -> tuple[dict[date_cls, float], dict[date_cls, float], int]:
    """Our daily load series in TWO views (all loads / aerobic-only).

    Sums ``activity.intervals_icu_load`` per day (§5.1 values, so BOTH
    sides of the comparison are fed the same load inputs), attributed with
    the SAME :func:`app.services.daily_load._activity_date` rule as the
    persistence service (local start date when it parses, UTC otherwise).
    Every calendar day from ``window_start`` to ``window_end`` is present
    (rest days explicit ``0.0``), as :func:`compute_pmc` requires.

    The aerobic-only view excludes exactly the sports the engine classifies
    as strength (:func:`app.engine.load.is_strength_sport`, imported so the
    classification cannot drift) — Intervals' CTL definition. The all-loads
    view includes everything — our engine's deliberate definition.

    Returns ``(all_series, aerobic_series, activities_without_load)``: an
    activity whose ``intervals_icu_load`` is NULL inside the window cannot
    feed a same-inputs comparison and is counted — reported by the caller,
    never silently dropped. Activities outside the window are ignored.
    """
    all_series = {
        window_start + timedelta(days=offset): 0.0
        for offset in range((window_end - window_start).days + 1)
    }
    aerobic_series = dict(all_series)
    without_load = 0
    for row in activities:
        day = _activity_date(row)
        if not window_start <= day <= window_end:
            continue
        if row.intervals_icu_load is None:
            without_load += 1
            continue
        all_series[day] = all_series.get(day, 0.0) + row.intervals_icu_load
        if not is_strength_sport(row.type or ""):
            aerobic_series[day] = aerobic_series.get(day, 0.0) + row.intervals_icu_load
    return all_series, aerobic_series, without_load


@dataclass(frozen=True, slots=True)
class CrossCheckRun:
    """Everything one cross-check run observed (the CLI's report input)."""

    window_start: date_cls
    window_end: date_cls
    tolerance: float
    absolute_tolerance: float
    discontinuity_threshold: float
    athlete_id: int
    activities_considered: int
    strength_activities: int
    activities_without_load: int
    result: PmcCrossCheckResult


async def run_cross_check(
    session: AsyncSession,
    *,
    window_start: date_cls,
    window_end: date_cls,
    tolerance: float = DEFAULT_TOLERANCE,
    absolute_tolerance: float = DEFAULT_ABSOLUTE_TOLERANCE,
    discontinuity_threshold: float = DEFAULT_DISCONTINUITY_THRESHOLD,
    athlete_id: int = 1,
) -> CrossCheckRun:
    """One async DB pass: build, compute, compare (no writes, ever)."""
    if tolerance <= 0.0:
        raise ValueError(f"tolerance must be positive, got {tolerance!r}")
    if absolute_tolerance <= 0.0:
        raise ValueError(
            f"absolute_tolerance must be positive, got {absolute_tolerance!r}"
        )
    if window_end < window_start:
        raise ValueError(
            f"window_end {window_end} is before window_start {window_start}"
        )
    margin = timedelta(days=1)  # same bracketing margin as the service
    activities = (
        (
            await session.execute(
                select(ActivityRow).where(
                    ActivityRow.start_time
                    >= datetime.combine(
                        window_start - margin, dt_time.min, tzinfo=UTC
                    ),
                    ActivityRow.start_time
                    < datetime.combine(
                        window_end + margin, dt_time.min, tzinfo=UTC
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    in_window = [
        row
        for row in activities
        if window_start <= _activity_date(row) <= window_end
    ]
    all_series, aerobic_series, without_load = build_daily_load_series(
        in_window, window_start, window_end
    )
    pmc_all = compute_pmc(all_series)
    pmc_aerobic = compute_pmc(aerobic_series)
    strength_activities = sum(
        1 for row in in_window if is_strength_sport(row.type or "")
    )
    wellness_rows = (
        (
            await session.execute(
                select(WellnessRow).where(
                    WellnessRow.athlete_id == athlete_id,
                    WellnessRow.date >= window_start,
                    WellnessRow.date <= window_end,
                )
            )
        )
        .scalars()
        .all()
    )
    their_values = {
        row.date: (row.intervals_icu_ctl, row.intervals_icu_atl)
        for row in wellness_rows
    }
    result = compare_pmc_series(
        pmc_all.days,
        pmc_aerobic.days,
        their_values,
        tolerance=tolerance,
        absolute_tolerance=absolute_tolerance,
        discontinuity_threshold=discontinuity_threshold,
    )
    return CrossCheckRun(
        window_start=window_start,
        window_end=window_end,
        tolerance=tolerance,
        absolute_tolerance=absolute_tolerance,
        discontinuity_threshold=discontinuity_threshold,
        athlete_id=athlete_id,
        activities_considered=len(in_window),
        strength_activities=strength_activities,
        activities_without_load=without_load,
        result=result,
    )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.4f}%"


def _bound_marker(day: MetricDeviationDay) -> str:
    """Which tolerance bound (if any) this day satisfied."""
    if day.their_value is None:
        return "missing"
    if day.their_side_excluded:
        return "their-side revision (excluded)"
    if day.within_rel and day.within_abs:
        return "rel+abs"
    if day.within_rel:
        return "rel"
    if day.within_abs:
        return "abs"
    return "FAIL"


def _print_comparison(comp: MetricComparison, *, headline: bool) -> None:
    """Print one comparison's statistics line (both bounds, per day marks)."""
    label = f"{comp.metric} [{comp.view}]"
    if headline:
        label += " — LIKE-FOR-LIKE (drives the verdict)"
    comparable = comp.comparable_days
    print(
        f"{label}: comparable {comparable} day(s) | "
        f"rel |dev| median {_pct(comp.median_rel_dev)} max {_pct(comp.max_rel_dev)} | "
        f"abs |dev| median {_fmt(comp.median_abs_dev)} max {_fmt(comp.max_abs_dev)} pts | "
        f"rel-bounded {comp.days_within_rel}, abs-bounded {comp.days_within_abs}, "
        f"failed both {comp.days_failed_both}"
    )


def _report(run: CrossCheckRun) -> None:
    """Print the human-readable cross-check report (stdout; §12.3 evidence).

    The cross-check-only / never-authoritative statement (§5.1) is part of
    every report, not a one-off comment. Both load views are printed in
    full — the like-for-like verdict never hides the other view.
    """
    result = run.result
    window_days = (run.window_end - run.window_start).days + 1
    print(
        "PMC cross-check vs Intervals.icu (§12.3) — CROSS-CHECK ONLY: "
        "Intervals values are NEVER authoritative (§5.1)"
    )
    print(
        f"window {run.window_start}..{run.window_end} ({window_days} days) | "
        f"tolerance (owner-agreed §12.3): relative ±{result.tolerance * 100:.1f}% OR "
        f"absolute ±{result.absolute_tolerance} CTL/ATL points (inclusive bounds) | "
        f"their-side revision threshold {result.discontinuity_threshold} pts | "
        f"athlete {run.athlete_id}"
    )
    print(
        "definition note: Intervals' CTL EXCLUDES strength load (their ATL "
        "INCLUDES it); our engine includes strength in both (deliberate, "
        "triathlete who lifts). Verdict is like-for-like: CTL vs the "
        "aerobic-only view, ATL vs the all-loads view."
    )
    print(
        f"activities in window: {run.activities_considered} "
        f"({run.strength_activities} strength — excluded from the aerobic-only "
        f"view; {run.activities_without_load} without stored intervals_icu_load "
        "— excluded from BOTH sides' inputs)"
    )
    print(
        f"days compared: {len(result.days)} | "
        f"missing Intervals CTL on {len(result.missing_ctl_dates)} day(s) | "
        f"missing Intervals ATL on {len(result.missing_atl_dates)} day(s)"
    )
    revisions = result.all_revisions
    if revisions:
        print(
            "Intervals-side revision(s) — no activity explains it "
            f"(threshold {result.discontinuity_threshold} pts, excluded from "
            "the affected series' statistics and verdict):"
        )
        for rev in revisions:
            print(
                f"  {rev.date} {rev.series}: previous {rev.previous_value:.4f} "
                f"({rev.previous_date}), expected by pure decay "
                f"{rev.expected_value:.4f}, actual {rev.actual_value:.4f} "
                f"(deviation {rev.deviation:.4f} pts) — "
                "Intervals-side revision (no activity explains it)"
            )
    else:
        print("Intervals-side revisions: none detected")
    header = (
        "per-date deltas (date | our | their | rel dev | abs dev pts | bound)"
    )
    for view, _ in (("all_loads", False), ("aerobic_only", True)):
        print(f"--- view: {view} ({header}) ---")
        for day, ctl, atl in _view_rows(result, view):
            print(
                f"{day.date} CTL | {_fmt(ctl.our_value)} | {_fmt(ctl.their_value)} | "
                f"{_pct(ctl.rel_dev)} | {_fmt(ctl.abs_dev)} | {_bound_marker(ctl)} || "
                f"ATL | {_fmt(atl.our_value)} | {_fmt(atl.their_value)} | "
                f"{_pct(atl.rel_dev)} | {_fmt(atl.abs_dev)} | {_bound_marker(atl)}"
            )
    print("deviation statistics (over comparable, non-excluded days):")
    _print_comparison(result.ctl_all_loads, headline=False)
    _print_comparison(result.ctl_aerobic_only, headline=True)
    _print_comparison(result.atl_all_loads, headline=True)
    _print_comparison(result.atl_aerobic_only, headline=False)
    if result.passed:
        print(
            f"VERDICT: PASS — like-for-like check (CTL vs aerobic-only view, "
            f"ATL vs all-loads view) within ±{result.tolerance * 100:.1f}% "
            f"relative OR ±{result.absolute_tolerance} points on all "
            f"comparable day(s) "
            f"({result.ctl_aerobic_only.comparable_days} CTL / "
            f"{result.atl_all_loads.comparable_days} ATL); "
            f"{len(revisions)} Intervals-side revision(s) excluded and listed "
            "above; cross-check only — Intervals values are never authoritative"
        )
    else:
        failures: list[str] = []
        for comp in (result.ctl_aerobic_only, result.atl_all_loads):
            if comp.failed_dates or comp.comparable_days == 0:
                failures.append(
                    f"{comp.metric}: {len(comp.failed_dates)} day(s) failed "
                    f"both bounds, {comp.comparable_days} comparable"
                )
        print(
            f"VERDICT: FAIL — like-for-like check (CTL vs aerobic-only view, "
            f"ATL vs all-loads view): {'; '.join(failures)}; missing values: "
            f"{len(result.missing_ctl_dates)} CTL / "
            f"{len(result.missing_atl_dates)} ATL; cross-check only — "
            "Intervals values are never authoritative"
        )


def _view_rows(
    result: PmcCrossCheckResult, view: str
) -> list[tuple[PmcCrossCheckDay, MetricDeviationDay, MetricDeviationDay]]:
    """(day, ctl comparison, atl comparison) rows for one view's table."""
    if view == "all_loads":
        ctl_comp, atl_comp = result.ctl_all_loads, result.atl_all_loads
    else:
        ctl_comp, atl_comp = result.ctl_aerobic_only, result.atl_aerobic_only
    return list(
        zip(result.days, ctl_comp.days, atl_comp.days, strict=True)
    )


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid float value: {value!r}"
        ) from None
    if parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("--days must be a positive integer")
    return parsed


def _iso_date(value: str) -> date_cls:
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date (expected YYYY-MM-DD): {value!r}"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    """Build the cross-check CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.cross_check_pmc",
        description=(
            "Cross-check OUR engine's PMC (CTL/ATL) against the stored "
            "NON-authoritative Intervals.icu values (§12.3). Read-only. "
            "Like-for-like verdict: CTL vs the aerobic-only load view "
            "(Intervals' CTL excludes strength), ATL vs the all-loads view "
            "(their ATL includes strength)."
        ),
    )
    parser.add_argument(
        "--days",
        type=_positive_int,
        required=True,
        metavar="N",
        help=(
            "window length in days, ending at --end (default: today); use "
            f">= {MIN_CROSS_CHECK_WINDOW_DAYS} so the zero-seed influence "
            "(e^(-n/42) with tau_ctl = 42 d) decays below ~1.4%%"
        ),
    )
    parser.add_argument(
        "--tolerance",
        type=_positive_float,
        default=DEFAULT_TOLERANCE,
        metavar="T",
        help=(
            f"relative tolerance, default {DEFAULT_TOLERANCE} (= ±10%%; "
            "owner-agreed §12.3 tolerance; hybrid rule: a day passes when "
            "the relative OR the absolute bound holds)"
        ),
    )
    parser.add_argument(
        "--absolute-tolerance",
        type=_positive_float,
        default=DEFAULT_ABSOLUTE_TOLERANCE,
        metavar="PTS",
        help=(
            "absolute tolerance in CTL/ATL points, default "
            f"{DEFAULT_ABSOLUTE_TOLERANCE}: a day passes when the relative "
            "OR this absolute bound holds (decaying-series guard)"
        ),
    )
    parser.add_argument(
        "--end",
        type=_iso_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="last day of the window, inclusive (default: today, Europe/Madrid)",
    )
    parser.add_argument(
        "--athlete-id",
        type=int,
        default=1,
        help="athlete_id of the wellness rows (default: 1)",
    )
    return parser


async def _run(
    days: int,
    tolerance: float,
    absolute_tolerance: float,
    end_date: date_cls | None,
    athlete_id: int,
    config: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> CrossCheckRun:
    window_end = end_date or today_in_zone(config.timezone)
    window_start = window_end - timedelta(days=days - 1)
    if session_factory is not None:
        async with session_factory() as session:
            return await run_cross_check(
                session,
                window_start=window_start,
                window_end=window_end,
                tolerance=tolerance,
                absolute_tolerance=absolute_tolerance,
                athlete_id=athlete_id,
            )
    engine = create_db_engine(config.database_url)
    try:
        async with make_session_factory(engine)() as session:
            return await run_cross_check(
                session,
                window_start=window_start,
                window_end=window_end,
                tolerance=tolerance,
                absolute_tolerance=absolute_tolerance,
                athlete_id=athlete_id,
            )
    finally:
        await engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
) -> int:
    """CLI entry point: 0 on PASS, 1 on FAIL, 2 on argument errors."""
    args = build_parser().parse_args(argv)
    config = settings or get_settings()
    run = asyncio.run(
        _run(
            args.days,
            args.tolerance,
            args.absolute_tolerance,
            args.end,
            args.athlete_id,
            config,
            session_factory,
        )
    )
    _report(run)
    return 0 if run.result.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
