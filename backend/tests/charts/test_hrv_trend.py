"""Snapshot tests for the HRV ln rMSSD chart with baseline band (CH-7).

Same contract as the Performance Manager and weekly TSS chart tests:
pure renderer, committed PNG baseline, explicit §10 width check, loud
rejection of empty input. Extra pins for this chart: the readiness
baseline band must never be drawn inverted (lower above upper), and the
canonical fixture is computed from fixed formulas here — not imported
from tests/charts/fixtures.py — so the chart's shape is pinned by this
file alone.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest

from tests.charts.snapshot import assert_png_snapshot, png_dimensions

if TYPE_CHECKING:
    from app.charts.hrv_trend import HrvSample

CHART_NAME = "hrv_trend"
FIRST_DAY = date(2026, 9, 1)
N_DAYS = 30
BASELINE_CENTER = 4.0
BASELINE_HALF_WIDTH = 0.25
AMPLITUDE = 0.15


def _hrv_samples() -> tuple[HrvSample, ...]:
    """Canonical fixture: fixed formulas only, no randomness, no wall clock.

    ln rMSSD wobbles around the readiness engine's rolling baseline,
    whose band is a constant 4.0 +/- 0.25 across the window.
    """
    from app.charts.hrv_trend import HrvSample

    return tuple(
        HrvSample(
            day=FIRST_DAY + timedelta(days=i),
            ln_rmssd=4.1 + AMPLITUDE * math.sin(i / 3),
            baseline_lower=BASELINE_CENTER - BASELINE_HALF_WIDTH,
            baseline_upper=BASELINE_CENTER + BASELINE_HALF_WIDTH,
        )
        for i in range(N_DAYS)
    )


def test_matches_snapshot_baseline() -> None:
    from app.charts.hrv_trend import render_hrv_trend

    png = render_hrv_trend(_hrv_samples())
    assert_png_snapshot(png, CHART_NAME)


def test_is_1080_px_wide_per_rendering_conventions() -> None:
    from app.charts.hrv_trend import render_hrv_trend

    png = render_hrv_trend(_hrv_samples())
    width, _height = png_dimensions(png)
    assert width == 1080


def test_empty_samples_are_rejected_not_rendered() -> None:
    from app.charts.hrv_trend import render_hrv_trend

    with pytest.raises(ValueError, match="no samples"):
        render_hrv_trend(())


def test_inverted_baseline_band_is_rejected_not_rendered() -> None:
    from app.charts.hrv_trend import HrvSample, render_hrv_trend

    bad = (
        HrvSample(
            day=FIRST_DAY,
            ln_rmssd=4.1,
            baseline_lower=BASELINE_CENTER + BASELINE_HALF_WIDTH,
            baseline_upper=BASELINE_CENTER - BASELINE_HALF_WIDTH,
        ),
    )
    with pytest.raises(ValueError, match="inverted baseline band"):
        render_hrv_trend(bad)
