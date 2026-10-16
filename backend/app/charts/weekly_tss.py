"""Weekly TSS stacked by sport chart (§10, CH-3).

Pure renderer: weekly per-sport TSS totals in, PNG bytes out. No DB, no
network, no clock inside — the title shows the week range of the data,
so identical input yields identical bytes (snapshot-tested).
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
    LEGEND_FONTSIZE,
    TEXT,
    TICK_FONTSIZE,
    TITLE_FONTSIZE,
    categorical_color,
    render_figure_to_png,
)


@dataclass(frozen=True)
class WeeklyTssSample:
    """One ISO week's total training load, split per sport.

    ``sport_tss`` is an ordered (sport, tss) sequence. The stacking order
    across weeks is deterministic (alphabetical by sport name), so weeks
    may list sports in any order, and a sport present in only some weeks
    still gets one consistent band color.
    """

    week_start: date
    sport_tss: tuple[tuple[str, float], ...]


def _validated(samples: Sequence[WeeklyTssSample]) -> list[WeeklyTssSample]:
    if not samples:
        raise ValueError("no samples to render: the weekly TSS chart needs at least one week")
    for sample in samples:
        for _sport, tss in sample.sport_tss:
            if tss < 0:
                raise ValueError("negative TSS is not renderable: totals must be >= 0")
    return sorted(samples, key=lambda s: s.week_start)


def render_weekly_tss_by_sport(samples: Sequence[WeeklyTssSample]) -> bytes:
    """Render weekly TSS stacked by sport as a 1080 px wide PNG (§10)."""
    ordered = _validated(samples)

    sports = sorted({sport for sample in ordered for sport, _tss in sample.sport_tss})
    weeks = [sample.week_start for sample in ordered]
    per_sport: dict[str, list[float]] = {sport: [] for sport in sports}
    for sample in ordered:
        totals = dict(sample.sport_tss)
        for sport in sports:
            per_sport[sport].append(totals.get(sport, 0.0))

    fig = Figure(figsize=(10.8, 6.0), dpi=100, facecolor=BACKGROUND)
    ax = fig.add_subplot()

    # matplotlib's unit machinery accepts date objects at runtime, but its
    # type stubs do not; convert to matplotlib date floats explicitly and
    # format the axis deterministically (same convention as the
    # Performance Manager chart).
    x = mdates.date2num(weeks)  # type: ignore[no-untyped-call]  # matplotlib stubs are untyped
    locator = mdates.AutoDateLocator()  # type: ignore[no-untyped-call]
    bottom = [0.0] * len(ordered)
    for sport in sports:
        values = per_sport[sport]
        ax.bar(x, values, bottom=bottom, width=5.0, color=categorical_color(sport), label=sport)
        bottom = [b + v for b, v in zip(bottom, values, strict=True)]
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))  # type: ignore[no-untyped-call]

    ax.set_title(
        f"Weekly TSS by sport  {weeks[0].isoformat()} \u2192 {weeks[-1].isoformat()}",
        fontsize=TITLE_FONTSIZE,
        color=TEXT,
        loc="left",
    )
    ax.set_xlabel("Week starting", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.set_ylabel("TSS (training load)", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.grid(axis="y", color=GRID, linewidth=0.8, alpha=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=TEXT, labelsize=TICK_FONTSIZE)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.legend(fontsize=LEGEND_FONTSIZE, frameon=False, loc="upper left")
    fig.tight_layout()

    return render_figure_to_png(fig)
