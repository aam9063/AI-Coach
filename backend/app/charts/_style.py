"""Shared rendering style for chart renderers (PROJECT_BRIEF §10).

One place fixes the conventions every chart shares: 1080 px wide PNG,
consistent palette, dark text on a light background, units on axes, and
a date range in the title. Individual renderers only choose series and
labels; byte-level determinism (snapshot-tested) is anchored here:

- a fixed figure size and dpi (10.8 in x 100 dpi = 1080 px wide);
- matplotlib's bundled DejaVu Sans, so no platform font substitution;
- ``metadata={"Software": None}`` to keep matplotlib's version string
  (and any future timestamped metadata) out of the PNG bytes.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

from matplotlib.artist import Artist
from matplotlib.axes import Axes
from matplotlib.figure import Figure

# §10: 1080 px wide, readable on a phone. Height is proportionate but
# generous so legend + axis labels never crowd the plot.
FIGSIZE_INCHES = (10.8, 6.0)
DPI = 100
PNG_WIDTH_PX = FIGSIZE_INCHES[0] * DPI  # 1080

# Dark text on light background (§10). One accent color per concept,
# reused across charts: load (CTL/ATL) in blues, form (TSB) in orange.
BACKGROUND = "#ffffff"
TEXT = "#1a1a1a"
GRID = "#d9d9d9"
COLOR_CTL = "#1f5fbf"
COLOR_ATL = "#6aa9e9"
COLOR_TSB = "#e07b28"

TITLE_FONTSIZE = 16
LABEL_FONTSIZE = 12
TICK_FONTSIZE = 10
LEGEND_FONTSIZE = 11

# Categorical cycle for per-sport or per-series bands (weekly TSS, time in
# zone, ...). Stable assignment: a given label always maps to the same
# color, so stacks stay readable across charts and snapshots.
CATEGORICAL = (
    "#1f5fbf",
    "#e07b28",
    "#3a9e5f",
    "#8a5fbf",
    "#c23b5f",
    "#2aa0a8",
    "#b08800",
    "#5d6d7e",
)


def categorical_color(label: str) -> str:
    """Stable color for a categorical label (sport, zone, ...)."""
    index = sum(ord(ch) for ch in label) % len(CATEGORICAL)
    return CATEGORICAL[index]


def render_figure_to_png(fig: Figure) -> bytes:
    """Serialize a figure to PNG bytes under the §10 conventions."""
    buffer = io.BytesIO()
    fig.savefig(
        buffer,
        format="png",
        dpi=DPI,
        facecolor=BACKGROUND,
        metadata={"Software": None},
    )
    return buffer.getvalue()


def place_figure_legend(
    fig: Figure,
    ax: Axes,
    entries: int,
    handles: Sequence[Artist] | None = None,
    labels: Sequence[str] | None = None,
) -> None:
    """One frameless legend row in a bottom strip OUTSIDE the data area.

    Legends anchored inside the axes overlap the series (owner feedback
    on the received CH-11 e2e chart: the Performance Manager legend sat
    on top of the CTL/ATL lines). A figure-level row in a reserved bottom
    strip never collides with any series, whatever the data shape — and
    every chart places it identically, so the convention is predictable.

    ``tight_layout`` ignores outside-anchored legends, so the helper
    reserves the strip itself via ``tight_layout(rect=...)``. For twin
    axes (e.g. pace + HR) pass the merged ``handles``/``labels``
    explicitly; otherwise the helper reads them from ``ax``.
    """
    if handles is not None and labels is not None:
        legend_handles, legend_labels = list(handles), list(labels)
    else:
        legend_handles, legend_labels = ax.get_legend_handles_labels()
    if legend_handles:
        fig.legend(
            legend_handles,
            legend_labels,
            loc="lower center",
            ncols=max(1, min(entries, 4)),
            frameon=False,
            fontsize=LEGEND_FONTSIZE,
            handlelength=1.8,
            columnspacing=1.4,
        )
    fig.tight_layout(rect=(0.0, 0.055, 1.0, 1.0))
