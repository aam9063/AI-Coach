"""Race prediction breakdown chart with uncertainty bars (§10, §7.7, CH-8).

Pure renderer: race predictions in, PNG bytes out. No DB, no network,
no clock inside, so identical input yields identical bytes
(snapshot-tested in tests/charts/).

The race-prediction engine (Feature 8) is not implemented yet; this
renderer is written against the contracted output shape: each
prediction carries a point estimate and an uncertainty interval
``{point, lower, upper}``. There is deliberately no date axis here —
the x axis is predicted time, formatted as ``h:mm`` by a fixed,
deterministic helper.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from app.charts._style import (
    BACKGROUND,
    COLOR_CTL,
    GRID,
    LABEL_FONTSIZE,
    TEXT,
    TICK_FONTSIZE,
    TITLE_FONTSIZE,
    place_figure_legend,
    render_figure_to_png,
)


@dataclass(frozen=True)
class RacePredictionSample:
    """One predicted finish time with an uncertainty interval (§7.7).

    ``point_seconds`` is the predicted time for the distance in
    ``label`` (e.g. ``"10k"``, ``"Half"``); ``lower_seconds`` and
    ``upper_seconds`` bound the uncertainty interval, which must
    satisfy ``lower <= point <= upper`` with positive times.
    """

    label: str
    point_seconds: float
    lower_seconds: float
    upper_seconds: float


def format_seconds_hmm(seconds: float) -> str:
    """Format seconds deterministically as ``h:mm`` (e.g. 7261 -> "2:01")."""
    total_minutes = round(seconds / 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}:{minutes:02d}"


def _validated(
    predictions: Sequence[RacePredictionSample],
) -> list[RacePredictionSample]:
    if not predictions:
        raise ValueError(
            "no predictions to render: the race prediction chart needs at least one distance"
        )
    for prediction in predictions:
        if not prediction.label.strip():
            raise ValueError(
                "empty label is not renderable: every prediction needs a distance name"
            )
        for value in (
            prediction.point_seconds,
            prediction.lower_seconds,
            prediction.upper_seconds,
        ):
            if value <= 0:
                raise ValueError(
                    "non-positive seconds is not renderable: predicted times must be > 0"
                )
        if (
            prediction.lower_seconds > prediction.point_seconds
            or prediction.point_seconds > prediction.upper_seconds
        ):
            raise ValueError(
                "inverted prediction interval is not renderable: need lower <= point <= upper"
            )
    # Fastest predicted time first, whatever order the caller used.
    return sorted(predictions, key=lambda p: p.point_seconds)


def render_race_prediction_breakdown(predictions: Sequence[RacePredictionSample]) -> bytes:
    """Render predicted finish times with uncertainty bars as a 1080 px PNG (§10)."""
    ordered = _validated(predictions)

    labels = [p.label for p in ordered]
    points = [p.point_seconds for p in ordered]
    lower = [p.lower_seconds for p in ordered]
    upper = [p.upper_seconds for p in ordered]

    fig = Figure(figsize=(10.8, 6.0), dpi=100, facecolor=BACKGROUND)
    ax = fig.add_subplot()

    # Horizontal bars, one per distance, fastest at the top. Error bars
    # span [lower, upper]; the darker marker sits on the point estimate.
    y = list(range(len(ordered)))
    ax.barh(y, points, height=0.55, color=COLOR_CTL, label="Point estimate")
    below = [p - lo for p, lo in zip(points, lower, strict=True)]
    above = [up - p for p, up in zip(points, upper, strict=True)]
    ax.errorbar(
        points,
        y,
        xerr=[below, above],
        fmt="o",
        color=TEXT,
        ecolor=TEXT,
        elinewidth=1.4,
        capsize=4,
    )
    ax.invert_yaxis()
    ax.set_yticks(y)
    ax.set_yticklabels(labels)

    ax.set_title(
        "Race predictions  (point estimate \u00b1 uncertainty)",
        fontsize=TITLE_FONTSIZE,
        color=TEXT,
        loc="left",
    )
    ax.set_xlabel("Predicted time (h:mm)", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.xaxis.set_major_formatter(
        FuncFormatter(lambda value, _pos: format_seconds_hmm(value)),
    )
    ax.grid(axis="x", color=GRID, linewidth=0.8, alpha=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=TEXT, labelsize=TICK_FONTSIZE)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    place_figure_legend(fig, ax, entries=1)

    return render_figure_to_png(fig)
