"""Snapshot tests for the race prediction breakdown chart (CH-8, §10/§7.7).

Same contract as the other chart tests: pure renderer, committed PNG
baseline, explicit §10 width check, loud rejection of invalid input.
The canonical fixture here stays SYNTHETIC (fixed formulas inside this
file): it pins the renderer's contracted prediction shape
``{point, lower, upper}`` independent of the engine. The engine exists
(app/engine/predict.py) and the engine/chart/tool agreement is proven
end-to-end in tests/agent/test_plot_metric.py. Extra pins: intervals must not be inverted,
times must be positive, labels must be non-empty, ordering is by
predicted time (not input order), and the h:mm axis formatter is
deterministic.
"""

from __future__ import annotations

import pytest

from app.charts.race_prediction import RacePredictionSample
from tests.charts.snapshot import assert_png_snapshot, png_dimensions

CHART_NAME = "race_prediction"


def _canonical_predictions() -> tuple[RacePredictionSample, ...]:
    points = (("5k", 1140.0), ("10k", 2380.0), ("Half", 4980.0), ("Marathon", 10320.0))
    return tuple(
        RacePredictionSample(
            label=label,
            point_seconds=point,
            lower_seconds=round(point * 0.97),
            upper_seconds=round(point * 1.04),
        )
        for label, point in points
    )


def test_matches_snapshot_baseline() -> None:
    from app.charts.race_prediction import render_race_prediction_breakdown

    png = render_race_prediction_breakdown(_canonical_predictions())
    assert_png_snapshot(png, CHART_NAME)


def test_is_1080_px_wide_per_rendering_conventions() -> None:
    from app.charts.race_prediction import render_race_prediction_breakdown

    png = render_race_prediction_breakdown(_canonical_predictions())
    width, _height = png_dimensions(png)
    assert width == 1080


def test_empty_predictions_are_rejected_not_rendered() -> None:
    from app.charts.race_prediction import render_race_prediction_breakdown

    with pytest.raises(ValueError, match="no predictions"):
        render_race_prediction_breakdown(())


def test_lower_above_point_is_rejected_as_inverted_interval() -> None:
    from app.charts.race_prediction import render_race_prediction_breakdown

    bad = (
        RacePredictionSample(
            label="10k", point_seconds=2380.0, lower_seconds=2400.0, upper_seconds=2600.0
        ),
    )
    with pytest.raises(ValueError, match="inverted prediction interval"):
        render_race_prediction_breakdown(bad)


def test_point_above_upper_is_rejected_as_inverted_interval() -> None:
    from app.charts.race_prediction import render_race_prediction_breakdown

    bad = (
        RacePredictionSample(
            label="10k", point_seconds=2380.0, lower_seconds=2300.0, upper_seconds=2370.0
        ),
    )
    with pytest.raises(ValueError, match="inverted prediction interval"):
        render_race_prediction_breakdown(bad)


def test_non_positive_seconds_are_rejected_not_rendered() -> None:
    from app.charts.race_prediction import render_race_prediction_breakdown

    bad = (
        RacePredictionSample(
            label="10k", point_seconds=0.0, lower_seconds=0.0, upper_seconds=100.0
        ),
    )
    with pytest.raises(ValueError, match="positive"):
        render_race_prediction_breakdown(bad)


def test_empty_label_is_rejected_not_rendered() -> None:
    from app.charts.race_prediction import render_race_prediction_breakdown

    bad = (
        RacePredictionSample(
            label="  ", point_seconds=2380.0, lower_seconds=2300.0, upper_seconds=2500.0
        ),
    )
    with pytest.raises(ValueError, match="label"):
        render_race_prediction_breakdown(bad)


def test_ordering_is_by_predicted_time_not_input_order() -> None:
    from app.charts.race_prediction import render_race_prediction_breakdown

    canonical = _canonical_predictions()
    shuffled = (canonical[2], canonical[0], canonical[3], canonical[1])
    assert render_race_prediction_breakdown(shuffled) == render_race_prediction_breakdown(
        canonical
    )


def test_time_formatter_is_deterministic_h_mm() -> None:
    from app.charts.race_prediction import format_seconds_hmm

    assert format_seconds_hmm(7261.0) == "2:01"
    assert format_seconds_hmm(1140.0) == "0:19"
    assert format_seconds_hmm(10320.0) == "2:52"
