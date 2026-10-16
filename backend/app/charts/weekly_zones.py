"""Weekly time-in-zone (3-zone) chart (§10, §7.5, CH-6).

Pure renderer: weekly per-sport minutes in zones 1/2/3 in, PNG bytes
out. No DB, no network, no clock inside — the title shows the week
range of the data, so identical input yields identical bytes
(snapshot-tested).

Zone color approach: fixed module constants, taken once from the shared
categorical palette. A color encodes the ZONE (not the sport), and the
constants are resolved a single time here so zone 1/2/3 render the same
color in every week, every stack order, and every snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from matplotlib import dates as mdates
from matplotlib.figure import Figure

from app.charts._style import (
    BACKGROUND,
    GRID,
    LABEL_FONTSIZE,
    TEXT,
    TICK_FONTSIZE,
    TITLE_FONTSIZE,
    categorical_color,
    place_figure_legend,
    render_figure_to_png,
)

# Fixed zone colors: zone 1 bottom, zone 2 middle, zone 3 top, with the
# same color in every week (§10 consistent palette).
COLOR_ZONE1 = categorical_color("zone1")
COLOR_ZONE2 = categorical_color("zone2")
COLOR_ZONE3 = categorical_color("zone3")

ZONE_COLORS = (COLOR_ZONE1, COLOR_ZONE2, COLOR_ZONE3)
ZONE_LABELS = ("Zone 1", "Zone 2", "Zone 3")


@dataclass(frozen=True)
class WeeklyZoneSample:
    """One ISO week's time in zone, split per sport.

    ``sport_zones`` maps each sport to exactly three minute totals —
    (zone 1, zone 2, zone 3) in that position order, which fixes the
    stacking order (zone 1 at the bottom of the bar). The sport order
    inside the tuple does not affect the drawing: segments are drawn in
    alphabetical sport order per zone band, so a sport present in only
    some weeks still lands in the same band position.
    """

    week_start: date
    sport_zones: tuple[tuple[str, tuple[float, float, float]], ...]


def _validated(samples: Sequence[WeeklyZoneSample]) -> list[WeeklyZoneSample]:
    if not samples:
        raise ValueError("no samples to render: the time-in-zone chart needs at least one week")
    for sample in samples:
        for _sport, zones in sample.sport_zones:
            if len(zones) != 3:
                raise ValueError(
                    "each sport's zone tuple must have exactly 3 values (zone 1, zone 2, zone 3)"
                )
            for minutes in zones:
                if minutes < 0:
                    raise ValueError(
                        "negative minutes are not renderable: zone minutes must be >= 0"
                    )
    return sorted(samples, key=lambda s: s.week_start)


def render_weekly_time_in_zone(samples: Sequence[WeeklyZoneSample]) -> bytes:
    """Render weekly time in zone (3 zones) as a 1080 px wide PNG (§10)."""
    ordered = _validated(samples)

    sports = sorted({sport for sample in ordered for sport, _zones in sample.sport_zones})
    weeks = [sample.week_start for sample in ordered]
    # per_sport[sport][zone_index] -> minutes, one entry per week
    # (missing sport in a week = zeros, same convention as weekly TSS).
    per_sport: dict[str, list[list[float]]] = {
        sport: [[0.0] * len(ordered) for _ in range(3)] for sport in sports
    }
    for week_index, sample in enumerate(ordered):
        by_sport = dict(sample.sport_zones)
        for sport in sports:
            zones = by_sport.get(sport, (0.0, 0.0, 0.0))
            for zone_index, minutes in enumerate(zones):
                per_sport[sport][zone_index][week_index] = minutes

    fig = Figure(figsize=(10.8, 6.0), dpi=100, facecolor=BACKGROUND)
    ax = fig.add_subplot()

    # matplotlib's unit machinery accepts date objects at runtime, but its
    # type stubs do not; convert to matplotlib date floats explicitly and
    # format the axis deterministically (same convention as the weekly TSS
    # and Performance Manager charts).
    x = mdates.date2num(weeks)  # type: ignore[no-untyped-call]  # matplotlib stubs are untyped
    locator = mdates.AutoDateLocator()  # type: ignore[no-untyped-call]
    bottom = [0.0] * len(ordered)
    for zone_index, (label, color) in enumerate(zip(ZONE_LABELS, ZONE_COLORS, strict=True)):
        for sport_index, sport in enumerate(sports):
            values = per_sport[sport][zone_index]
            # The legend entry is per zone (the color encodes the zone),
            # so only the first sport's segment carries the label.
            ax.bar(
                x,
                values,
                bottom=bottom,
                width=5.0,
                color=color,
                label=label if sport_index == 0 else None,
            )
            bottom = [b + v for b, v in zip(bottom, values, strict=True)]
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))  # type: ignore[no-untyped-call]

    ax.set_title(
        f"Time in zone per week  {weeks[0].isoformat()} \u2192 {weeks[-1].isoformat()}",
        fontsize=TITLE_FONTSIZE,
        color=TEXT,
        loc="left",
    )
    ax.set_xlabel("Week starting", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.set_ylabel("Time in zone (min)", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.grid(axis="y", color=GRID, linewidth=0.8, alpha=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=TEXT, labelsize=TICK_FONTSIZE)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    place_figure_legend(fig, ax, entries=len(ZONE_LABELS))

    return render_figure_to_png(fig)
