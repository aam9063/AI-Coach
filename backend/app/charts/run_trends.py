"""Run pace/HR trend and Efficiency Factor trend charts (§10, CH-5).

Pure renderers: a sequence of frozen dataclass samples in, PNG bytes
out. No DB, no network, no clock inside — the title shows the date
range of the data itself, so identical input yields identical bytes
(snapshot-tested in tests/charts/).

The pace/HR chart inverts the pace axis (lower s/km is faster, so
faster paces plot higher) and puts avg HR on a secondary right axis.
The EF chart adds a horizontal reference line at the mean EF.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from matplotlib import dates as mdates
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from app.charts._style import (
    BACKGROUND,
    GRID,
    LABEL_FONTSIZE,
    LEGEND_FONTSIZE,
    TEXT,
    TICK_FONTSIZE,
    TITLE_FONTSIZE,
    render_figure_to_png,
)

COLOR_PACE = "#1f5fbf"
COLOR_HR = "#e07b28"
COLOR_EF = "#1f5fbf"


@dataclass(frozen=True)
class RunSessionSample:
    """One run session: its day, pace in s/km, and average HR in bpm."""

    day: date
    pace_s_per_km: float
    avg_hr_bpm: float


@dataclass(frozen=True)
class EfSample:
    """One run session's Efficiency Factor (normalized pace or power per bpm)."""

    day: date
    ef: float


def _date_range_title(samples: Sequence[date]) -> str:
    return f"{samples[0].isoformat()} \u2192 {samples[-1].isoformat()}"


def _validated_run_samples(
    samples: Sequence[RunSessionSample],
) -> list[RunSessionSample]:
    if not samples:
        raise ValueError(
            "no samples to render: the run pace/HR trend chart needs at least one session"
        )
    for sample in samples:
        if sample.pace_s_per_km <= 0:
            raise ValueError("non-positive pace is not renderable: pace must be > 0 s/km")
        if sample.avg_hr_bpm <= 0:
            raise ValueError("non-positive HR is not renderable: avg HR must be > 0 bpm")
    return sorted(samples, key=lambda s: s.day)


def _validated_ef_samples(samples: Sequence[EfSample]) -> list[EfSample]:
    if not samples:
        raise ValueError(
            "no samples to render: the EF trend chart needs at least one session"
        )
    for sample in samples:
        if sample.ef <= 0:
            raise ValueError("non-positive EF is not renderable: EF must be > 0")
    return sorted(samples, key=lambda s: s.day)


def _style_date_axis(ax: Axes) -> None:
    locator = mdates.AutoDateLocator()  # type: ignore[no-untyped-call]  # matplotlib stubs are untyped
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))  # type: ignore[no-untyped-call]  # matplotlib stubs are untyped


def _apply_shared_style(fig: Figure, ax: Axes, title: str, xlabel: str) -> None:
    ax.set_title(title, fontsize=TITLE_FONTSIZE, color=TEXT, loc="left")
    ax.set_xlabel(xlabel, fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.grid(color=GRID, linewidth=0.8, alpha=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=TEXT, labelsize=TICK_FONTSIZE)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    fig.autofmt_xdate()
    fig.tight_layout()


def render_run_pace_hr_trend(samples: Sequence[RunSessionSample]) -> bytes:
    """Render run pace and avg HR over time as a 1080 px wide PNG (§10).

    Pace is plotted on the left y-axis inverted (faster = higher, since
    lower s/km is better); avg HR rides a secondary right y-axis.
    """
    ordered = _validated_run_samples(samples)

    days = [s.day for s in ordered]
    pace = [s.pace_s_per_km for s in ordered]
    hr = [s.avg_hr_bpm for s in ordered]

    fig = Figure(figsize=(10.8, 6.0), dpi=100, facecolor=BACKGROUND)
    ax = fig.add_subplot()

    # matplotlib's unit machinery accepts date objects at runtime, but its
    # type stubs do not; convert to matplotlib date floats explicitly and
    # format the axis deterministically (same convention as the
    # Performance Manager chart).
    x = mdates.date2num(days)  # type: ignore[no-untyped-call]  # matplotlib stubs are untyped
    _style_date_axis(ax)
    ax.plot(x, pace, color=COLOR_PACE, linewidth=2.0, label="Pace (s/km)")
    ax.set_ylabel("Pace (s/km, faster up)", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.invert_yaxis()

    hr_ax = ax.twinx()
    hr_ax.plot(x, hr, color=COLOR_HR, linewidth=2.0, label="Avg HR (bpm)")
    hr_ax.set_ylabel("Avg HR (bpm)", fontsize=LABEL_FONTSIZE, color=TEXT)
    hr_ax.tick_params(colors=TEXT, labelsize=TICK_FONTSIZE)
    for spine in hr_ax.spines.values():
        spine.set_color(GRID)

    lines, labels = ax.get_legend_handles_labels()
    hr_lines, hr_labels = hr_ax.get_legend_handles_labels()
    ax.legend(
        lines + hr_lines,
        labels + hr_labels,
        fontsize=LEGEND_FONTSIZE,
        frameon=False,
        loc="upper left",
    )

    _apply_shared_style(
        fig,
        ax,
        f"Run pace & HR trend  {_date_range_title(days)}",
        "Date",
    )
    return render_figure_to_png(fig)


def render_ef_trend(samples: Sequence[EfSample]) -> bytes:
    """Render Efficiency Factor over time as a 1080 px wide PNG (§10).

    A horizontal reference line marks the mean EF across the samples.
    """
    ordered = _validated_ef_samples(samples)

    days = [s.day for s in ordered]
    ef = [s.ef for s in ordered]
    mean_ef = sum(ef) / len(ef)

    fig = Figure(figsize=(10.8, 6.0), dpi=100, facecolor=BACKGROUND)
    ax = fig.add_subplot()

    x = mdates.date2num(days)  # type: ignore[no-untyped-call]  # matplotlib stubs are untyped
    _style_date_axis(ax)
    ax.plot(x, ef, color=COLOR_EF, linewidth=2.0, label="EF")
    ax.axhline(
        mean_ef,
        color=GRID,
        linewidth=1.2,
        linestyle="--",
        label=f"Mean EF ({mean_ef:.2f})",
    )

    ax.legend(fontsize=LEGEND_FONTSIZE, frameon=False, loc="upper left")
    _apply_shared_style(
        fig,
        ax,
        f"Efficiency Factor trend  {_date_range_title(days)}",
        "Date",
    )
    ax.set_ylabel(
        "EF (normalized pace or power per bpm)", fontsize=LABEL_FONTSIZE, color=TEXT
    )
    return render_figure_to_png(fig)
