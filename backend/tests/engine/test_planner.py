"""Ordered decision table of the one-day session planner (T1, specs S4/S5).

Contract under test (``app.engine.planner.plan_session`` / ``PlanResult``):

- The decision table is ORDERED and the first matching row wins; the order
  itself is part of the contract, so it gets its own tests:
    1. readiness suggests reducing intensity -> recovery (Z1-Z2, capped at
       60 min), BEFORE any TSB-based row.
    2. TSB below the engine's overreached band (reusing
       ``app.engine.readiness.DEFAULT_TSB_VERY_NEGATIVE``) -> rest, no
       zones, no duration.
    3. TSB neutral/positive AND readiness ok -> quality in zone Z3.
    4. A dominant sport share above 0.70 refines the sport of the session
       rows 1-3 chose: the OTHER sport is preferred, unless the athlete
       named a sport explicitly (the explicit request always wins).
    5. Missing readiness or TSB, or unconfigured thresholds, is an explicit
       ``insufficient_data`` naming exactly what is missing (S5) — and it
       is evaluated BEFORE rows 1-4 can produce any session.
- Zones are stable KEYS ("Z1".."Z5"), never numeric targets: the numeric
  min/max values belong to the tool layer (T3), which attaches them from
  the athlete's configured zones. A structural test pins that the result
  type carries no numeric-target field at all.
- ``reason`` is a factual engine sentence quoting the numbers it used, so
  the model can quote it verbatim and the numeric-faithfulness gate (S4)
  can check the reply against the tool output.
- The result is a frozen dataclass (engine convention) and inputs are
  validated (``ValueError``, never silent clamping), mirroring the other
  pure engine modules.
"""

import dataclasses
import re

import pytest

from app.engine.planner import (
    RECOVERY_MAX_DURATION_S,
    SPORT_SHARE_THRESHOLD,
    PlanResult,
    plan_session,
)


def _plan(
    *,
    readiness_suggests_reduce_intensity: bool | None = False,
    tsb: float | None = 5.0,
    thresholds_configured: bool = True,
    sport_share: float | None = None,
    requested_sport: str | None = None,
) -> PlanResult:
    """Call ``plan_session`` with healthy defaults and named overrides.

    Defaults describe a ready-to-train athlete (readiness ok, TSB +5.0,
    thresholds configured, no sport-imbalance signal, no explicit request)
    so each test names only the condition it exercises.
    """
    return plan_session(
        readiness_suggests_reduce_intensity=readiness_suggests_reduce_intensity,
        tsb=tsb,
        thresholds_configured=thresholds_configured,
        sport_share=sport_share,
        requested_sport=requested_sport,
    )


# ---------------------------------------------------------------------------
# One test per decision-table row (the table order, first match wins)
# ---------------------------------------------------------------------------


def test_rule1_reduce_intensity_gives_capped_recovery() -> None:
    result = _plan(readiness_suggests_reduce_intensity=True, tsb=-2.0)
    assert result.status == "ok"
    assert result.kind == "recovery"
    assert result.session_type == "recovery ride"
    assert result.zones == ("Z1", "Z2")
    assert result.duration_s == RECOVERY_MAX_DURATION_S == 3600
    # The reason quotes the cap it applied (numeric faithfulness, S4).
    assert "3600" in result.reason


def test_rule2_overreached_tsb_gives_rest_without_zones() -> None:
    result = _plan(tsb=-18.4)
    assert result.status == "ok"
    assert result.kind == "rest"
    assert result.session_type == "rest or mobility"
    assert result.zones is None
    assert result.duration_s is None
    # The reason quotes both the observed TSB and the band threshold.
    assert "-18.4" in result.reason
    assert "-10.0" in result.reason


def test_rule2_tsb_exactly_at_the_band_edge_is_not_rest() -> None:
    # Strict comparison, mirroring readiness's exact-at-threshold rule: TSB
    # at exactly -10.0 is NOT below the band, so the table falls through.
    result = _plan(tsb=-10.0)
    assert result.kind == "quality"


def test_rule3_neutral_tsb_and_ok_readiness_give_quality() -> None:
    result = _plan(tsb=5.2)
    assert result.status == "ok"
    assert result.kind == "quality"
    assert result.zones == ("Z3",)
    assert result.duration_s is None
    assert "+5.2" in result.reason


def test_rule4_dominant_sport_share_prefers_the_other_sport() -> None:
    result = _plan(tsb=5.2, sport_share=0.85)
    assert result.status == "ok"
    # The planner never invents a sport name (S2): with no explicit request
    # it cannot name "the other sport" (the activity rows live in the
    # caller), so it reports no sport and states the imbalance with numbers.
    assert result.sport is None
    assert "0.85" in result.reason
    assert "0.70" in result.reason
    assert "other sport" in result.reason


def test_rule4_share_exactly_at_the_threshold_does_not_fire() -> None:
    result = _plan(tsb=5.2, sport_share=SPORT_SHARE_THRESHOLD)
    assert "other sport" not in result.reason


def test_requested_sport_overrides_the_imbalance_rule() -> None:
    result = _plan(tsb=5.2, sport_share=0.90, requested_sport="run")
    assert result.status == "ok"
    assert result.sport == "run"
    assert "other sport" not in result.reason


def test_rule5_missing_tsb_is_insufficient_data() -> None:
    result = _plan(tsb=None)
    assert result.status == "insufficient_data"
    assert result.sport is None
    assert result.session_type is None
    assert result.zones is None
    assert result.duration_s is None
    assert result.kind is None
    assert len(result.missing) == 1
    assert "TSB" in result.missing[0]


def test_rule5_missing_readiness_is_insufficient_data() -> None:
    result = _plan(readiness_suggests_reduce_intensity=None)
    assert result.status == "insufficient_data"
    assert len(result.missing) == 1
    assert "readiness" in result.missing[0].lower()


def test_rule5_unconfigured_thresholds_name_the_two_thresholds() -> None:
    result = _plan(thresholds_configured=False)
    assert result.status == "insufficient_data"
    joined = " ".join(result.missing)
    # Exactly the two intensity anchors the zones (Feature 3) need.
    assert "athlete_ftp_w" in joined
    assert "athlete_lthr_bpm" in joined


def test_rule5_every_missing_input_is_named_at_once() -> None:
    """Both GATING inputs are named in one answer, not one at a time.

    Thresholds are no longer part of this gate: they gate only the rows that name
    zones (see the rest/zone tests), so an athlete missing readiness AND
    thresholds learns about readiness first instead of a wall of three.
    """
    result = plan_session(
        readiness_suggests_reduce_intensity=None,
        tsb=None,
        thresholds_configured=False,
        sport_share=None,
        requested_sport=None,
    )
    assert result.status == "insufficient_data"
    assert len(result.missing) == 2
    assert "athlete_ftp_w" not in " ".join(result.missing)


# ---------------------------------------------------------------------------
# Ordering of the table (part of the contract, each with its own test)
# ---------------------------------------------------------------------------


def test_ordering_rule1_beats_rule3_when_both_hold() -> None:
    # A positive TSB alone would pick quality (rule 3), but a
    # reduce-intensity verdict must win first (rule 1).
    result = _plan(readiness_suggests_reduce_intensity=True, tsb=10.0)
    assert result.kind == "recovery"


def test_ordering_missing_input_beats_the_reduce_intensity_row() -> None:
    # Even a reduce-intensity verdict cannot produce a session while the
    # day's TSB is unknown: rule 5 is evaluated before rows 1-4.
    result = _plan(readiness_suggests_reduce_intensity=True, tsb=None)
    assert result.status == "insufficient_data"
    assert result.kind is None


# ---------------------------------------------------------------------------
# Zones are keys, never numeric targets (the tool layer attaches numbers)
# ---------------------------------------------------------------------------


def test_zones_are_stable_keys_never_numeric_targets() -> None:
    ok_results = [
        _plan(readiness_suggests_reduce_intensity=True, tsb=-2.0),
        _plan(tsb=5.2),
        _plan(tsb=5.2, sport_share=0.85),
        _plan(tsb=5.2, requested_sport="run"),
    ]
    for result in ok_results:
        assert result.zones is not None
        for zone in result.zones:
            assert re.fullmatch(r"Z[1-5]", zone), zone
    # Structural: the result type has no field that could carry numeric
    # zone targets (min/max pairs belong to the tool layer, T3).
    field_names = set(PlanResult.__dataclass_fields__)
    assert not {"zone_targets", "zone_min", "zone_max"} & field_names


# ---------------------------------------------------------------------------
# Result-shape and input-validation conventions (engine-wide)
# ---------------------------------------------------------------------------


def test_result_is_a_frozen_dataclass() -> None:
    result = _plan()
    assert dataclasses.is_dataclass(result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.kind = "rest"  # type: ignore[misc]


def test_non_finite_or_out_of_range_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        _plan(tsb=float("nan"))
    with pytest.raises(ValueError):
        _plan(sport_share=1.5)
    with pytest.raises(ValueError):
        _plan(sport_share=float("inf"))


def test_rest_does_not_require_thresholds() -> None:
    """A rest prescription expresses no intensity.

    Requiring FTP/LTHR to tell an overreached athlete to rest would invert the
    product: the engine would answer "set your FTP" to someone who must not train.
    """
    result = plan_session(
        readiness_suggests_reduce_intensity=False,
        tsb=-25.0,
        thresholds_configured=False,
        sport_share=None,
        requested_sport=None,
    )
    assert result.status == "ok"
    assert result.kind == "rest"


def test_a_zone_bearing_session_still_requires_thresholds() -> None:
    """The rule still holds where it matters: no zones without thresholds."""
    result = plan_session(
        readiness_suggests_reduce_intensity=True,
        tsb=-2.0,
        thresholds_configured=False,
        sport_share=None,
        requested_sport=None,
    )
    assert result.status == "insufficient_data"
    assert result.missing
    assert "thresh" in result.missing[0].lower()
