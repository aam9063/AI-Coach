"""Performance Manager (CTL/ATL/TSB) and EWMA ACWR over daily load series.

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no imports of ``app.db`` / ``app.ingest`` /
``app.core`` (PROJECT_BRIEF sections 6 and 14). Daily loads come in as an
explicit ``Mapping[date, float]`` (one entry per calendar day, rest days as
``0.0``); side effects, persistence and settings reads belong in callers.

Formulas and references
-----------------------
Performance Manager (Coggan; Allen & Coggan, "Training and Racing with a
Power Meter", Performance Manager Chart):

    CTL_t = CTL_{t-1} + (TSS_t - CTL_{t-1}) * (1 - e^(-1/tau_ctl))
    ATL_t = ATL_{t-1} + (TSS_t - ATL_{t-1}) * (1 - e^(-1/tau_atl))
    TSB_t = CTL_{t-1} - ATL_{t-1}

with the default time constants ``tau_ctl = 42`` days (chronic training
load) and ``tau_atl = 7`` days (acute training load); both are explicit,
overridable parameters. ``TSB_t`` deliberately uses the *previous* day's
CTL and ATL (freshness as of the morning of day ``t``), which also makes
``TSB`` on the first day exactly ``seed_ctl - seed_atl``.

Seeding: the recursions need starting values. Defaults are ``seed_ctl =
0.0`` and ``seed_atl = 0.0`` (the documented default: the athlete starts
from zero load — the same default Intervals.icu-style PMC charts use);
callers with prior history pass the last CTL/ATL explicitly. Seeds are
never guessed from the data.

Low-confidence flag: a PMC series with fewer than ``min_history_days``
(default 90, configurable) days of history is flagged
``confident=False``: CTL in particular converges slowly (time constant 42
days), so early values are dominated by the seed, not the athlete.

ACWR, EWMA version (acute:chronic workload ratio, Williams, Trewartha,
Cross, Kemp & Stokes 2017):

    EWMA_t = EWMA_{t-1} + (TSS_t - EWMA_{t-1}) * (1 - e^(-1/tau))

with ``tau_acute = 7`` days and ``tau_chronic = 28`` days, and

    ACWR = acute_EWMA / chronic_EWMA

Zero-chronic semantics: with no chronic baseline (all-zero or empty load
history, i.e. ``chronic_EWMA == 0``) the ratio is ``None`` — a defined
"no chronic baseline yet" state, never a silent ``0.0`` and never an
exception. Context-only stance (PROJECT_BRIEF section 7.2): the ACWR
result is plain metadata (:class:`AcwrResult` carries exactly
``acute_ewma``, ``chronic_ewma`` and ``ratio`` — no warning, risk or flag
field) and must never trigger warnings on its own; warning logic belongs
to Feature 5 (section 7.4).

Daily-series validation (shared by all functions): an empty series, a
negative daily TSS, a missing calendar day between the first and last
date (a "gap" — rest days must be present as explicit ``0.0`` so
misalignment can never be silently smoothed over), and non-positive time
constants all raise ``ValueError`` instead of degrading silently.

All functions are pure and fully typed.

Reference: Allen & Coggan, "Training and Racing with a Power Meter"
(Performance Manager); Williams et al. 2017 (EWMA ACWR).
"""

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

__all__ = [
    "ACWR_TAU_ACUTE_DAYS",
    "ACWR_TAU_CHRONIC_DAYS",
    "DEFAULT_MIN_HISTORY_DAYS",
    "DEFAULT_TAU_ATL_DAYS",
    "DEFAULT_TAU_CTL_DAYS",
    "AcwrResult",
    "PmcDay",
    "PmcPerSport",
    "PmcSeries",
    "acwr_ewma",
    "compute_pmc",
    "compute_pmc_per_sport",
]

DEFAULT_TAU_CTL_DAYS: Final[float] = 42.0
"""Default chronic-load time constant: tau_ctl = 42 days (Allen & Coggan).

Module-level constant so tests and callers can reference it; LOAD-11 owns
moving engine constants into settings with source comments.
"""

DEFAULT_TAU_ATL_DAYS: Final[float] = 7.0
"""Default acute-load time constant: tau_atl = 7 days (Allen & Coggan)."""

DEFAULT_MIN_HISTORY_DAYS: Final[int] = 90
"""Default minimum history before a PMC series is flagged confident.

CTL has a 42-day time constant, so 90 days (~2 time constants) is the
documented owner-choice threshold below which seed values dominate the
recursion (PROJECT_BRIEF section 7.2).
"""

ACWR_TAU_ACUTE_DAYS: Final[float] = 7.0
"""ACWR acute EWMA time constant: 7 days (Williams et al. 2017)."""

ACWR_TAU_CHRONIC_DAYS: Final[float] = 28.0
"""ACWR chronic EWMA time constant: 28 days (Williams et al. 2017)."""


@dataclass(frozen=True, slots=True)
class PmcDay:
    """One day of Performance Manager output.

    ``tss`` is the day's total load (rest days ``0.0``); ``ctl``/``atl``
    are the day's exponentially smoothed chronic/acute loads; ``tsb`` is
    the freshness ``CTL_{t-1} - ATL_{t-1}`` (yesterday's form, positive =
    fresh, negative = fatigued).
    """

    date: dt.date
    tss: float
    ctl: float
    atl: float
    tsb: float


@dataclass(frozen=True, slots=True)
class PmcSeries:
    """Performance Manager series over a contiguous daily load history.

    ``history_days`` is the number of daily entries; ``confident`` is
    ``history_days >= min_history_days`` (default 90): below that the
    series is seed-dominated and must be presented as low-confidence
    (PROJECT_BRIEF section 7.2).
    """

    days: tuple[PmcDay, ...]
    history_days: int
    confident: bool


@dataclass(frozen=True, slots=True)
class PmcPerSport:
    """Combined and per-sport Performance Manager series (section 7.2).

    ``per_sport`` maps each sport key to its own :class:`PmcSeries`;
    ``combined`` is computed over the per-day sum of all sports.
    """

    per_sport: dict[str, PmcSeries]
    combined: PmcSeries


@dataclass(frozen=True, slots=True)
class AcwrResult:
    """EWMA ACWR as context-only metadata (PROJECT_BRIEF section 7.2).

    Exactly three fields — ``acute_ewma``, ``chronic_ewma`` (TSS units)
    and ``ratio`` — deliberately no warning/risk/flag field: ACWR is
    displayed as context and must never trigger warnings on its own
    (warning logic is Feature 5, section 7.4). ``ratio is None`` means no
    chronic baseline yet (``chronic_ewma == 0``), never a silent 0.
    """

    acute_ewma: float
    chronic_ewma: float
    ratio: float | None


def _validated_daily_loads(loads: Mapping[dt.date, float]) -> list[tuple[dt.date, float]]:
    """Validate a daily load mapping and return it as sorted (date, tss) items.

    Raises ``ValueError`` for an empty series, a negative daily TSS, or a
    missing calendar day between the first and last date (rest days must
    be present as explicit ``0.0``; a hole cannot be distinguished from
    missing data and is never silently smoothed over).
    """
    if not loads:
        raise ValueError(
            "daily load series is empty: provide at least one calendar day "
            "(rest days as explicit 0.0)"
        )
    items = sorted(loads.items())
    for date, tss in items:
        if tss < 0.0:
            raise ValueError(
                f"negative daily TSS on {date}: {tss!r} (rest days are 0.0, "
                "negative load is invalid)"
            )
    for (prev_date, _), (curr_date, _) in pairwise(items):
        if (curr_date - prev_date).days != 1:
            raise ValueError(
                f"daily load series has a calendar gap between {prev_date} and "
                f"{curr_date}: every calendar day must be present, with rest "
                "days as explicit 0.0"
            )
    return items


def _ewma(previous: float, tss: float, tau_days: float) -> float:
    """One EWMA step: ``previous + (tss - previous) * (1 - e^(-1/tau))``."""
    return previous + (tss - previous) * (1.0 - math.exp(-1.0 / tau_days))


def _validate_time_constants(*taus: tuple[str, float]) -> None:
    """Reject non-positive time constants (misconfiguration, never a default)."""
    for name, tau in taus:
        if tau <= 0.0:
            raise ValueError(f"{name} must be positive, got {tau!r} days")


def _validate_seeds(**seeds: float) -> None:
    """Reject negative seeds: a load baseline can never be negative."""
    for name, seed in seeds.items():
        if seed < 0.0:
            raise ValueError(f"{name} must be non-negative, got {seed!r}")


def compute_pmc(
    loads: Mapping[dt.date, float],
    *,
    seed_ctl: float = 0.0,
    seed_atl: float = 0.0,
    tau_ctl_days: float = DEFAULT_TAU_CTL_DAYS,
    tau_atl_days: float = DEFAULT_TAU_ATL_DAYS,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
) -> PmcSeries:
    """Performance Manager (CTL/ATL/TSB) over a contiguous daily load series.

    Formulas (Allen & Coggan, Performance Manager Chart):

        CTL_t = CTL_{t-1} + (TSS_t - CTL_{t-1}) * (1 - e^(-1/tau_ctl))
        ATL_t = ATL_{t-1} + (TSS_t - ATL_{t-1}) * (1 - e^(-1/tau_atl))
        TSB_t = CTL_{t-1} - ATL_{t-1}

    Defaults: ``tau_ctl_days=42``, ``tau_atl_days=7`` (both overridable),
    seeds ``seed_ctl=0.0`` / ``seed_atl=0.0`` (documented default — the
    athlete starts from zero load; pass the last CTL/ATL explicitly when
    continuing prior history). ``TSB_t`` uses the *previous* day's CTL and
    ATL, so day 1's TSB is exactly ``seed_ctl - seed_atl``.

    Validation (``ValueError``): empty series, negative daily TSS, a
    calendar gap (rest days must be present as explicit ``0.0``),
    non-positive time constants, negative seeds, non-positive
    ``min_history_days``.

    ``PmcSeries.confident`` is False while ``history_days`` is below
    ``min_history_days`` (default 90): early CTL values are seed-dominated.

    Reference: Allen & Coggan, "Training and Racing with a Power Meter".
    """
    _validate_time_constants(("tau_ctl_days", tau_ctl_days), ("tau_atl_days", tau_atl_days))
    _validate_seeds(seed_ctl=seed_ctl, seed_atl=seed_atl)
    if min_history_days <= 0:
        raise ValueError(
            f"min_history_days must be positive, got {min_history_days!r}"
        )
    items = _validated_daily_loads(loads)
    prev_ctl = seed_ctl
    prev_atl = seed_atl
    days: list[PmcDay] = []
    for date, tss in items:
        ctl = _ewma(prev_ctl, tss, tau_ctl_days)
        atl = _ewma(prev_atl, tss, tau_atl_days)
        days.append(PmcDay(date=date, tss=tss, ctl=ctl, atl=atl, tsb=prev_ctl - prev_atl))
        prev_ctl, prev_atl = ctl, atl
    history_days = len(days)
    return PmcSeries(
        days=tuple(days),
        history_days=history_days,
        confident=history_days >= min_history_days,
    )


def compute_pmc_per_sport(
    sport_loads: Mapping[str, Mapping[dt.date, float]],
    *,
    seed_ctl: float = 0.0,
    seed_atl: float = 0.0,
    tau_ctl_days: float = DEFAULT_TAU_CTL_DAYS,
    tau_atl_days: float = DEFAULT_TAU_ATL_DAYS,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
) -> PmcPerSport:
    """Combined AND per-sport Performance Manager series (section 7.2).

    Each sport's series is computed with :func:`compute_pmc` (same seeds,
    time constants and confidence threshold; each must itself be
    contiguous — a gap inside one sport raises ``ValueError``). The
    combined series spans every calendar day from the earliest to the
    latest date across all sports, summing the sports per day and using
    an explicit ``0.0`` for days no sport trained (combined rest days),
    so the combined recursion always steps exactly one day at a time.

    An empty sport mapping raises ``ValueError``.

    Reference: Allen & Coggan, "Training and Racing with a Power Meter".
    """
    if not sport_loads:
        raise ValueError(
            "sport load mapping is empty: provide at least one sport series"
        )
    per_sport = {
        sport: compute_pmc(
            series,
            seed_ctl=seed_ctl,
            seed_atl=seed_atl,
            tau_ctl_days=tau_ctl_days,
            tau_atl_days=tau_atl_days,
            min_history_days=min_history_days,
        )
        for sport, series in sorted(sport_loads.items())
    }
    all_dates = sorted({date for series in sport_loads.values() for date in series})
    first, last = all_dates[0], all_dates[-1]
    combined_loads = {
        first + dt.timedelta(days=offset): math.fsum(
            series.get(first + dt.timedelta(days=offset), 0.0)
            for series in sport_loads.values()
        )
        for offset in range((last - first).days + 1)
    }
    combined = compute_pmc(
        combined_loads,
        seed_ctl=seed_ctl,
        seed_atl=seed_atl,
        tau_ctl_days=tau_ctl_days,
        tau_atl_days=tau_atl_days,
        min_history_days=min_history_days,
    )
    return PmcPerSport(per_sport=per_sport, combined=combined)


def acwr_ewma(
    loads: Mapping[dt.date, float],
    *,
    tau_acute_days: float = ACWR_TAU_ACUTE_DAYS,
    tau_chronic_days: float = ACWR_TAU_CHRONIC_DAYS,
    seed_acute: float = 0.0,
    seed_chronic: float = 0.0,
) -> AcwrResult:
    """Acute:chronic workload ratio, EWMA version (Williams et al. 2017).

    Formula:

        EWMA_t = EWMA_{t-1} + (TSS_t - EWMA_{t-1}) * (1 - e^(-1/tau))
        ACWR   = acute_EWMA / chronic_EWMA

    Defaults ``tau_acute_days=7``, ``tau_chronic_days=28`` (overridable);
    seeds default to 0.0 and can be passed explicitly when continuing
    prior history.

    Zero-chronic semantics: when ``chronic_ewma == 0`` (all-zero load
    history, i.e. no chronic baseline yet) the returned ``ratio`` is
    ``None`` — a defined absence, never a silent ``0.0`` and never an
    exception.

    Context-only stance (PROJECT_BRIEF section 7.2): the result is plain
    metadata (:class:`AcwrResult` carries no warning/risk/flag field) and
    must never trigger warnings on its own; warning logic is Feature 5.

    Validation (``ValueError``): empty series, negative daily TSS,
    calendar gap, non-positive time constants, negative seeds.

    Reference: Williams, Trewartha, Cross, Kemp & Stokes 2017 (EWMA ACWR).
    """
    _validate_time_constants(
        ("tau_acute_days", tau_acute_days), ("tau_chronic_days", tau_chronic_days)
    )
    _validate_seeds(seed_acute=seed_acute, seed_chronic=seed_chronic)
    items = _validated_daily_loads(loads)
    acute = seed_acute
    chronic = seed_chronic
    for _, tss in items:
        acute = _ewma(acute, tss, tau_acute_days)
        chronic = _ewma(chronic, tss, tau_chronic_days)
    ratio: float | None = None if chronic == 0.0 else acute / chronic
    return AcwrResult(acute_ewma=acute, chronic_ewma=chronic, ratio=ratio)
