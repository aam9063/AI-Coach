"""Activity-level plausibility filtering (data quality, pure engine).

Established on the owner's REAL ingested history (857 activities, 2020-2026):
9 of the owner's 539 runs with distance+duration carry impossible average
paces (e.g. 5.00 km in 10:36 = 2:07/km; 15.20 km in 34:36 = 2:16/km — both
well beyond the fastest average speeds a human can sustain). Such records
poison every naive downstream fit, so they are rejected at the ACTIVITY
level, with a machine-readable reason, BEFORE any stream or threshold work.

The band bounds are documented, owner-reviewable constants
(:data:`app.engine.quality.DEFAULT_PLAUSIBILITY_BANDS_MPS`), never invented
silently — see that module's docstring for the full provenance of every
bound (running average-pace band, cycling average-speed band, swim band).

Boundary convention: the project's strict one — EXACTLY at a band limit
counts as beyond it (same convention as ``app.engine.intensity``'s pause
rule), so plausibility requires ``floor < avg < ceiling`` strictly.
"""

from __future__ import annotations

import pytest

from app.engine.quality import (
    DEFAULT_PLAUSIBILITY_BANDS_MPS,
    ActivityPlausibility,
    assess_activity_plausibility,
    plausibility_band_key,
)

# The owner's named corrupt records (parent's measurements, 2026-10):
# 5.00 km in 10:36 -> 7.87 m/s (2:07/km); 15.20 km in 34:36 -> 7.32 m/s
# (2:16/km). Both are far above any humanly possible run average pace.
OWNER_CORRUPT_RECORDS = [
    # (distance_m, duration_s, expected avg m/s)
    (5000.0, 636, 7.8616),
    (15200.0, 2076, 7.3218),
]


class TestOwnerCorruptRecords:
    """The records that poisoned the naive fits are rejected, with reasons."""

    @pytest.mark.parametrize(("distance_m", "duration_s", "avg_mps"), OWNER_CORRUPT_RECORDS)
    def test_owner_corrupt_runs_rejected_above_ceiling(
        self, distance_m: float, duration_s: int, avg_mps: float
    ) -> None:
        result = assess_activity_plausibility("run", distance_m, duration_s)

        assert result.outcome == "implausible"
        assert result.reason == "average_speed_above_ceiling"
        assert result.average_speed_mps == pytest.approx(avg_mps, rel=1e-3)
        assert "ceiling" in result.detail

    def test_owner_corrupt_record_is_rejected_via_its_sport_family(self) -> None:
        """A stored ``VirtualRun`` type hits the same run band (no drift)."""
        assert plausibility_band_key("VirtualRun") == "run"
        result = assess_activity_plausibility("VirtualRun", 5000.0, 636)

        assert result.outcome == "implausible"
        assert result.reason == "average_speed_above_ceiling"


class TestOrdinaryRuns:
    """Ordinary owner-like runs pass untouched."""

    def test_easy_run_passes(self) -> None:
        # 5 km in 30:00 = 2.78 m/s (9:00/km) — inside the owner's easy range.
        result = assess_activity_plausibility("run", 5000.0, 1800)

        assert result.outcome == "plausible"
        assert result.reason is None
        assert result.average_speed_mps == pytest.approx(5000.0 / 1800.0)

    def test_threshold_like_run_passes(self) -> None:
        # 10 km in 45:00 = 3.70 m/s (4:30/km) — fast but humanly possible.
        result = assess_activity_plausibility("run", 10000.0, 2700)

        assert result.outcome == "plausible"

    def test_stored_lowercase_type_passes(self) -> None:
        # The owner's DB stores lower-case types (live-verified: 'run').
        result = assess_activity_plausibility("run", 5000.0, 1800)

        assert result.outcome == "plausible"


class TestWalkLikePace:
    """A walk-like average pace is rejected FOR THE RUN SPORT."""

    def test_walk_pace_rejected_for_run(self) -> None:
        # 5 km in 60:00 = 1.39 m/s (12:00/km) — walking, not running.
        result = assess_activity_plausibility("run", 5000.0, 3600)

        assert result.outcome == "implausible"
        assert result.reason == "average_speed_below_floor"

    def test_walk_sport_itself_is_not_assessable(self) -> None:
        """The walk SPORT has no band: never judged with the run band."""
        assert plausibility_band_key("walk") is None
        result = assess_activity_plausibility("walk", 5000.0, 3600)

        assert result.outcome == "not_assessable"
        assert result.reason == "no_plausibility_band_for_sport"

    def test_strength_sport_is_not_assessable(self) -> None:
        result = assess_activity_plausibility("weighttraining", None, 3600)

        assert result.outcome == "not_assessable"


class TestBoundaryConvention:
    """Boundary cases follow the project's strict convention: EXACTLY at a
    limit counts as beyond it (plausibility needs floor < avg < ceiling)."""

    def test_exactly_at_ceiling_is_implausible(self) -> None:
        # 6.5 m/s exactly: 6500 m in 1000 s.
        result = assess_activity_plausibility("run", 6500.0, 1000)

        assert result.outcome == "implausible"
        assert result.reason == "average_speed_above_ceiling"

    def test_exactly_at_floor_is_implausible(self) -> None:
        # 1.5 m/s exactly: 1500 m in 1000 s.
        result = assess_activity_plausibility("run", 1500.0, 1000)

        assert result.outcome == "implausible"
        assert result.reason == "average_speed_below_floor"

    def test_just_inside_the_band_is_plausible(self) -> None:
        # 6.499 m/s (6500 m in 1000.15... s) and 1.501 m/s both pass.
        just_below_ceiling = assess_activity_plausibility("run", 6499.0, 1000)
        just_above_floor = assess_activity_plausibility("run", 1501.0, 1000)

        assert just_below_ceiling.outcome == "plausible"
        assert just_above_floor.outcome == "plausible"


class TestMissingAndCorruptData:
    """Missing / nonpositive summary fields are reported, never guessed."""

    def test_missing_distance_is_not_assessable(self) -> None:
        result = assess_activity_plausibility("run", None, 1800)

        assert result.outcome == "not_assessable"
        assert result.reason == "missing_distance_or_duration"

    def test_missing_duration_is_not_assessable(self) -> None:
        result = assess_activity_plausibility("run", 5000.0, None)

        assert result.outcome == "not_assessable"
        assert result.reason == "missing_distance_or_duration"

    def test_nonpositive_duration_is_reported_implausible(self) -> None:
        result = assess_activity_plausibility("run", 5000.0, 0)

        assert result.outcome == "implausible"
        assert result.reason == "nonpositive_distance_or_duration"

    def test_nonpositive_distance_is_reported_implausible(self) -> None:
        result = assess_activity_plausibility("run", -1.0, 1800)

        assert result.outcome == "implausible"
        assert result.reason == "nonpositive_distance_or_duration"


class TestPerSportBands:
    """The ride and swim bands behave per their documented bounds."""

    def test_ride_at_normal_speed_passes(self) -> None:
        result = assess_activity_plausibility("ride", 40000.0, 5400)  # 7.4 m/s

        assert result.outcome == "plausible"

    def test_ride_above_ceiling_rejected(self) -> None:
        # 60 km in 30 min = 20 m/s (72 km/h) — impossible average.
        result = assess_activity_plausibility("ride", 60000.0, 1800)

        assert result.outcome == "implausible"
        assert result.reason == "average_speed_above_ceiling"

    def test_ride_below_floor_rejected(self) -> None:
        # 1000 m in 3000 s = 0.33 m/s (3.6 km/h) — the bike barely moved.
        result = assess_activity_plausibility("ride", 1000.0, 3000)

        assert result.outcome == "implausible"
        assert result.reason == "average_speed_below_floor"

    def test_swim_above_ceiling_rejected(self) -> None:
        # 1500 m in 600 s = 2.5 m/s — strictly above the swim band ceiling
        # of 2.3 m/s (peak human sprint swim speed); an activity AVERAGE of
        # 2.5 m/s is impossible. (Exactly-at-a-limit would also be rejected:
        # the band comparison follows the strict convention.)
        result = assess_activity_plausibility("swim", 1500.0, 600)

        assert result.outcome == "implausible"
        assert result.reason == "average_speed_above_ceiling"

    def test_swim_normal_pace_passes(self) -> None:
        result = assess_activity_plausibility("swim", 1500.0, 1800)  # 0.83 m/s

        assert result.outcome == "plausible"


class TestConfigurableBands:
    """The bands are explicit owner-reviewable parameters, not buried."""

    def test_bands_constant_documents_run_ride_swim(self) -> None:
        assert set(DEFAULT_PLAUSIBILITY_BANDS_MPS) == {"run", "ride", "swim"}
        assert DEFAULT_PLAUSIBILITY_BANDS_MPS["run"] == (1.5, 6.5)

    def test_custom_band_overrides_the_default(self) -> None:
        custom = {"run": (2.0, 4.0)}
        inside_custom = assess_activity_plausibility(
            "run", 5000.0, 1800, bands=custom  # 2.78 m/s: inside (2.0, 4.0)
        )

        assert inside_custom.outcome == "plausible"

        also_inside = assess_activity_plausibility(
            "run", 10000.0, 2700, bands=custom  # 3.70 m/s: inside (2.0, 4.0)
        )
        assert also_inside.outcome == "plausible"

        clearly_out = assess_activity_plausibility(
            "run", 5000.0, 600, bands=custom  # 8.33 m/s > 4.0
        )
        assert clearly_out.outcome == "implausible"

    def test_unknown_sport_without_band_is_not_assessable(self) -> None:
        result = assess_activity_plausibility("elliptical", 5000.0, 1800)

        assert result.outcome == "not_assessable"
        assert result.reason == "no_plausibility_band_for_sport"


def test_result_type_is_frozen() -> None:
    """Result types follow the engine convention: frozen dataclasses."""
    result = assess_activity_plausibility("run", 5000.0, 1800)

    assert isinstance(result, ActivityPlausibility)
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is attr-dependent
        result.outcome = "implausible"  # type: ignore[misc]
