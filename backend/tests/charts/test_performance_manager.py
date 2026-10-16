"""Snapshot tests for the Performance Manager chart (CH-1/CH-2, §10/§12.7).

The renderer is pure: ``PerformanceManagerSample`` sequence in, PNG bytes
out. Tests pin three things:

1. the rendered bytes match the committed snapshot baseline;
2. the PNG satisfies the §10 width convention (1080 px wide) — the
   baseline encodes it only implicitly, so it is asserted explicitly;
3. empty input is rejected loudly instead of rendering an empty chart.
"""

from __future__ import annotations

import pytest

from tests.charts.fixtures import PERFORMANCE_MANAGER_SAMPLES
from tests.charts.snapshot import assert_png_snapshot, png_dimensions

CHART_NAME = "performance_manager"


def test_matches_snapshot_baseline() -> None:
    from app.charts import render_performance_manager

    png = render_performance_manager(PERFORMANCE_MANAGER_SAMPLES)
    assert_png_snapshot(png, CHART_NAME)


def test_is_1080_px_wide_per_rendering_conventions() -> None:
    from app.charts import render_performance_manager

    png = render_performance_manager(PERFORMANCE_MANAGER_SAMPLES)
    width, _height = png_dimensions(png)
    assert width == 1080


def test_empty_samples_are_rejected_not_rendered() -> None:
    from app.charts import render_performance_manager

    with pytest.raises(ValueError, match="no samples"):
        render_performance_manager(())
