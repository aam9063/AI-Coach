"""HRV ln rMSSD chart with baseline band (§10, §7.4, CH-7).

Pure renderer: a sequence of :class:`HrvSample` in, PNG bytes out. No
DB, no network, no clock inside — the title shows the date range of the
data itself, so identical input yields identical bytes (snapshot-tested
in tests/charts/).

The baseline band is the readiness engine's rolling ln rMSSD baseline:
a shaded region between ``baseline_lower`` and ``baseline_upper`` behind
the daily points, so a reader can see at a glance whether today's
recovery sits inside or outside the athlete's normal range.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from matplotlib import dates as mdates
from matplotlib.figure import Figure

from app.charts._style import (
    BACKGROUND,
    CATEGORICAL,
    GRID,
    LABEL_FONTSIZE,
    LEGEND_FONTSIZE,
    TEXT,
    TICK_FONTSIZE,
    TITLE_FONTSIZE,
    render_figure_to_png,
)

# One fixed color for both the daily ln rMSSD line and its baseline band
# (same palette as the shared categorical cycle, drawn from _style).
COLOR_HRV = CATEGORICAL[2]


@dataclass(frozen=True)
class HrvSample:
    """One day of HRV: ln rMSSD plus the rolling baseline band.

    ``baseline_lower``/``baseline_upper`` come from the readiness
    engine's rolling baseline (§7.4); they are repeated per day so the
    chart input stays a flat, orderable sequence.
    """

    day: date
    ln_rmssd: float
    baseline_lower: float
    baseline_upper: float


def _validated(samples: Sequence[HrvSample]) -> list[HrvSample]:
    if not samples:
        raise ValueError("no samples to render: the HRV chart needs at least one day")
    for sample in samples:
        if sample.baseline_lower > sample.baseline_upper:
            raise ValueError(
                "inverted baseline band: baseline_lower must be <= baseline_upper"
            )
    return sorted(samples, key=lambda s: s.day)


def render_hrv_trend(samples: Sequence[HrvSample]) -> bytes:
    """Render daily ln rMSSD with its baseline band as a 1080 px PNG (§10)."""
    ordered = _validated(samples)

    days = [s.day for s in ordered]
    values = [s.ln_rmssd for s in ordered]
    lower = [s.baseline_lower for s in ordered]
    upper = [s.baseline_upper for s in ordered]

    fig = Figure(figsize=(10.8, 6.0), dpi=100, facecolor=BACKGROUND)
    ax = fig.add_subplot()

    # matplotlib's unit machinery accepts date objects at runtime, but its
    # type stubs do not; convert to matplotlib date floats explicitly and
    # format the axis deterministically (same convention as the
    # Performance Manager chart).
    x = mdates.date2num(days)  # type: ignore[no-untyped-call]  # matplotlib stubs are untyped
    locator = mdates.AutoDateLocator()  # type: ignore[no-untyped-call]
    ax.fill_between(
        x,
        lower,
        upper,
        color=COLOR_HRV,
        alpha=0.25,
        linewidth=0.0,
        label="Baseline band",
    )
    ax.plot(x, values, color=COLOR_HRV, linewidth=2.0, marker="o", markersize=3.0, label="ln rMSSD")
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))  # type: ignore[no-untyped-call]

    ax.set_title(
        f"HRV (ln rMSSD)  {days[0].isoformat()} \u2192 {days[-1].isoformat()}",
        fontsize=TITLE_FONTSIZE,
        color=TEXT,
        loc="left",
    )
    ax.set_xlabel("Date", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.set_ylabel("ln rMSSD", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.grid(axis="y", color=GRID, linewidth=0.8, alpha=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=TEXT, labelsize=TICK_FONTSIZE)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    fig.autofmt_xdate()
    ax.legend(fontsize=LEGEND_FONTSIZE, frameon=False, loc="upper left")
    fig.tight_layout()

    return render_figure_to_png(fig)
