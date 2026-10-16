"""Performance Manager chart: CTL, ATL, TSB over time (§10, CH-2).

Pure renderer: a sequence of :class:`PerformanceManagerSample` in, PNG
bytes out. No DB, no network, no clock inside — the title shows the
date range of the data itself, so identical input yields identical
bytes (snapshot-tested in tests/charts/).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from matplotlib import dates as mdates
from matplotlib.figure import Figure

from app.charts._style import (
    BACKGROUND,
    COLOR_ATL,
    COLOR_CTL,
    COLOR_TSB,
    GRID,
    LABEL_FONTSIZE,
    TEXT,
    TICK_FONTSIZE,
    TITLE_FONTSIZE,
    place_figure_legend,
    render_figure_to_png,
)


@dataclass(frozen=True)
class PerformanceManagerSample:
    """One day of the Performance Manager: fitness, fatigue, form."""

    day: date
    ctl: float
    atl: float
    tsb: float


def _validated_samples(
    samples: Sequence[PerformanceManagerSample],
) -> list[PerformanceManagerSample]:
    if not samples:
        raise ValueError(
            "no samples to render: the performance manager chart needs at least one day"
        )
    return sorted(samples, key=lambda s: s.day)


def render_performance_manager(samples: Sequence[PerformanceManagerSample]) -> bytes:
    """Render CTL/ATL/TSB over time as a 1080 px wide PNG (§10)."""
    ordered = _validated_samples(samples)

    days = [s.day for s in ordered]
    ctl = [s.ctl for s in ordered]
    atl = [s.atl for s in ordered]
    tsb = [s.tsb for s in ordered]

    fig = Figure(figsize=(10.8, 6.0), dpi=100, facecolor=BACKGROUND)
    ax = fig.add_subplot()

    # matplotlib's unit machinery accepts date objects at runtime, but its
    # type stubs do not; convert to matplotlib date floats explicitly and
    # format the axis deterministically.
    x = mdates.date2num(days)  # type: ignore[no-untyped-call]  # matplotlib stubs are untyped
    locator = mdates.AutoDateLocator()  # type: ignore[no-untyped-call]
    ax.plot(x, ctl, color=COLOR_CTL, linewidth=2.0, label="CTL (fitness)")
    ax.plot(x, atl, color=COLOR_ATL, linewidth=2.0, label="ATL (fatigue)")
    ax.plot(x, tsb, color=COLOR_TSB, linewidth=2.0, label="TSB (form)")
    ax.axhline(0, color=GRID, linewidth=1.0)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))  # type: ignore[no-untyped-call]

    ax.set_title(
        f"Performance Manager  {days[0].isoformat()} \u2192 {days[-1].isoformat()}",
        fontsize=TITLE_FONTSIZE,
        color=TEXT,
        loc="left",
    )
    ax.set_xlabel("Date", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.set_ylabel("Score (CTL/ATL/TSB)", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.grid(color=GRID, linewidth=0.8, alpha=0.6)
    ax.tick_params(colors=TEXT, labelsize=TICK_FONTSIZE)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    fig.autofmt_xdate()
    place_figure_legend(fig, ax, entries=3)

    return render_figure_to_png(fig)
