"""Registry and fail-safe contract tests for the two delegating tools.

PROJECT_BRIEF §9.3 named a ten-tool registry; ``predict_race`` (Feature 8)
and ``plot_metric`` (Feature 7) are now IMPLEMENTED, so the registry
remains the complete ten-tool set and the fail-safe contracts that
survive are pinned here (the full happy paths live beside their
features: tests/agent/test_predict_race.py, test_plot_metric.py):

- both are registered in ``tool_list()`` (the explicit list, never a
  loader), and the registry is exactly the ten tools the brief names;
- error responses NEVER fabricate a prediction or a chart, and the
  unavailable kinds still answer with an explicit error naming what is
  missing (never ``insufficient_data`` for a capability gap, and never
  bare numbers).
"""

from __future__ import annotations

import pytest

from app.agent import tools

pytestmark = pytest.mark.anyio


class TestPredictRaceFailSafes:
    async def test_race_id_and_distance_are_mutually_exclusive(self) -> None:
        result = await tools.predict_race(race_id=1, distance="olympic")
        assert result["status"] == "error"
        assert result.get("segments") is None and result.get("total") is None
        assert result["detail"]

    async def test_unknown_race_key_is_an_error_not_a_guess(self) -> None:
        result = await tools.predict_race(distance="double_ironman")
        assert result["status"] == "error"
        # No fabricated numbers: no segment or total prediction anywhere.
        for forbidden in ("segments", "total"):
            assert result.get(forbidden) is None
        assert result["detail"]

    async def test_registered_in_the_explicit_tool_list(self) -> None:
        names = {
            t.__name__ if hasattr(t, "__name__") else t.tool_spec["name"]
            for t in tools.tool_list()
        }
        assert "predict_race" in names


class TestPlotMetricUnavailableKinds:
    async def test_unsupported_kind_errors_naming_the_missing_source(self) -> None:
        result = await tools.plot_metric(kind="mean_maximal_curve", date_range="30d")
        assert result["status"] == "error"
        assert "mean-maximal" in result["detail"].lower()
        assert result["status"] != "insufficient_data"

    async def test_never_fabricates_a_chart_in_error_responses(self) -> None:
        result = await tools.plot_metric(kind="race_prediction", date_range="90d", sport="ride")
        assert result["status"] == "error"
        # The typed error model may carry null chart fields; the invariant is
        # that no real chart identity is ever fabricated on an error path.
        assert result.get("chart_id") is None
        assert result.get("media_url") is None
        assert result["detail"]

    async def test_registered_in_the_explicit_tool_list(self) -> None:
        names = {
            t.__name__ if hasattr(t, "__name__") else t.tool_spec["name"]
            for t in tools.tool_list()
        }
        assert "plot_metric" in names


class TestRegistryIsTheCompleteBriefSet:
    def test_tool_list_is_the_ten_tools_the_brief_names(self) -> None:
        listed = tools.tool_list()
        names = {
            t.__name__ if hasattr(t, "__name__") else t.tool_spec["name"]
            for t in listed
        }
        assert names == {
            "get_load_status", "get_zones", "get_readiness",
            "get_activity_analysis", "get_intensity_distribution",
            "predict_race", "plot_metric", "search_evidence",
            "log_subjective", "propose_threshold_update",
        }
        assert len(listed) == 10
