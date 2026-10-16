"""Mean-maximal power curve with critical-power fit overlay (§10, CH-4).

Pure renderer: best efforts plus a fitted 2-parameter CP model in, PNG
bytes out. No DB, no network, no clock inside — the title carries the
fit values themselves, so identical input yields identical bytes
(snapshot-tested in tests/charts/).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from matplotlib.figure import Figure

from app.charts._style import (
    BACKGROUND,
    COLOR_CTL,
    COLOR_TSB,
    GRID,
    LABEL_FONTSIZE,
    LEGEND_FONTSIZE,
    TEXT,
    TICK_FONTSIZE,
    TITLE_FONTSIZE,
    render_figure_to_png,
)


@dataclass(frozen=True)
class MeanMaximalEffort:
    """One best effort: mean power held for a given duration."""

    duration_s: float
    power_w: float


@dataclass(frozen=True)
class CriticalPowerFit:
    """The 2-parameter critical-power model: power(t) = CP + W' / t."""

    critical_power_w: float
    w_prime_j: float


def _validated(
    efforts: Sequence[MeanMaximalEffort], cp_fit: CriticalPowerFit
) -> list[MeanMaximalEffort]:
    if not efforts:
        raise ValueError(
            "no efforts to render: the mean-maximal power curve needs at least one effort"
        )
    if cp_fit.w_prime_j <= 0:
        raise ValueError(
            "w_prime must be positive: a CP fit without anaerobic capacity is not renderable"
        )
    for effort in efforts:
        if effort.duration_s <= 0:
            raise ValueError("duration must be positive: power at t=0 is undefined")
        if effort.power_w <= 0:
            raise ValueError("power must be positive: a non-positive best effort is not renderable")
    return sorted(efforts, key=lambda e: e.duration_s)


def render_mean_maximal_curve(
    efforts: Sequence[MeanMaximalEffort], cp_fit: CriticalPowerFit
) -> bytes:
    """Render best-effort points with the CP model curve overlaid (§10)."""
    ordered = _validated(efforts, cp_fit)

    fig = Figure(figsize=(10.8, 6.0), dpi=100, facecolor=BACKGROUND)
    ax = fig.add_subplot()

    # Log-scale duration axis: efforts typically span one minute to an
    # hour, and the hyperbolic CP curve is far more readable this way.
    durations = [e.duration_s for e in ordered]
    powers = [e.power_w for e in ordered]
    ax.scatter(durations, powers, color=COLOR_CTL, s=48, zorder=3, label="Best efforts")

    curve_t = np.logspace(np.log10(durations[0]), np.log10(durations[-1]), 200)
    curve_p = cp_fit.critical_power_w + cp_fit.w_prime_j / curve_t
    ax.plot(
        curve_t,
        curve_p,
        color=COLOR_TSB,
        linewidth=2.0,
        zorder=2,
        label="CP fit: CP + W'/t",
    )
    ax.set_xscale("log")

    cp_w = f"{cp_fit.critical_power_w:g}"
    w_prime_kj = cp_fit.w_prime_j / 1000
    ax.set_title(
        f"Mean-maximal power curve \u2014 CP {cp_w} W \u00b7 W' {w_prime_kj:.1f} kJ",
        fontsize=TITLE_FONTSIZE,
        color=TEXT,
        loc="left",
    )
    ax.set_xlabel("Duration (s, log scale)", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.set_ylabel("Power (W)", fontsize=LABEL_FONTSIZE, color=TEXT)
    ax.grid(True, which="both", color=GRID, linewidth=0.8, alpha=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=TEXT, labelsize=TICK_FONTSIZE)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.legend(fontsize=LEGEND_FONTSIZE, frameon=False, loc="upper right")
    fig.tight_layout()

    return render_figure_to_png(fig)
