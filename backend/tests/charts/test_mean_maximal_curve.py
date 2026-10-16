"""Snapshot tests for the mean-maximal power curve with CP fit (CH-4, §10/§12.7).

Same contract as the other chart tests: pure renderer, committed PNG
baseline, explicit §10 width check, loud rejection of empty or invalid
input. Canonical fixture values are computed here from fixed formulas
(true CP model with CP=285, W'=18400 plus small fixed offsets), so the
baseline is reproducible without touching the shared fixtures module.
"""

from __future__ import annotations

import pytest

from app.charts.mean_maximal_curve import CriticalPowerFit, MeanMaximalEffort
from tests.charts.snapshot import assert_png_snapshot, png_dimensions

CHART_NAME = "mean_maximal_curve"

DURATIONS_S = (60.0, 120.0, 300.0, 600.0, 1200.0, 3600.0)
CP_W = 285.0
W_PRIME_J = 18400.0
OFFSETS_W = (4.0, -3.0, 2.5, -2.0, 1.5, -1.0)


def canonical_efforts() -> tuple[MeanMaximalEffort, ...]:
    """Best efforts sampled from the true CP model plus fixed offsets."""
    return tuple(
        MeanMaximalEffort(duration_s=d, power_w=CP_W + W_PRIME_J / d + offset)
        for d, offset in zip(DURATIONS_S, OFFSETS_W, strict=True)
    )


def canonical_fit() -> CriticalPowerFit:
    return CriticalPowerFit(critical_power_w=CP_W, w_prime_j=W_PRIME_J)


def test_matches_snapshot_baseline() -> None:
    from app.charts.mean_maximal_curve import render_mean_maximal_curve

    png = render_mean_maximal_curve(canonical_efforts(), canonical_fit())
    assert_png_snapshot(png, CHART_NAME)


def test_is_1080_px_wide_per_rendering_conventions() -> None:
    from app.charts.mean_maximal_curve import render_mean_maximal_curve

    png = render_mean_maximal_curve(canonical_efforts(), canonical_fit())
    width, _height = png_dimensions(png)
    assert width == 1080


def test_empty_efforts_are_rejected_not_rendered() -> None:
    from app.charts.mean_maximal_curve import render_mean_maximal_curve

    with pytest.raises(ValueError, match="no efforts"):
        render_mean_maximal_curve((), canonical_fit())


def test_non_positive_duration_is_rejected_not_rendered() -> None:
    from app.charts.mean_maximal_curve import render_mean_maximal_curve

    bad = (MeanMaximalEffort(duration_s=0.0, power_w=300.0),)
    with pytest.raises(ValueError, match="duration"):
        render_mean_maximal_curve(bad, canonical_fit())


def test_non_positive_power_is_rejected_not_rendered() -> None:
    from app.charts.mean_maximal_curve import render_mean_maximal_curve

    bad = (MeanMaximalEffort(duration_s=300.0, power_w=-1.0),)
    with pytest.raises(ValueError, match="power"):
        render_mean_maximal_curve(bad, canonical_fit())


def test_non_positive_w_prime_is_rejected_not_rendered() -> None:
    from app.charts.mean_maximal_curve import render_mean_maximal_curve

    bad_fit = CriticalPowerFit(critical_power_w=285.0, w_prime_j=0.0)
    with pytest.raises(ValueError, match="w_prime"):
        render_mean_maximal_curve(canonical_efforts(), bad_fit)
