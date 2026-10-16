"""Snapshot tests for the weekly TSS stacked-by-sport chart (CH-3, §10/§12.7).

Same contract as the Performance Manager chart tests: pure renderer,
committed PNG baseline, explicit §10 width check, loud rejection of
empty input. Extra pins for this chart: a sport that only appears in
one week does not crash the stack, and negative TSS is rejected instead
of drawing a band below the axis.
"""

from __future__ import annotations

from datetime import date

import pytest

from tests.charts.fixtures import WEEKLY_TSS_SAMPLES
from tests.charts.snapshot import assert_png_snapshot, png_dimensions

CHART_NAME = "weekly_tss_by_sport"


def test_matches_snapshot_baseline() -> None:
    from app.charts import render_weekly_tss_by_sport

    png = render_weekly_tss_by_sport(WEEKLY_TSS_SAMPLES)
    assert_png_snapshot(png, CHART_NAME)


def test_is_1080_px_wide_per_rendering_conventions() -> None:
    from app.charts import render_weekly_tss_by_sport

    png = render_weekly_tss_by_sport(WEEKLY_TSS_SAMPLES)
    width, _height = png_dimensions(png)
    assert width == 1080


def test_empty_samples_are_rejected_not_rendered() -> None:
    from app.charts import render_weekly_tss_by_sport

    with pytest.raises(ValueError, match="no samples"):
        render_weekly_tss_by_sport(())


def test_negative_tss_is_rejected_not_rendered_below_the_axis() -> None:
    from app.charts import render_weekly_tss_by_sport
    from app.charts.weekly_tss import WeeklyTssSample

    bad = (WeeklyTssSample(week_start=date(2026, 8, 31), sport_tss=(("ride", -10.0),)),)
    with pytest.raises(ValueError, match="negative TSS"):
        render_weekly_tss_by_sport(bad)


def test_a_sport_appearing_only_in_some_weeks_stacks_consistently() -> None:
    from app.charts import render_weekly_tss_by_sport
    from app.charts.weekly_tss import WeeklyTssSample

    sparse = (
        WeeklyTssSample(week_start=date(2026, 8, 31), sport_tss=(("ride", 100.0),)),
        WeeklyTssSample(week_start=date(2026, 9, 7), sport_tss=(("ride", 90.0), ("run", 30.0))),
        WeeklyTssSample(week_start=date(2026, 9, 14), sport_tss=(("run", 40.0),)),
    )
    png = render_weekly_tss_by_sport(sparse)
    width, _height = png_dimensions(png)
    assert width == 1080
