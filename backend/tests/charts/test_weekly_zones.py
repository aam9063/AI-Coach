"""Snapshot tests for the weekly time-in-zone (3-zone) chart (CH-6, §10/§7.5).

Same contract as the weekly TSS tests: pure renderer, committed PNG
baseline, explicit §10 width check, loud rejection of empty input.
Extra pins for this chart: negative zone minutes are rejected, a zone
tuple that is not exactly three values is rejected, and zones keep one
fixed color in every week (the color encodes the zone, not the sport).

The canonical fixture is computed here from fixed arithmetic — 8 weeks
of ride/run/swim with a 70/20/10 pyramidal zone distribution and growing
weekly volume — so the baseline is reproducible without touching the
shared fixtures module.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import cast

import pytest

from app.charts.weekly_zones import WeeklyZoneSample, render_weekly_time_in_zone
from tests.charts.snapshot import assert_png_snapshot, png_dimensions

CHART_NAME = "weekly_time_in_zone"

FIRST_WEEK = date(2026, 8, 3)  # a Monday


def _week_start(week_index: int) -> date:
    return FIRST_WEEK + timedelta(weeks=week_index)


def _pyramidal_zones(total_minutes: float) -> tuple[float, float, float]:
    """70/20/10 split: zone 1 base, zone 2 threshold, zone 3 top."""
    return (total_minutes * 0.70, total_minutes * 0.20, total_minutes * 0.10)


WEEKLY_ZONE_SAMPLES = tuple(
    WeeklyZoneSample(
        week_start=_week_start(week),
        sport_zones=(
            ("ride", _pyramidal_zones(120.0 + 10.0 * week)),
            ("run", _pyramidal_zones(60.0 + 5.0 * week)),
            ("swim", _pyramidal_zones(30.0 + 3.0 * week)),
        ),
    )
    for week in range(8)
)


def test_matches_snapshot_baseline() -> None:
    png = render_weekly_time_in_zone(WEEKLY_ZONE_SAMPLES)
    assert_png_snapshot(png, CHART_NAME)


def test_is_1080_px_wide_per_rendering_conventions() -> None:
    png = render_weekly_time_in_zone(WEEKLY_ZONE_SAMPLES)
    width, _height = png_dimensions(png)
    assert width == 1080


def test_empty_samples_are_rejected_not_rendered() -> None:
    with pytest.raises(ValueError, match="no samples"):
        render_weekly_time_in_zone(())


def test_negative_minutes_are_rejected_not_rendered_below_the_axis() -> None:
    bad = (
        WeeklyZoneSample(
            week_start=date(2026, 8, 3),
            sport_zones=(("ride", (70.0, -5.0, 10.0)),),
        ),
    )
    with pytest.raises(ValueError, match="negative"):
        render_weekly_time_in_zone(bad)


def test_a_zone_tuple_without_exactly_three_values_is_rejected() -> None:
    bad_zones: tuple[float, ...] = (70.0, 20.0)  # deliberately malformed
    bad = (
        WeeklyZoneSample(
            week_start=date(2026, 8, 3),
            sport_zones=(("ride", cast("tuple[float, float, float]", bad_zones)),),
        ),
    )
    with pytest.raises(ValueError, match="exactly 3"):
        render_weekly_time_in_zone(bad)
