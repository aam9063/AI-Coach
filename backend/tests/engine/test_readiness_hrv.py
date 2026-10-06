"""HRV readiness: ln(rMSSD) 7-day rolling mean vs 60-day baseline, ± 0.5 SD
band (RID-1, PROJECT_BRIEF section 7.4).

Contract under test (``app.engine.readiness.hrv_readiness``):

- The observed value is the TRAILING 7-day rolling mean of daily ln(rMSSD),
  ending INCLUSIVE on the series' last date. The baseline is the athlete's
  OWN previous 60 calendar days, EXCLUDING the rolling window (the two
  windows never overlap), with the mean and the SAMPLE SD (ddof=1) over the
  valid (non-``None``) days only.
- The signal is flagged when the rolling mean falls OUTSIDE
  baseline_mean ± 0.5 x baseline_SD, STRICTLY: exactly at the boundary does
  NOT flag (the ZON-9 margin precedent: 189.00 W at exactly +5% does not
  propose). The boundary comparison is tolerant to float noise.
- Missing days: a ``None`` inside the BASELINE reduces the evidence and the
  confidence but still assesses; a ``None`` inside the ROLLING WINDOW or a
  history shorter than the required baseline days is reported explicitly as
  ``insufficient_data`` — never substituted by a default.
- Constant baseline (SD = 0): the ± 0.5 SD band collapses to the baseline
  mean itself (documented owner choice, see the module docstring); any
  rolling mean tolerantly different from the mean is outside the band.

Hand-computed arithmetic for the boundary cases
-----------------------------------------------
Baseline: 60 days alternating 3.9 / 4.1 (30 days each) — a realistic
ln(rMSSD) level (e^4 ≈ 55 ms rMSSD):

    mean = (30 x 3.9 + 30 x 4.1) / 60 = 240 / 60 = 4.0 exactly
    SS   = 30 x (0.1)^2 + 30 x (0.1)^2 = 0.6
    SD   = sqrt(0.6 / 59) = 0.1 x sqrt(60/59) ≈ 0.100843897   (ddof=1)

Half band = 0.5 x SD ≈ 0.050421948, so:

    upper boundary = 4.0 + 0.5 x SD ≈ 4.050421948
    lower boundary = 4.0 - 0.5 x SD ≈ 3.949578052

A 7-day window of constant values has a rolling mean equal to that value,
so each case pins the boundary exactly:

(a) window = upper boundary exactly  -> NOT flagged (strict "outside"),
    direction "normal";
(a-) window = lower boundary exactly -> NOT flagged, "normal";
(b) window = upper boundary + 0.02   -> flagged "elevated"
    (deviation ≈ 0.070421948, ≈ 0.698 SD);
(c) window = 4.0 (the mean itself)   -> NOT flagged, deviation 0;
(d) window = lower boundary - 0.02   -> flagged "low"
    (deviation ≈ -0.070421948, ≈ -0.698 SD).

Monotone-drift probe: with v_i = 4.0 + 0.05 x i for 67 days, the baseline
(design: days 0..59) has mean 4.0 + 0.05 x 29.5 = 5.475 and sample SD
0.05 x sqrt(305) ≈ 0.873212460 (sum of squared deviations of 0..59 around
their mean is 60 x 3599 / 12 = 17995; 17995 / 59 = 305 exactly), while the
window (days 60..66) has mean 4.0 + 0.05 x 63 = 7.15: deviation 1.675,
≈ 1.918 SD — flagged. Asserting baseline_mean = 5.475 (NOT the mean of days
7..66, which would be 5.825) pins the documented "baseline excludes the
rolling window" semantics.
"""

import datetime as dt
import math
from collections.abc import Sequence

import pytest

from app.engine.readiness import (
    DEFAULT_HRV_BAND_SD,
    DEFAULT_HRV_BASELINE_DAYS,
    DEFAULT_HRV_WINDOW_DAYS,
    hrv_readiness,
)

_SERIES_START = dt.date(2026, 6, 1)

_SD = 0.1 * math.sqrt(60.0 / 59.0)  # ≈ 0.100843897 (sample SD, ddof=1)
_HALF_BAND = 0.5 * _SD  # ≈ 0.050421948
_UPPER_BOUNDARY = 4.0 + _HALF_BAND  # ≈ 4.050421948
_LOWER_BOUNDARY = 4.0 - _HALF_BAND  # ≈ 3.949578052

_BASELINE: list[float | None] = [3.9, 4.1] * 30  # 60 days, mean 4.0, SD _SD


def _hrv_series(
    window_values: Sequence[float | None],
    baseline: Sequence[float | None] = _BASELINE,
) -> dict[dt.date, float | None]:
    """A daily ln(rMSSD) mapping: ``baseline`` days then the window values."""
    values = list(baseline) + list(window_values)
    return {
        _SERIES_START + dt.timedelta(days=i): value
        for i, value in enumerate(values)
    }


class TestBandBoundaryStrictness:
    """Exactly at ± 0.5 SD does NOT flag; just beyond does (ZON-9 precedent)."""

    def test_exactly_at_upper_boundary_does_not_flag(self) -> None:
        # Case (a): rolling mean exactly mean + 0.5 SD -> inside (strict band).
        result = hrv_readiness(_hrv_series([_UPPER_BOUNDARY] * 7))
        assert result.status == "assessed"
        assert result.key == "hrv_ln_rmssd"
        assert result.flagged is False
        assert result.direction == "normal"
        assert result.observed == pytest.approx(_UPPER_BOUNDARY)
        assert result.baseline_mean == pytest.approx(4.0)
        assert result.baseline_sd == pytest.approx(_SD)
        assert result.deviation == pytest.approx(_HALF_BAND)
        assert result.deviation_in_sd == pytest.approx(0.5)
        # Complete 7-day window and complete 60-day baseline -> full confidence.
        assert result.confidence == pytest.approx(1.0)
        assert result.n_window_valid == 7
        assert result.n_baseline_valid == 60
        assert result.band_half_width_sd == pytest.approx(DEFAULT_HRV_BAND_SD)
        assert result.window_days == DEFAULT_HRV_WINDOW_DAYS
        assert result.baseline_days == DEFAULT_HRV_BASELINE_DAYS

    def test_exactly_at_lower_boundary_does_not_flag(self) -> None:
        # Case (a-): the mirrored boundary is equally inside the strict band.
        result = hrv_readiness(_hrv_series([_LOWER_BOUNDARY] * 7))
        assert result.status == "assessed"
        assert result.flagged is False
        assert result.direction == "normal"
        assert result.deviation == pytest.approx(-_HALF_BAND)
        assert result.deviation_in_sd == pytest.approx(-0.5)

    def test_just_beyond_upper_boundary_flags_elevated(self) -> None:
        # Case (b): upper boundary + 0.02 -> ≈ 0.698 SD above the mean.
        result = hrv_readiness(_hrv_series([_UPPER_BOUNDARY + 0.02] * 7))
        assert result.status == "assessed"
        assert result.flagged is True
        assert result.direction == "elevated"
        assert result.deviation == pytest.approx(_HALF_BAND + 0.02)
        assert result.deviation_in_sd == pytest.approx(
            (_HALF_BAND + 0.02) / _SD
        )
        assert result.observed == pytest.approx(_UPPER_BOUNDARY + 0.02)

    def test_window_at_the_baseline_mean_does_not_flag(self) -> None:
        # Case (c): deviation exactly 0, comfortably inside the band.
        result = hrv_readiness(_hrv_series([4.0] * 7))
        assert result.status == "assessed"
        assert result.flagged is False
        assert result.direction == "normal"
        assert result.deviation == pytest.approx(0.0)
        assert result.deviation_in_sd == pytest.approx(0.0)

    def test_just_beyond_lower_boundary_flags_low(self) -> None:
        # Case (d): the mirror of (b) below the band — suppressed HRV.
        result = hrv_readiness(_hrv_series([_LOWER_BOUNDARY - 0.02] * 7))
        assert result.status == "assessed"
        assert result.flagged is True
        assert result.direction == "low"
        assert result.deviation == pytest.approx(-(_HALF_BAND + 0.02))
        assert result.deviation_in_sd == pytest.approx(
            -(_HALF_BAND + 0.02) / _SD
        )

    def test_barely_beyond_upper_boundary_still_flags(self) -> None:
        # A margin far above the boundary tolerance (1e-12 relative) yet
        # physiologically nil (1e-9 ln units ≈ 1e-8 SD) must still flag:
        # the strict band is only relaxed by the boundary tolerance itself.
        # (A single-ULP probe cannot work here: averaging the 7 identical
        # window values rounds the ULP back onto the boundary, which IS
        # at-boundary under the documented tolerant rule.)
        just_beyond = _UPPER_BOUNDARY + 1e-9
        result = hrv_readiness(_hrv_series([just_beyond] * 7))
        assert result.status == "assessed"
        assert result.flagged is True
        assert result.direction == "elevated"


class TestMissingDays:
    """Missing days are reported explicitly, never substituted."""

    def test_missing_baseline_day_still_assesses_with_reduced_confidence(
        self,
    ) -> None:
        baseline = list(_BASELINE)
        baseline[10] = None  # one missing measurement inside the 60-day baseline
        result = hrv_readiness(_hrv_series([4.0] * 7, baseline=baseline))
        assert result.status == "assessed"
        assert result.flagged is False
        assert result.n_baseline_valid == 59
        assert result.confidence == pytest.approx(59.0 / 60.0)
        # Mean and SD recomputed over the 59 valid days only:
        # mean = (29 x 3.9 + 30 x 4.1) / 59 = 236.1 / 59 = 4.00169...
        assert result.baseline_mean == pytest.approx(236.1 / 59.0)
        assert result.baseline_sd == pytest.approx(
            0.100843896817922, rel=1e-9
        )

    def test_missing_day_inside_the_rolling_window_is_insufficient_data(
        self,
    ) -> None:
        window = [4.1, 4.1, None, 4.1, 4.1, 4.1, 4.1]
        result = hrv_readiness(_hrv_series(window))
        assert result.status == "insufficient_data"
        assert result.direction == "insufficient_data"
        assert result.flagged is None
        assert result.observed is None
        assert result.baseline_mean is None
        assert result.baseline_sd is None
        assert result.n_window_valid == 6
        assert result.n_baseline_valid == 60
        assert "window" in result.detail.lower()
        assert "6" in result.detail and "7" in result.detail

    def test_short_history_is_reported_insufficient(self) -> None:
        # 39 days total: 7-day window + 32 valid baseline days < 42 required.
        values: list[float] = ([3.9, 4.1] * 19) + [3.9]
        result = hrv_readiness(_hrv_series(values[-7:], baseline=values[:-7]))
        assert result.status == "insufficient_data"
        assert result.direction == "insufficient_data"
        assert result.flagged is None
        assert result.observed is None
        assert result.n_baseline_valid == 32
        assert "42" in result.detail and "32" in result.detail


class TestConstantBaseline:
    """SD = 0: the band collapses to the mean (documented owner choice)."""

    def test_any_deviation_above_the_mean_flags(self) -> None:
        result = hrv_readiness(_hrv_series([4.05] * 7, baseline=[4.0] * 60))
        assert result.status == "assessed"
        assert result.baseline_sd == 0.0
        assert result.flagged is True
        assert result.direction == "elevated"
        assert result.deviation == pytest.approx(0.05)
        # deviation_in_sd is undefined for SD = 0: reported as None, never inf.
        assert result.deviation_in_sd is None

    def test_any_deviation_below_the_mean_flags(self) -> None:
        result = hrv_readiness(_hrv_series([3.95] * 7, baseline=[4.0] * 60))
        assert result.status == "assessed"
        assert result.flagged is True
        assert result.direction == "low"

    def test_rolling_mean_at_the_mean_does_not_flag(self) -> None:
        result = hrv_readiness(_hrv_series([4.0] * 7, baseline=[4.0] * 60))
        assert result.status == "assessed"
        assert result.flagged is False
        assert result.direction == "normal"
        assert result.deviation_in_sd is None


class TestBaselineExcludesRollingWindow:
    """The 60-day baseline ends the day BEFORE the 7-day window starts."""

    def test_monotone_drift_probe(self) -> None:
        values = [4.0 + 0.05 * i for i in range(67)]
        result = hrv_readiness(_hrv_series(values[-7:], baseline=values[:-7]))
        assert result.status == "assessed"
        # Baseline = days 0..59 (mean 5.475), NOT days 7..66 (mean 5.825).
        assert result.baseline_mean == pytest.approx(5.475)
        assert result.baseline_sd == pytest.approx(0.05 * math.sqrt(305))
        assert result.observed == pytest.approx(7.15)
        assert result.deviation == pytest.approx(1.675)
        assert result.deviation_in_sd == pytest.approx(
            1.675 / (0.05 * math.sqrt(305))
        )
        assert result.flagged is True
        assert result.direction == "elevated"


class TestValidation:
    """Invalid inputs raise ValueError instead of degrading silently."""

    def test_empty_series_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            hrv_readiness({})

    def test_calendar_gap_raises(self) -> None:
        series = _hrv_series([4.0] * 7)
        # Remove one baseline day's KEY entirely (distinct from a None value).
        del series[_SERIES_START + dt.timedelta(days=3)]
        with pytest.raises(ValueError, match="calendar gap"):
            hrv_readiness(series)

    def test_non_finite_value_raises(self) -> None:
        series = _hrv_series([4.0] * 7)
        day = _SERIES_START + dt.timedelta(days=20)
        series[day] = float("nan")
        with pytest.raises(ValueError, match="finite"):
            hrv_readiness(series)

    def test_non_positive_window_days_raises(self) -> None:
        with pytest.raises(ValueError, match="window_days"):
            hrv_readiness(_hrv_series([4.0] * 7), window_days=0)

    def test_band_sd_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="band_sd"):
            hrv_readiness(_hrv_series([4.0] * 7), band_sd=-0.1)

    def test_min_window_valid_fraction_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="min_window_valid_fraction"):
            hrv_readiness(
                _hrv_series([4.0] * 7), min_window_valid_fraction=1.5
            )

    def test_min_baseline_valid_days_below_two_raises(self) -> None:
        # The sample SD (ddof=1) needs at least two valid baseline days.
        with pytest.raises(ValueError, match="min_baseline_valid_days"):
            hrv_readiness(_hrv_series([4.0] * 7), min_baseline_valid_days=1)

    def test_min_baseline_valid_days_above_baseline_days_raises(self) -> None:
        with pytest.raises(ValueError, match="min_baseline_valid_days"):
            hrv_readiness(
                _hrv_series([4.0] * 7), min_baseline_valid_days=61
            )
