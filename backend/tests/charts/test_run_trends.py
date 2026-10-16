"""Snapshot tests for the run pace/HR trend and EF trend charts (CH-5, §10).

Same contract as the Performance Manager and weekly TSS chart tests:
pure renderer, committed PNG baseline, explicit §10 width check, loud
rejection of empty input. Extra pins for these charts: non-positive
pace/HR/EF values are rejected instead of drawing an inverted or
meaningless axis, and the EF chart's mean reference line renders.
Canonical fixtures are computed here from fixed formulas so the
baselines are reproducible forever (they stay in this file, not in
tests/charts/fixtures.py, to keep CH-5 self-contained).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest

from tests.charts.snapshot import assert_png_snapshot, png_dimensions

if TYPE_CHECKING:
    from app.charts.run_trends import EfSample, RunSessionSample

PACE_HR_CHART_NAME = "run_pace_hr_trend"
EF_CHART_NAME = "ef_trend"

RUN_DAYS = 20
RUN_DAY_SPACING = 3
EF_DAYS = 20

FIRST_RUN_DAY = date(2026, 9, 1)


def _run_pace_hr_samples() -> tuple[RunSessionSample, ...]:
    """20 run days at 3-day spacing: pace drifts 330 -> 300 s/km with a
    weekly sinusoid, HR drifts 148 -> 141 bpm."""
    from app.charts.run_trends import RunSessionSample

    samples = []
    for i in range(RUN_DAYS):
        progress = i / (RUN_DAYS - 1)
        pace = 330.0 - 30.0 * progress + 4.0 * ((2 * 3.14159 * i) / 7.0 % (2 * 3.14159))
        hr = 148.0 - 7.0 * progress
        samples.append(
            RunSessionSample(
                day=FIRST_RUN_DAY + timedelta(days=RUN_DAY_SPACING * i),
                pace_s_per_km=pace,
                avg_hr_bpm=hr,
            )
        )
    return tuple(samples)


def _ef_samples() -> tuple[EfSample, ...]:
    """20 run days at 3-day spacing: EF drifts 1.40 -> 1.55 with a weekly ripple."""
    from app.charts.run_trends import EfSample

    samples = []
    for i in range(EF_DAYS):
        progress = i / (EF_DAYS - 1)
        ef = 1.40 + 0.15 * progress + 0.02 * ((2 * 3.14159 * i) / 7.0 % (2 * 3.14159))
        samples.append(
            EfSample(day=FIRST_RUN_DAY + timedelta(days=RUN_DAY_SPACING * i), ef=ef)
        )
    return tuple(samples)


RUN_PACE_HR_SAMPLES = _run_pace_hr_samples()
EF_TREND_SAMPLES = _ef_samples()


def test_run_pace_hr_trend_matches_snapshot_baseline() -> None:
    from app.charts.run_trends import render_run_pace_hr_trend

    png = render_run_pace_hr_trend(RUN_PACE_HR_SAMPLES)
    assert_png_snapshot(png, PACE_HR_CHART_NAME)


def test_run_pace_hr_trend_is_1080_px_wide_per_rendering_conventions() -> None:
    from app.charts.run_trends import render_run_pace_hr_trend

    png = render_run_pace_hr_trend(RUN_PACE_HR_SAMPLES)
    width, _height = png_dimensions(png)
    assert width == 1080


def test_ef_trend_matches_snapshot_baseline() -> None:
    from app.charts.run_trends import render_ef_trend

    png = render_ef_trend(EF_TREND_SAMPLES)
    assert_png_snapshot(png, EF_CHART_NAME)


def test_ef_trend_is_1080_px_wide_per_rendering_conventions() -> None:
    from app.charts.run_trends import render_ef_trend

    png = render_ef_trend(EF_TREND_SAMPLES)
    width, _height = png_dimensions(png)
    assert width == 1080


def test_empty_pace_hr_samples_are_rejected_not_rendered() -> None:
    from app.charts.run_trends import render_run_pace_hr_trend

    with pytest.raises(ValueError, match="no samples"):
        render_run_pace_hr_trend(())


def test_empty_ef_samples_are_rejected_not_rendered() -> None:
    from app.charts.run_trends import render_ef_trend

    with pytest.raises(ValueError, match="no samples"):
        render_ef_trend(())


def test_non_positive_pace_is_rejected_not_rendered_inverted() -> None:
    from app.charts.run_trends import RunSessionSample, render_run_pace_hr_trend

    bad = (RunSessionSample(day=FIRST_RUN_DAY, pace_s_per_km=0.0, avg_hr_bpm=150.0),)
    with pytest.raises(ValueError, match="positive"):
        render_run_pace_hr_trend(bad)


def test_non_positive_hr_is_rejected_not_rendered() -> None:
    from app.charts.run_trends import RunSessionSample, render_run_pace_hr_trend

    bad = (RunSessionSample(day=FIRST_RUN_DAY, pace_s_per_km=320.0, avg_hr_bpm=-1.0),)
    with pytest.raises(ValueError, match="positive"):
        render_run_pace_hr_trend(bad)


def test_non_positive_ef_is_rejected_not_rendered() -> None:
    from app.charts.run_trends import EfSample, render_ef_trend

    bad = (EfSample(day=FIRST_RUN_DAY, ef=0.0),)
    with pytest.raises(ValueError, match="positive"):
        render_ef_trend(bad)
