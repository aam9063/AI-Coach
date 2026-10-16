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
