"""Canonical input fixtures for chart snapshot tests.

Every value is computed from a fixed formula so the fixtures are
reproducible forever: no randomness, no wall clock, no stored engine
output that a re-run could drift. Each fixture is the *minimum* data the
renderer needs (§14 minimum context rule) — plain numbers, not ORM rows.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.charts.performance_manager import PerformanceManagerSample
from app.charts.weekly_tss import WeeklyTssSample

FIRST_DAY = date(2026, 9, 1)
DAYS = 28
FIRST_WEEK = date(2026, 8, 31)  # Monday
WEEKS = 6


def _performance_manager_samples() -> tuple[PerformanceManagerSample, ...]:
    """28 days of CTL/ATL/TSB with a visible weekly training rhythm.

    CTL climbs steadily, ATL pulses with a 7-day period (weekend blocks),
    and TSB is the CTL-ATL difference, dipping negative on the hard days.
    """
    samples: list[PerformanceManagerSample] = []
    for i in range(DAYS):
        ctl = 40.0 + 0.25 * i
        atl = 50.0 + 2.5 * (i % 7)
        tsb = ctl - atl
        samples.append(
            PerformanceManagerSample(day=FIRST_DAY + timedelta(days=i), ctl=ctl, atl=atl, tsb=tsb)
        )
    return tuple(samples)


def _weekly_tss_samples() -> tuple[WeeklyTssSample, ...]:
    """6 weeks of per-sport TSS totals (ride/run/swim) with a build rhythm.

    Total weekly load grows ~15% per week, and the sport mix shifts so the
    stacked bands are visibly distinct: ride dominant, run steady, swim flat.
    """
    sports = ("ride", "run", "swim")
    samples: list[WeeklyTssSample] = []
    for w in range(WEEKS):
        total = 300.0 * (1.15**w)
        ride = total * 0.55
        run = total * (0.25 + 0.01 * w)
        swim = total - ride - run
        samples.append(
            WeeklyTssSample(
                week_start=FIRST_WEEK + timedelta(weeks=w),
                sport_tss=tuple(zip(sports, (ride, run, swim), strict=True)),
            )
        )
    return tuple(samples)


PERFORMANCE_MANAGER_SAMPLES = _performance_manager_samples()
WEEKLY_TSS_SAMPLES = _weekly_tss_samples()
