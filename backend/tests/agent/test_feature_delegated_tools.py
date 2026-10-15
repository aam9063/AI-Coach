"""WA-6 closing tests: the two registry tools that delegate to unbuilt
features — ``predict_race`` (Feature 8) and ``plot_metric`` (Feature 7).

PROJECT_BRIEF §9.3 explicitly allows the registry to stub-delegate until
those features land, so the registry is the COMPLETE ten-tool set the
brief names. Contract pinned here:

- Both are registered in ``tool_list()`` (the explicit list, never a
  loader).
- Both return ``status="error"`` whose detail says the capability is not
  implemented yet AND which feature brings it.
- They NEVER fabricate a prediction or a chart, and NEVER return
  ``insufficient_data`` — that status means "missing data", a different
  statement from "the capability does not exist yet".
"""

from __future__ import annotations

import pytest

from app.agent import tools

pytestmark = pytest.mark.anyio


class TestPredictRaceStub:
    async def test_returns_error_naming_feature_8(self) -> None:
        result = await tools.predict_race(race_id=1)
        assert result["status"] == "error"
        assert "not implemented" in result["detail"].lower()
        assert "Feature 8" in result["detail"]
        # Never insufficient_data: nothing is missing, the capability
        # simply does not exist yet.
        assert result["status"] != "insufficient_data"

    async def test_never_fabricates_a_prediction(self) -> None:
        result = await tools.predict_race(distance=21.1, target_date="2026-12-06")
        assert result["status"] == "error"
        # No fabricated numbers: no time, pace or split anywhere.
        for forbidden in ("prediction", "finish_time", "segments", "pace"):
            assert forbidden not in result
        assert result["detail"]

    async def test_registered_in_the_explicit_tool_list(self) -> None:
        names = {
            t.__name__ if hasattr(t, "__name__") else t.tool_spec["name"]
            for t in tools.tool_list()
        }
        assert "predict_race" in names


class TestPlotMetricStub:
    async def test_returns_error_naming_feature_7(self) -> None:
        result = await tools.plot_metric(kind="load", date_range="30d")
        assert result["status"] == "error"
        assert "not implemented" in result["detail"].lower()
        assert "Feature 7" in result["detail"]
        assert result["status"] != "insufficient_data"

    async def test_never_fabricates_a_chart(self) -> None:
        result = await tools.plot_metric(kind="ctl_atl", date_range="90d", sport="ride")
        assert result["status"] == "error"
        for forbidden in ("chart_id", "media_url", "image"):
            assert forbidden not in result
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
