"""Resting-HR and sleep readiness vs the athlete's own 30-day baseline
(RID-2, PROJECT_BRIEF section 7.4).

Contract under test (``app.engine.readiness.resting_hr_readiness`` /
``app.engine.readiness.sleep_readiness``):

- The observed value is the daily measurement ON the series' last date (the
  as-of date). The baseline is the athlete's OWN previous 30 calendar days,
  EXCLUDING the as-of day itself (the observed day never contributes to its
  own baseline), mean and SAMPLE SD (ddof=1) over the valid days only.
- Direction vocabulary: ``"elevated"`` (observed strictly above the band),
  ``"normal"`` (inside the band), ``"low"`` (strictly below) — the direction
  describes the OBSERVED VALUE's relation to the baseline band, never the
  advice; the multi-signal warning rule (RID-3) interprets it.
- The ± 0.5 SD band is strict at the boundary (exactly at it does NOT flag),
  mirroring the HRV signal and the ZON-9 margin precedent.
- A missing observation on the as-of date or a short baseline history is
  reported explicitly as ``insufficient_data``, never substituted.

Hand-computed arithmetic
------------------------
Resting HR baseline: 30 days alternating 55 / 65 bpm (15 each):
    mean = 60.0 bpm exactly; SS = 15 x 25 + 15 x 25 = 750;
    SD (ddof=1) = sqrt(750/29) = 5 x sqrt(30/29) ≈ 5.085476 bpm.
Sleep baseline: 30 days alternating 7.0 / 8.0 h (15 each):
    mean = 7.5 h exactly; SD = 0.5 x sqrt(30/29) ≈ 0.508548 h.
"""

import dataclasses
import datetime as dt
import math
from collections.abc import Sequence

import pytest

from app.engine.readiness import (
    DEFAULT_RHR_BASELINE_DAYS,
    DEFAULT_SLEEP_BASELINE_DAYS,
    resting_hr_readiness,
    sleep_readiness,
)

_SERIES_START = dt.date(2026, 6, 1)

_RHR_SD = 5.0 * math.sqrt(30.0 / 29.0)  # ≈ 5.085476 bpm
_SLEEP_SD = 0.5 * math.sqrt(30.0 / 29.0)  # ≈ 0.508548 h


def _daily(
    values: Sequence[float | None], *, start: dt.date = _SERIES_START
) -> dict[dt.date, float | None]:
    return {
        start + dt.timedelta(days=i): value
        for i, value in enumerate(values)
    }


_RHR_BASELINE: list[float | None] = [55.0, 65.0] * 15  # mean 60, SD _RHR_SD
_SLEEP_BASELINE: list[float | None] = [7.0, 8.0] * 15  # mean 7.5, SD _SLEEP_SD


class TestRestingHrReadiness:
    def test_elevated_observation_flags(self) -> None:
        # as-of value 65 bpm: deviation +5 ≈ +0.983 SD -> outside the band.
        result = resting_hr_readiness(_daily([*_RHR_BASELINE, 65.0]))
        assert result.status == "assessed"
        assert result.key == "resting_hr"
        assert result.flagged is True
        assert result.direction == "elevated"
        assert result.observed == pytest.approx(65.0)
        assert result.baseline_mean == pytest.approx(60.0)
        assert result.baseline_sd == pytest.approx(_RHR_SD)
        assert result.deviation == pytest.approx(5.0)
        assert result.deviation_in_sd == pytest.approx(5.0 / _RHR_SD)
        assert result.baseline_days == DEFAULT_RHR_BASELINE_DAYS
        assert result.n_window_valid == 1
        assert result.n_baseline_valid == 30
        assert result.confidence == pytest.approx(1.0)

    def test_observation_inside_the_band_is_normal(self) -> None:
        result = resting_hr_readiness(_daily([*_RHR_BASELINE, 60.0]))
        assert result.status == "assessed"
        assert result.flagged is False
        assert result.direction == "normal"
        assert result.deviation == pytest.approx(0.0)

    def test_low_observation_flags_low(self) -> None:
        # 55 bpm: deviation -5 ≈ -0.983 SD. Direction describes the VALUE.
        result = resting_hr_readiness(_daily([*_RHR_BASELINE, 55.0]))
        assert result.status == "assessed"
        assert result.flagged is True
        assert result.direction == "low"
        assert result.deviation == pytest.approx(-5.0)

    def test_exactly_at_upper_boundary_does_not_flag(self) -> None:
        boundary = 60.0 + 0.5 * _RHR_SD  # ≈ 62.542738 bpm
        result = resting_hr_readiness(_daily([*_RHR_BASELINE, boundary]))
        assert result.status == "assessed"
        assert result.flagged is False
        assert result.direction == "normal"
        assert result.deviation_in_sd == pytest.approx(0.5)

    def test_baseline_excludes_the_as_of_day(self) -> None:
        # A very high as-of value must NOT inflate its own baseline: the
        # baseline mean stays 60.0 (days 0..29), not pulled toward 64.8.
        result = resting_hr_readiness(_daily([*_RHR_BASELINE, 90.0]))
        assert result.baseline_mean == pytest.approx(60.0)
        assert result.baseline_sd == pytest.approx(_RHR_SD)
        assert result.flagged is True

    def test_missing_as_of_observation_is_insufficient_data(self) -> None:
        result = resting_hr_readiness(_daily([*_RHR_BASELINE, None]))
        assert result.status == "insufficient_data"
        assert result.direction == "insufficient_data"
        assert result.flagged is None
        assert result.observed is None
        assert "missing" in result.detail.lower()

    def test_missing_baseline_day_reduces_confidence(self) -> None:
        baseline = list(_RHR_BASELINE)
        baseline[5] = None
        result = resting_hr_readiness(_daily([*baseline, 60.0]))
        assert result.status == "assessed"
        assert result.n_baseline_valid == 29
        assert result.confidence == pytest.approx(29.0 / 30.0)


class TestSleepReadiness:
    def test_elevated_duration_flags(self) -> None:
        # 8.5 h vs mean 7.5: deviation +1.0 ≈ +1.966 SD -> flagged.
        result = sleep_readiness(_daily([*_SLEEP_BASELINE, 8.5]))
        assert result.status == "assessed"
        assert result.key == "sleep_duration"
        assert result.flagged is True
        assert result.direction == "elevated"
        assert result.observed == pytest.approx(8.5)
        assert result.baseline_mean == pytest.approx(7.5)
        assert result.baseline_sd == pytest.approx(_SLEEP_SD)
        assert result.deviation == pytest.approx(1.0)
        assert result.baseline_days == DEFAULT_SLEEP_BASELINE_DAYS

    def test_below_mean_but_inside_the_band_is_normal(self) -> None:
        # 7.4 h: deviation -0.1 ≈ -0.197 SD — below the mean yet inside.
        result = sleep_readiness(_daily([*_SLEEP_BASELINE, 7.4]))
        assert result.status == "assessed"
        assert result.flagged is False
        assert result.direction == "normal"

    def test_short_duration_beyond_the_band_flags_low(self) -> None:
        # 6.9 h: deviation -0.6 ≈ -1.180 SD.
        result = sleep_readiness(_daily([*_SLEEP_BASELINE, 6.9]))
        assert result.status == "assessed"
        assert result.flagged is True
        assert result.direction == "low"
        assert result.deviation == pytest.approx(-0.6)

    def test_non_positive_duration_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            sleep_readiness(_daily([*_SLEEP_BASELINE, 0.0]))

    def test_short_history_is_insufficient_data(self) -> None:
        result = sleep_readiness(
            _daily([7.0, 8.0] * 5 + [7.5])  # 10 baseline days < 21 required
        )
        assert result.status == "insufficient_data"
        assert result.direction == "insufficient_data"
        assert result.flagged is None
        assert result.observed is None


class TestStructuredResultContract:
    """Every signal is a structured object, never a single magic score."""

    def test_no_composite_score_field(self) -> None:
        result = resting_hr_readiness(_daily([*_RHR_BASELINE, 60.0]))
        assert not hasattr(result, "score")
        assert not hasattr(result, "readiness")

    def test_frozen_result(self) -> None:
        result = sleep_readiness(_daily([*_SLEEP_BASELINE, 7.5]))
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.direction = "elevated"  # type: ignore[misc]
