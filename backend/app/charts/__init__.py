"""Server-side chart renderers (PROJECT_BRIEF §10): data in, PNG bytes out.

Every renderer is a pure function — no DB, no network, no wall clock —
so identical input renders identical bytes, pinned by the snapshot
tests under tests/charts/. Conventions shared by all charts (1080 px
wide, palette, dark text on light background, units on axes, date in
the title) live in ``_style``.
"""

from app.charts.hrv_trend import HrvSample, render_hrv_trend
from app.charts.mean_maximal_curve import (
    CriticalPowerFit,
    MeanMaximalEffort,
    render_mean_maximal_curve,
)
from app.charts.performance_manager import (
    PerformanceManagerSample,
    render_performance_manager,
)
from app.charts.race_prediction import (
    RacePredictionSample,
    render_race_prediction_breakdown,
)
from app.charts.run_trends import (
    EfSample,
    RunSessionSample,
    render_ef_trend,
    render_run_pace_hr_trend,
)
from app.charts.weekly_tss import WeeklyTssSample, render_weekly_tss_by_sport
from app.charts.weekly_zones import (
    WeeklyZoneSample,
    render_weekly_time_in_zone,
)

__all__ = [
    "CriticalPowerFit",
    "EfSample",
    "HrvSample",
    "MeanMaximalEffort",
    "PerformanceManagerSample",
    "RacePredictionSample",
    "RunSessionSample",
    "WeeklyTssSample",
    "WeeklyZoneSample",
    "render_ef_trend",
    "render_hrv_trend",
    "render_mean_maximal_curve",
    "render_performance_manager",
    "render_race_prediction_breakdown",
    "render_run_pace_hr_trend",
    "render_weekly_time_in_zone",
    "render_weekly_tss_by_sport",
]
