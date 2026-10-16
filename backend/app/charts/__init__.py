"""Server-side chart renderers (PROJECT_BRIEF §10): data in, PNG bytes out.

Every renderer is a pure function — no DB, no network, no wall clock —
so identical input renders identical bytes, pinned by the snapshot
tests under tests/charts/. Conventions shared by all charts (1080 px
wide, palette, dark text on light background, units on axes, date in
the title) live in ``_style``.
"""

from app.charts.performance_manager import (
    PerformanceManagerSample,
    render_performance_manager,
)
from app.charts.weekly_tss import WeeklyTssSample, render_weekly_tss_by_sport

__all__ = [
    "PerformanceManagerSample",
    "WeeklyTssSample",
    "render_performance_manager",
    "render_weekly_tss_by_sport",
]
