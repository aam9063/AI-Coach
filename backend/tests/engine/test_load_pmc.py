"""Reference-value and semantics tests for the Performance Manager (ODD LOAD-7).

Every expected value is hand-derived from the recursion documented in
``app/engine/pmc.py`` (Coggan Performance Manager, Allen & Coggan "Training
and Racing with a Power Meter", chapter on the Performance Manager Chart):

    CTL_t = CTL_{t-1} + (TSS_t - CTL_{t-1}) * (1 - e^(-1/tau_ctl))   tau_ctl = 42 d
    ATL_t = ATL_{t-1} + (TSS_t - ATL_{t-1}) * (1 - e^(-1/tau_atl))   tau_atl = 7 d
    TSB_t = CTL_{t-1} - ATL_{t-1}

With seed 0 and a constant daily TSS of 100 the recursion has the closed
form CTL_n = 100 * (1 - e^(-n/tau)), giving the reference points below
(arithmetic: e^(-1/42) = 0.9764716867, e^(-2/42) = 0.9534969548,
e^(-1/7) = 0.8668778998, e^(-2/7) = 0.7514772931):

    k42  = 1 - e^(-1/42) = 0.0235283133   -> CTL day 1 = 2.3528313348
    CTL day 2 = 100 * (1 - e^(-2/42))     = 4.6503045167
    k7   = 1 - e^(-1/7)  = 0.1331221002   -> ATL day 1 = 13.3122100250
    ATL day 2 = 100 * (1 - e^(-2/7))      = 24.8522706925
    TSB day 1 = seed_ctl - seed_atl       = 0.0
    TSB day 2 = CTL_1 - ATL_1 = 2.3528313348 - 13.3122100250 = -10.9593786902

Seeded case (seed_ctl=50, seed_atl=40, TSS=100 on day 1):

    CTL day 1 = 50 + (100 - 50) * k42 = 51.1764156674
    ATL day 1 = 40 + (100 - 40) * k7  = 47.9873260150
    TSB day 1 = 50 - 40               = 10.0

Time-constant configurability (tau_ctl = 14): 100 * (1 - e^(-1/14)) with
e^(-1/14) = 0.9310627797 -> 6.8937220296.

Convergence: after 300 days at constant 100, CTL = 100 * (1 - e^(-300/42))
= 99.9209509677 and ATL = 100 * (1 - e^(-300/7)) ~ 100 (to machine precision).

Gap/rest-day semantics under test: the daily series must contain one entry
per calendar day with rest days present as explicit 0.0; a missing calendar
day raises ``ValueError`` (no silent misalignment).
"""

import datetime as dt
from typing import Final

import pytest

from app.engine.pmc import (
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_TAU_ATL_DAYS,
    DEFAULT_TAU_CTL_DAYS,
    PmcSeries,
    compute_pmc,
    compute_pmc_per_sport,
)

D0: Final = dt.date(2026, 1, 1)


def daily(tss_by_offset: dict[int, float]) -> dict[dt.date, float]:
    """Build a gap-free daily mapping anchored at D0."""
    return {D0 + dt.timedelta(days=i): tss for i, tss in tss_by_offset.items()}


# Hand-derived reference values (see module docstring arithmetic).
CTL_DAY1: Final = 2.3528313348
CTL_DAY2: Final = 4.6503045167
ATL_DAY1: Final = 13.3122100250
ATL_DAY2: Final = 24.8522706925
TSB_DAY2: Final = -10.9593786902
CTL_DAY1_SEED50: Final = 51.1764156674
ATL_DAY1_SEED40: Final = 47.9873260150
CTL_DAY1_TAU14: Final = 6.8937220296
CTL_300D: Final = 99.9209509677


class TestReferenceValues:
    def test_constant_100_tss_first_two_days(self) -> None:
        series = compute_pmc(daily({0: 100.0, 1: 100.0}))
        assert series.days[0].ctl == pytest.approx(CTL_DAY1, abs=1e-9)
        assert series.days[0].atl == pytest.approx(ATL_DAY1, abs=1e-9)
        assert series.days[1].ctl == pytest.approx(CTL_DAY2, abs=1e-9)
        assert series.days[1].atl == pytest.approx(ATL_DAY2, abs=1e-9)

    def test_tsb_uses_previous_days_ctl_and_atl(self) -> None:
        series = compute_pmc(daily({0: 100.0, 1: 100.0}))
        # TSB_t = CTL_{t-1} - ATL_{t-1}; day 1 uses the seeds (0 - 0).
        assert series.days[0].tsb == pytest.approx(0.0, abs=1e-12)
        assert series.days[1].tsb == pytest.approx(TSB_DAY2, abs=1e-9)

    def test_sequence_converges_toward_constant_load(self) -> None:
        loads = daily({i: 100.0 for i in range(300)})
        series = compute_pmc(loads)
        assert series.days[-1].ctl == pytest.approx(CTL_300D, abs=1e-9)
        assert series.days[-1].atl == pytest.approx(100.0, abs=1e-6)
        # Monotone approach from below with seed 0 and constant load.
        ctls = [day.ctl for day in series.days]
        assert ctls == sorted(ctls)
        assert ctls[0] < 3.0 and ctls[-1] > 99.0

    def test_seeding_is_explicit_and_honoured(self) -> None:
        series = compute_pmc(daily({0: 100.0}), seed_ctl=50.0, seed_atl=40.0)
        assert series.days[0].ctl == pytest.approx(CTL_DAY1_SEED50, abs=1e-9)
        assert series.days[0].atl == pytest.approx(ATL_DAY1_SEED40, abs=1e-9)
        # TSB on day 1 uses the supplied seeds, not 0.
        assert series.days[0].tsb == pytest.approx(10.0, abs=1e-12)

    def test_default_seeds_are_zero(self) -> None:
        series = compute_pmc(daily({0: 100.0}))
        assert series.days[0].ctl == pytest.approx(CTL_DAY1, abs=1e-9)

    def test_time_constants_are_configurable(self) -> None:
        assert DEFAULT_TAU_CTL_DAYS == 42.0
        assert DEFAULT_TAU_ATL_DAYS == 7.0
        series = compute_pmc(daily({0: 100.0}), tau_ctl_days=14.0)
        assert series.days[0].ctl == pytest.approx(CTL_DAY1_TAU14, abs=1e-9)


class TestDailySeriesValidation:
    def test_missing_calendar_day_raises(self) -> None:
        # Rest day 2 must be present with explicit 0.0; omitting it is a gap.
        loads = {D0: 100.0, D0 + dt.timedelta(days=2): 100.0}
        with pytest.raises(ValueError, match="gap"):
            compute_pmc(loads)

    def test_explicit_zero_rest_day_is_accepted(self) -> None:
        series = compute_pmc(daily({0: 100.0, 1: 0.0, 2: 100.0}))
        assert [day.tss for day in series.days] == [100.0, 0.0, 100.0]
        assert len(series.days) == 3

    def test_empty_series_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            compute_pmc({})

    def test_negative_tss_raises(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            compute_pmc(daily({0: 100.0, 1: -5.0}))

    def test_non_positive_time_constant_raises(self) -> None:
        with pytest.raises(ValueError, match="tau"):
            compute_pmc(daily({0: 100.0}), tau_ctl_days=0.0)

    def test_single_day_series_is_valid(self) -> None:
        series = compute_pmc(daily({0: 100.0}))
        assert series.history_days == 1
        assert len(series.days) == 1


class TestLowConfidenceFlag:
    def test_default_threshold_is_90_days(self) -> None:
        assert DEFAULT_MIN_HISTORY_DAYS == 90

    def test_89_days_is_low_confidence(self) -> None:
        series = compute_pmc(daily({i: 50.0 for i in range(89)}))
        assert series.history_days == 89
        assert series.confident is False

    def test_90_days_is_confident(self) -> None:
        series = compute_pmc(daily({i: 50.0 for i in range(90)}))
        assert series.history_days == 90
        assert series.confident is True

    def test_threshold_is_configurable(self) -> None:
        series_29 = compute_pmc(daily({i: 50.0 for i in range(29)}), min_history_days=30)
        series_30 = compute_pmc(daily({i: 50.0 for i in range(30)}), min_history_days=30)
        assert series_29.confident is False
        assert series_30.confident is True


class TestPerSport:
    def test_per_sport_and_combined_match_direct_computation(self) -> None:
        bike = daily({0: 60.0, 1: 60.0, 2: 60.0})
        run = daily({0: 40.0, 1: 40.0, 2: 40.0})
        result = compute_pmc_per_sport({"bike": bike, "run": run})
        assert set(result.per_sport) == {"bike", "run"}
        assert result.per_sport["bike"] == compute_pmc(bike)
        assert result.per_sport["run"] == compute_pmc(run)
        combined = {D0 + dt.timedelta(days=i): 100.0 for i in range(3)}
        assert result.combined == compute_pmc(combined)

    def test_combined_covers_union_of_dates_with_zero_fill(self) -> None:
        # Bike trains on day 1, run on day 3; day 2 is a combined rest day (0.0).
        bike = daily({0: 100.0})
        run = daily({2: 100.0})
        result = compute_pmc_per_sport({"bike": bike, "run": run})
        assert [day.date for day in result.combined.days] == [
            D0,
            D0 + dt.timedelta(days=1),
            D0 + dt.timedelta(days=2),
        ]
        assert [day.tss for day in result.combined.days] == [100.0, 0.0, 100.0]

    def test_per_sport_result_is_pmc_series(self) -> None:
        result = compute_pmc_per_sport({"swim": daily({0: 50.0})})
        assert isinstance(result.per_sport["swim"], PmcSeries)
        assert isinstance(result.combined, PmcSeries)

    def test_empty_sport_mapping_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            compute_pmc_per_sport({})

    def test_gap_inside_a_sport_series_propagates(self) -> None:
        gappy = {D0: 100.0, D0 + dt.timedelta(days=2): 100.0}
        with pytest.raises(ValueError, match="gap"):
            compute_pmc_per_sport({"bike": gappy})
