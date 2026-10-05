"""Deterministic tests for the PMC cross-check logic (LOAD-10, §12.3).

The cross-check compares OUR engine's CTL/ATL (``app.engine.pmc``) against
the stored NON-authoritative ``wellness.intervals_icu_ctl``/``..._atl``
columns (§5.1). Like-with-like contract (established on the owner's real
data, encoded here as synthetic tests):

- Intervals' **CTL excludes strength sessions' load** while our engine
  (deliberately, for a triathlete who lifts) includes it. So the
  like-for-like CTL comparison is OUR CTL computed over the
  **aerobic-only** load view vs their CTL.
- Intervals' **ATL includes strength load**, so the like-for-like ATL
  comparison is OUR ATL over the **all-loads** view vs their ATL.
- Both views are always computed and reported; the verdict uses the
  like-for-like pair. A pure relative tolerance is unusable for a
  decaying series near zero (real data showed 223% on values of order
  1e-4), so a day passes when the relative deviation is within
  ``--tolerance`` OR the absolute deviation is within
  ``--absolute-tolerance`` (CTL/ATL points).
- Days where THEIR series jumps beyond the documented threshold with no
  activity load that day are reported as Intervals-side revisions and
  excluded from that series' deviation statistics and verdict.

Every test here uses a synthetic series — the owner's live data is never
loaded (the real-data run is a manual evidence step, not a test).
"""

import datetime as dt
import math

import pytest

from app.db.models import ActivityRow
from app.engine.load import STRENGTH_SPORTS, is_strength_sport
from app.engine.pmc import PmcDay, compute_pmc
from app.tools.cross_check_pmc import (
    DEFAULT_ABSOLUTE_TOLERANCE,
    DEFAULT_DISCONTINUITY_THRESHOLD,
    DEFAULT_TOLERANCE,
    PmcCrossCheckResult,
    build_daily_load_series,
    compare_pmc_series,
    detect_their_side_revisions,
    relative_deviation,
)

DAY = dt.date(2026, 6, 1)


def pmc_day(
    offset_days: int,
    ctl: float,
    atl: float,
    *,
    tss: float = 0.0,
) -> PmcDay:
    date = DAY + dt.timedelta(days=offset_days)
    return PmcDay(date=date, tss=tss, ctl=ctl, atl=atl, tsb=ctl - atl)


def activity(
    *,
    day: dt.date,
    load: float | None,
    type: str = "Ride",
    source_id: str = "i1",
) -> ActivityRow:
    return ActivityRow(
        source="intervals",
        source_id=source_id,
        type=type,
        name="x",
        start_time=dt.datetime(day.year, day.month, day.day, 12, 0, tzinfo=dt.UTC),
        start_time_local=f"{day.isoformat()}T12:00:00",
        duration_s=3600,
        intervals_icu_load=load,
    )


class TestRelativeDeviation:
    """|ours - theirs| / |theirs|; undefined only when not comparable."""

    def test_above_and_below_their_value_are_absolute(self) -> None:
        assert relative_deviation(110.0, 100.0) == pytest.approx(0.10)
        assert relative_deviation(90.0, 100.0) == pytest.approx(0.10)

    def test_exact_match_is_zero(self) -> None:
        assert relative_deviation(100.0, 100.0) == 0.0

    def test_both_zero_is_zero_not_undefined(self) -> None:
        assert relative_deviation(0.0, 0.0) == 0.0

    def test_their_zero_with_our_load_is_not_comparable(self) -> None:
        # A relative deviation against a 0 baseline is undefined (division);
        # inventing a value is forbidden, so the day is reported instead.
        assert relative_deviation(5.0, 0.0) is None

    def test_missing_their_value_is_not_comparable(self) -> None:
        assert relative_deviation(5.0, None) is None


class TestComparePmcSeriesViewsAndVerdict:
    """Both load views are compared; the verdict is like-for-like."""

    def two_days(
        self,
        *,
        all_ctl: tuple[float, float],
        aerobic_ctl: tuple[float, float],
        all_atl: tuple[float, float],
        aerobic_atl: tuple[float, float],
        their_ctl: tuple[float, float],
        their_atl: tuple[float, float],
    ) -> tuple[
        tuple[PmcDay, ...],
        tuple[PmcDay, ...],
        dict[dt.date, tuple[float, float]],
    ]:
        """Two consecutive days, both carrying load (tss=10) so the
        their-side revision detector stays inert (dedicated tests below)."""
        all_days = (
            pmc_day(0, all_ctl[0], all_atl[0], tss=10.0),
            pmc_day(1, all_ctl[1], all_atl[1], tss=10.0),
        )
        aerobic_days = (
            pmc_day(0, aerobic_ctl[0], aerobic_atl[0], tss=10.0),
            pmc_day(1, aerobic_ctl[1], aerobic_atl[1], tss=10.0),
        )
        values = {
            DAY: (their_ctl[0], their_atl[0]),
            DAY + dt.timedelta(days=1): (their_ctl[1], their_atl[1]),
        }
        return all_days, aerobic_days, values

    def test_like_for_like_verdict_uses_aerobic_ctl_and_all_loads_atl(self) -> None:
        """Simulated Intervals semantics: their CTL excludes strength, their
        ATL includes it. The like-for-like pair matches exactly, the other
        two views deviate — the verdict must still PASS and both deviating
        views must be visible with failing days."""
        all_days, aerobic_days, values = self.two_days(
            all_ctl=(60.0, 60.0),  # includes strength: deviates from their CTL
            aerobic_ctl=(55.0, 53.7048),  # like-for-like: matches their CTL
            all_atl=(40.0, 34.6751),  # like-for-like: matches their ATL
            aerobic_atl=(20.0, 20.0),  # strength excluded: deviates
            their_ctl=(55.0, 53.7048),
            their_atl=(40.0, 34.6751),
        )
        result = compare_pmc_series(all_days, aerobic_days, values)
        assert result.passed
        # Like-for-like comparisons are clean.
        assert result.ctl_aerobic_only.days_failed_both == 0
        assert result.atl_all_loads.days_failed_both == 0
        # The other two views deviate and are REPORTED, not hidden.
        # CTL all-loads day 0: |60-55|/55 = 9.09% -> rel-bounded; day 1
        # exceeds both bounds.
        assert result.ctl_all_loads.days_failed_both == 1
        assert result.ctl_all_loads.days_within_rel == 1
        assert result.atl_aerobic_only.days_failed_both == 2

    def test_verdict_fails_when_like_for_like_ctl_deviates(self) -> None:
        all_days, aerobic_days, values = self.two_days(
            all_ctl=(60.0, 60.0),
            aerobic_ctl=(65.0, 65.0),  # like-for-like CTL deviates
            all_atl=(40.0, 34.6751),
            aerobic_atl=(20.0, 20.0),
            their_ctl=(55.0, 53.7048),
            their_atl=(40.0, 34.6751),
        )
        result = compare_pmc_series(all_days, aerobic_days, values)
        assert not result.passed
        assert result.ctl_aerobic_only.failed_dates == (
            DAY,
            DAY + dt.timedelta(days=1),
        )

    def test_verdict_fails_when_like_for_like_atl_deviates(self) -> None:
        all_days, aerobic_days, values = self.two_days(
            all_ctl=(60.0, 60.0),
            aerobic_ctl=(55.0, 53.7048),
            all_atl=(45.0, 45.0),  # like-for-like ATL deviates
            aerobic_atl=(20.0, 20.0),
            their_ctl=(55.0, 53.7048),
            their_atl=(40.0, 34.6751),
        )
        result = compare_pmc_series(all_days, aerobic_days, values)
        assert not result.passed
        assert result.atl_all_loads.failed_dates == (
            DAY,
            DAY + dt.timedelta(days=1),
        )

    def test_both_views_record_both_days_values(self) -> None:
        all_days, aerobic_days, values = self.two_days(
            all_ctl=(60.0, 60.0),
            aerobic_ctl=(55.0, 53.7048),
            all_atl=(40.0, 34.6751),
            aerobic_atl=(20.0, 20.0),
            their_ctl=(55.0, 53.7048),
            their_atl=(40.0, 34.6751),
        )
        result = compare_pmc_series(all_days, aerobic_days, values)
        day0 = result.days[0]
        assert day0.our_ctl_all == pytest.approx(60.0)
        assert day0.our_ctl_aerobic == pytest.approx(55.0)
        assert day0.our_atl_all == pytest.approx(40.0)
        assert day0.our_atl_aerobic == pytest.approx(20.0)
        assert day0.their_ctl == pytest.approx(55.0)
        assert day0.their_atl == pytest.approx(40.0)

    def test_end_to_end_with_compute_pmc_and_strength_divergence(self) -> None:
        """Full recursion over a synthetic window: our ALL-loads CTL vs
        their CTL (simulated as our aerobic CTL) deviates on and after the
        strength day, while the like-for-like pair matches; verdict PASS."""
        start = DAY - dt.timedelta(days=119)
        all_loads: dict[dt.date, float] = {}
        for offset in range(120):
            day = start + dt.timedelta(days=offset)
            all_loads[day] = 50.0 if offset % 3 == 0 else 0.0
        all_loads[start + dt.timedelta(days=100)] += 10.0  # one strength day
        aerobic_loads = dict(all_loads)
        aerobic_loads[start + dt.timedelta(days=100)] -= 10.0
        ours_all = compute_pmc(all_loads).days
        ours_aerobic = compute_pmc(aerobic_loads).days
        # Simulated Intervals values over the last 30 days.
        values = {
            day.date: (aer.ctl, a.atl)
            for day, aer, a in zip(ours_all[-30:], ours_aerobic[-30:], ours_all[-30:], strict=True)
        }
        result = compare_pmc_series(ours_all[-30:], ours_aerobic[-30:], values)
        assert result.passed
        assert result.ctl_aerobic_only.days_failed_both == 0
        assert result.ctl_aerobic_only.max_abs_dev == pytest.approx(0.0)
        assert result.atl_all_loads.days_failed_both == 0
        # The all-loads CTL view carries the definition difference: its
        # deviation equals the decayed excluded strength load,
        # 10 * k with k = 1 - e^(-1/42) (the parent's measured pattern,
        # e.g. 7k = 0.1647 on real data) — REPORTED, never hidden, and
        # absorbed by the owner-agreed hybrid tolerance (0.235 < 0.5 pts
        # and ~0.4% < 10%), never by widening the tolerance.
        assert result.ctl_all_loads.max_abs_dev == pytest.approx(
            10.0 * (1.0 - math.exp(-1.0 / 42.0))
        )


class TestHybridTolerance:
    """PASS per day: relative within --tolerance OR absolute within
    --absolute-tolerance; both statistics are reported."""

    def single_day(
        self,
        our_value: float,
        their_value: float,
        **kwargs: float,
    ) -> PmcCrossCheckResult:
        all_days = (pmc_day(0, our_value, our_value, tss=10.0),)
        aerobic_days = (pmc_day(0, our_value, our_value, tss=10.0),)
        return compare_pmc_series(
            all_days, aerobic_days, {DAY: (their_value, their_value)}, **kwargs
        )

    def test_relative_bound_only(self) -> None:
        # |104 - 100| / 100 = 4% (within 10%) but 4.0 points (> 0.5).
        result = self.single_day(104.0, 100.0)
        day = result.ctl_aerobic_only.days[0]
        assert day.within_rel and not day.within_abs and day.passed
        assert result.ctl_aerobic_only.days_within_rel == 1
        assert result.ctl_aerobic_only.days_within_abs == 0

    def test_absolute_bound_only(self) -> None:
        # |1.15 - 1.0| / 1.0 = 15% (> 10%) but 0.15 points (<= 0.5).
        result = self.single_day(1.15, 1.0)
        day = result.ctl_aerobic_only.days[0]
        assert day.within_abs and not day.within_rel and day.passed
        assert result.ctl_aerobic_only.days_within_abs == 1

    def test_both_bounds_fail(self) -> None:
        # 15% relative AND 15 points absolute: fails both, verdict FAIL.
        result = self.single_day(115.0, 100.0)
        day = result.ctl_aerobic_only.days[0]
        assert not day.within_rel and not day.within_abs and not day.passed
        assert result.ctl_aerobic_only.failed_dates == (DAY,)
        assert not result.passed

    def test_exact_relative_boundary_is_inclusive(self) -> None:
        # Exactly 10% relative deviation passes.
        result = self.single_day(110.0, 100.0)
        assert result.ctl_aerobic_only.days[0].within_rel
        assert result.passed

    def test_exact_absolute_boundary_is_inclusive(self) -> None:
        # Exactly 0.5 points passes the absolute bound (15% > 10% rel).
        result = self.single_day(1.5, 1.0)
        day = result.ctl_aerobic_only.days[0]
        assert day.within_abs and not day.within_rel
        assert result.passed

    def test_both_bounds_reported_in_statistics(self) -> None:
        all_days = (
            pmc_day(0, 104.0, 104.0, tss=10.0),  # rel 4%, abs 4.0
            pmc_day(1, 1.15, 1.15, tss=10.0),  # rel 15%, abs 0.15
        )
        aerobic_days = all_days
        values = {
            DAY: (100.0, 100.0),
            DAY + dt.timedelta(days=1): (1.0, 1.0),
        }
        result = compare_pmc_series(all_days, aerobic_days, values)
        comp = result.ctl_aerobic_only
        assert comp.median_rel_dev == pytest.approx(0.095)  # (0.04 + 0.15)/2
        assert comp.max_rel_dev == pytest.approx(0.15)
        assert comp.median_abs_dev == pytest.approx((4.0 + 0.15) / 2)
        assert comp.max_abs_dev == pytest.approx(4.0)
        assert comp.days_within_rel == 1
        assert comp.days_within_abs == 1
        assert comp.days_failed_both == 0

    def test_invalid_tolerances_raise(self) -> None:
        all_days = (pmc_day(0, 1.0, 1.0, tss=10.0),)
        aerobic_days = (pmc_day(0, 1.0, 1.0, tss=10.0),)
        values = {DAY: (1.0, 1.0)}
        with pytest.raises(ValueError, match="tolerance"):
            compare_pmc_series(all_days, aerobic_days, values, tolerance=0.0)
        with pytest.raises(ValueError, match="absolute"):
            compare_pmc_series(
                all_days, aerobic_days, values, absolute_tolerance=0.0
            )
        with pytest.raises(ValueError, match="discontinuity"):
            compare_pmc_series(
                all_days, aerobic_days, values, discontinuity_threshold=0.0
            )


class TestTheirSideRevisions:
    """Days where THEIR series jumps beyond the threshold with no activity
    load that day are Intervals-side revisions, not our error."""

    def test_detects_ctl_revision_on_activity_free_day(self) -> None:
        loads = {DAY: 0.0, DAY + dt.timedelta(days=1): 0.0}
        their_ctl = {DAY: 42.0, DAY + dt.timedelta(days=1): 40.5}
        their_atl = {DAY: 10.0, DAY + dt.timedelta(days=1): 10.0 * math.exp(-1.0 / 7.0)}
        revisions = detect_their_side_revisions(loads, their_ctl, their_atl)
        assert len(revisions) == 1
        rev = revisions[0]
        assert rev.series == "CTL"
        assert rev.date == DAY + dt.timedelta(days=1)
        assert rev.previous_value == pytest.approx(42.0)
        assert rev.expected_value == pytest.approx(42.0 * math.exp(-1.0 / 42.0))
        assert rev.actual_value == pytest.approx(40.5)
        assert rev.deviation == pytest.approx(
            abs(40.5 - 42.0 * math.exp(-1.0 / 42.0))
        )

    def test_detects_atl_revision_with_tau_seven(self) -> None:
        loads = {DAY: 0.0, DAY + dt.timedelta(days=1): 0.0}
        their_ctl = {DAY: 42.0, DAY + dt.timedelta(days=1): 42.0 * math.exp(-1.0 / 42.0)}
        their_atl = {DAY: 20.0, DAY + dt.timedelta(days=1): 19.0}
        revisions = detect_their_side_revisions(loads, their_ctl, their_atl)
        assert len(revisions) == 1
        assert revisions[0].series == "ATL"
        assert revisions[0].expected_value == pytest.approx(20.0 * math.exp(-1.0 / 7.0))

    def test_pure_decay_day_is_not_a_revision(self) -> None:
        loads = {DAY: 0.0, DAY + dt.timedelta(days=1): 0.0}
        their_ctl = {
            DAY: 42.0,
            DAY + dt.timedelta(days=1): 42.0 * math.exp(-1.0 / 42.0),
        }
        their_atl = {
            DAY: 20.0,
            DAY + dt.timedelta(days=1): 20.0 * math.exp(-1.0 / 7.0),
        }
        assert detect_their_side_revisions(loads, their_ctl, their_atl) == ()

    def test_day_with_activity_load_is_never_a_revision(self) -> None:
        loads = {DAY: 0.0, DAY + dt.timedelta(days=1): 80.0}
        their_ctl = {DAY: 42.0, DAY + dt.timedelta(days=1): 30.0}
        their_atl = {DAY: 20.0, DAY + dt.timedelta(days=1): 40.0}
        assert detect_their_side_revisions(loads, their_ctl, their_atl) == ()

    def test_missing_their_value_disables_detection_for_that_day(self) -> None:
        loads = {DAY: 0.0, DAY + dt.timedelta(days=1): 0.0}
        their_ctl = {DAY: 42.0, DAY + dt.timedelta(days=1): None}
        # Their ATL follows pure decay: nothing unexplained anywhere.
        their_atl = {
            DAY: 20.0,
            DAY + dt.timedelta(days=1): 20.0 * math.exp(-1.0 / 7.0),
        }
        assert detect_their_side_revisions(loads, their_ctl, their_atl) == ()

    def test_first_window_day_has_no_previous_day(self) -> None:
        loads = {DAY: 0.0}
        their_ctl = {DAY: 42.0}
        their_atl = {DAY: 20.0}
        assert detect_their_side_revisions(loads, their_ctl, their_atl) == ()

    def test_threshold_respected(self) -> None:
        loads = {DAY: 0.0, DAY + dt.timedelta(days=1): 0.0}
        deviation = abs(40.5 - 42.0 * math.exp(-1.0 / 42.0))
        their_ctl = {DAY: 42.0, DAY + dt.timedelta(days=1): 40.5}
        their_atl = {DAY: 0.0, DAY + dt.timedelta(days=1): 0.0}
        just_below = detect_their_side_revisions(
            loads, their_ctl, their_atl, threshold=deviation * 1.0001
        )
        assert just_below == ()
        just_above = detect_their_side_revisions(
            loads, their_ctl, their_atl, threshold=deviation * 0.9999
        )
        assert len(just_above) == 1

    def test_revision_day_is_excluded_from_stats_and_verdict(self) -> None:
        # Their CTL revised on a zero-load day: the like-for-like CTL
        # comparison would fail hugely on that day, but the deviation is
        # THEIR revision — excluded from the verdict and listed explicitly.
        all_days = (
            pmc_day(0, 55.0, 40.0, tss=0.0),
            pmc_day(1, 55.0, 40.0, tss=0.0),
        )
        aerobic_days = (
            pmc_day(0, 55.0, 20.0, tss=0.0),
            pmc_day(1, 55.0, 20.0, tss=0.0),
        )
        values = {
            DAY: (55.0, 40.0),
            DAY + dt.timedelta(days=1): (30.0, 40.0),  # revised down
        }
        result = compare_pmc_series(all_days, aerobic_days, values)
        assert len(result.their_ctl_revisions) == 1
        assert result.their_ctl_revisions[0].date == DAY + dt.timedelta(days=1)
        assert result.ctl_aerobic_only.days[1].their_side_excluded
        assert result.ctl_aerobic_only.failed_dates == ()
        # The revision day is excluded from the statistics too.
        assert result.ctl_aerobic_only.comparable_days == 1
        assert result.ctl_aerobic_only.max_rel_dev == pytest.approx(0.0)
        assert result.passed

    def test_revision_below_threshold_stays_inside_the_statistics(self) -> None:
        # A small unexplained wiggle is below the documented threshold: it
        # is NOT excluded and counts like any other deviation.
        small_revision = 42.0 * math.exp(-1.0 / 42.0) - 0.1
        all_days = (pmc_day(0, 42.0, 10.0, tss=0.0), pmc_day(1, 42.0, 10.0, tss=0.0))
        aerobic_days = all_days
        values = {
            DAY: (42.0, 10.0),
            DAY + dt.timedelta(days=1): (small_revision, 10.0),
        }
        result = compare_pmc_series(all_days, aerobic_days, values)
        assert result.their_ctl_revisions == ()
        assert result.ctl_aerobic_only.days[1].their_side_excluded is False
        assert result.ctl_aerobic_only.days_failed_both == 0  # within abs 0.5


class TestMissingValues:
    """Missing cross-check values are reported, never skipped or invented."""

    def test_missing_values_are_reported_not_skipped(self) -> None:
        all_days = (
            pmc_day(0, 30.0, 40.0, tss=10.0),
            pmc_day(1, 50.0, 60.0, tss=10.0),
            pmc_day(2, 70.0, 80.0, tss=10.0),
        )
        aerobic_days = all_days
        values: dict[dt.date, tuple[float | None, float | None]] = {
            DAY: (None, 40.0),  # CTL missing on day 0
            DAY + dt.timedelta(days=1): (50.0, 60.0),
            # day 2 entirely absent
        }
        result = compare_pmc_series(all_days, aerobic_days, values)
        assert result.missing_ctl_dates == (DAY, DAY + dt.timedelta(days=2))
        assert result.missing_atl_dates == (DAY + dt.timedelta(days=2),)
        assert result.ctl_aerobic_only.comparable_days == 1
        assert result.ctl_aerobic_only.days[0].their_value is None
        assert result.days[0].their_ctl is None

    def test_no_comparable_day_cannot_pass(self) -> None:
        all_days = (pmc_day(0, 30.0, 40.0, tss=10.0),)
        aerobic_days = all_days
        result = compare_pmc_series(all_days, aerobic_days, {})
        assert not result.passed
        assert result.ctl_aerobic_only.comparable_days == 0
        assert result.missing_ctl_dates == (DAY,)
        assert result.missing_atl_dates == (DAY,)

    def test_their_zero_with_our_load_fails_via_absolute_bound(self) -> None:
        # Relative deviation vs a 0 baseline is undefined; the absolute
        # bound still applies (5.0 points > 0.5): the day FAILS.
        all_days = (pmc_day(0, 5.0, 5.0, tss=10.0),)
        aerobic_days = all_days
        result = compare_pmc_series(all_days, aerobic_days, {DAY: (0.0, 0.0)})
        day = result.ctl_aerobic_only.days[0]
        assert day.rel_dev is None
        assert day.abs_dev == pytest.approx(5.0)
        assert not day.passed
        assert not result.passed

    def test_both_zero_passes(self) -> None:
        all_days = (pmc_day(0, 0.0, 0.0, tss=0.0),)
        aerobic_days = all_days
        result = compare_pmc_series(all_days, aerobic_days, {DAY: (0.0, 0.0)})
        assert result.passed
        assert result.ctl_aerobic_only.days[0].rel_dev == 0.0

    def test_end_to_end_identical_inputs_pass_both_views(self) -> None:
        """Our compute_pmc over a synthetic series vs 'Intervals' values
        produced BY the same recursion: identical by construction, PASS."""
        start = DAY - dt.timedelta(days=199)
        loads = {
            start + dt.timedelta(days=offset): (60.0 if offset % 3 == 0 else 0.0)
            for offset in range(200)
        }
        series = compute_pmc(loads).days
        values = {day.date: (day.ctl, day.atl) for day in series[-30:]}
        result = compare_pmc_series(series[-30:], series[-30:], values)
        assert result.passed
        assert result.ctl_aerobic_only.comparable_days == 30
        assert result.ctl_aerobic_only.max_rel_dev == pytest.approx(0.0)
        assert result.their_ctl_revisions == ()
        assert result.their_atl_revisions == ()


class TestBuildDailyLoadSeries:
    """Our daily load series in TWO views: all loads and aerobic-only."""

    WINDOW_START = dt.date(2026, 4, 1)
    WINDOW_END = dt.date(2026, 4, 7)

    def build(
        self, activities: list[ActivityRow]
    ) -> tuple[dict[dt.date, float], dict[dt.date, float], int]:
        return build_daily_load_series(
            activities, self.WINDOW_START, self.WINDOW_END
        )

    def test_sums_per_day_and_fills_rest_days_with_zero(self) -> None:
        all_series, aerobic_series, without_load = self.build(
            [
                activity(day=dt.date(2026, 4, 1), load=100.0, source_id="a"),
                activity(day=dt.date(2026, 4, 3), load=40.0, source_id="b"),
                activity(day=dt.date(2026, 4, 3), load=60.0, source_id="c"),
            ]
        )
        assert all_series[dt.date(2026, 4, 1)] == pytest.approx(100.0)
        assert all_series[dt.date(2026, 4, 3)] == pytest.approx(100.0)
        # No strength here: both views are identical.
        assert aerobic_series == all_series
        # Contiguous window, rest days explicit 0.0 (compute_pmc contract).
        assert len(all_series) == 7
        assert all_series[dt.date(2026, 4, 2)] == 0.0
        assert all_series[dt.date(2026, 4, 7)] == 0.0
        assert without_load == 0

    def test_strength_load_is_in_all_view_only(self) -> None:
        all_series, aerobic_series, _ = self.build(
            [
                activity(
                    day=dt.date(2026, 4, 2),
                    load=13.0,
                    type="WeightTraining",
                    source_id="s1",
                ),
                activity(day=dt.date(2026, 4, 2), load=80.0, source_id="r1"),
            ]
        )
        assert all_series[dt.date(2026, 4, 2)] == pytest.approx(93.0)
        assert aerobic_series[dt.date(2026, 4, 2)] == pytest.approx(80.0)

    def test_strength_classification_is_case_insensitive(self) -> None:
        for sport_type in ("WeightTraining", "STRENGTHWORKOUT", "Workout"):
            all_series, aerobic_series, _ = self.build(
                [activity(day=dt.date(2026, 4, 2), load=7.0, type=sport_type)]
            )
            assert all_series[dt.date(2026, 4, 2)] == pytest.approx(7.0)
            assert aerobic_series[dt.date(2026, 4, 2)] == 0.0

    def test_strength_sports_match_the_engine_classification(self) -> None:
        # The tool must classify exactly like the engine (imported helper,
        # not duplicated literals).
        assert {"weighttraining", "strengthworkout", "workout"} == STRENGTH_SPORTS
        assert is_strength_sport("WeightTraining")
        assert not is_strength_sport("Ride")

    def test_activity_without_stored_load_is_counted_not_summed(self) -> None:
        all_series, aerobic_series, without_load = self.build(
            [
                activity(day=dt.date(2026, 4, 2), load=None, source_id="a"),
                activity(day=dt.date(2026, 4, 2), load=25.0, source_id="b"),
            ]
        )
        assert all_series[dt.date(2026, 4, 2)] == pytest.approx(25.0)
        assert aerobic_series[dt.date(2026, 4, 2)] == pytest.approx(25.0)
        assert without_load == 1

    def test_activity_outside_the_window_is_excluded(self) -> None:
        all_series, _, without_load = self.build(
            [
                activity(day=dt.date(2026, 3, 31), load=999.0, source_id="early"),
                activity(day=dt.date(2026, 4, 8), load=999.0, source_id="late"),
            ]
        )
        assert all(value == 0.0 for value in all_series.values())
        assert without_load == 0

    def test_local_start_date_is_the_attribution_day(self) -> None:
        """Same day attribution as the persistence service: local date when
        it parses (a 23:30 local walk belongs to that local day)."""
        row = ActivityRow(
            source="intervals",
            source_id="tz",
            type="Walk",
            start_time=dt.datetime(2026, 4, 2, 23, 30, tzinfo=dt.UTC),
            start_time_local="2026-04-03T00:30:00+02:00",
            duration_s=3600,
            intervals_icu_load=10.0,
        )
        all_series, _, _ = self.build([row])
        assert all_series[dt.date(2026, 4, 3)] == pytest.approx(10.0)
        assert all_series[dt.date(2026, 4, 2)] == 0.0


def test_default_tolerances_are_the_documented_values() -> None:
    assert pytest.approx(0.10) == DEFAULT_TOLERANCE
    assert pytest.approx(0.5) == DEFAULT_ABSOLUTE_TOLERANCE
    assert 0.0 < DEFAULT_DISCONTINUITY_THRESHOLD < DEFAULT_ABSOLUTE_TOLERANCE
