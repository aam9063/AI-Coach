"""Swim Critical Swim Speed and swim zones (ZON-7, PROJECT_BRIEF sections 7.3/12.4).

Formula (Wakayoshi et al. 1992, section 7.3):

    CSS (m/s) = (400 - 200) / (T400 - T200)

Hand-computed anchors (arithmetic shown):

(a) T400 = 360 s (6:00), T200 = 170 s (2:50):
        CSS = (400 - 200) / (360 - 170) = 200 / 190 = 1.052631... m/s
        pace per 100 m = 100 / CSS = 100 * 190 / 200 = 95.0 s = 1:35 /100 m

(b) T400 = 480 s (8:00), T200 = 240 s (4:00):
        CSS = (400 - 200) / (480 - 240) = 200 / 240 = 0.833333... m/s
        pace per 100 m = 100 / CSS = 100 * 240 / 200 = 120.0 s = 2:00 /100 m
        (exactly the CSS pace the owner has configured in Intervals.icu — a
        real-world consistency anchor)

Swim zones (ZON-7): the brief names swim zones relative to CSS but publishes
NO percentage table (unlike Coggan power / Friel HR). The implemented default
is an explicit, documented OWNER-REVIEWABLE choice — five bands around the
single literature-anchored point 100% CSS = Threshold (critical speed IS the
threshold intensity by the critical-speed model):

    Z1 Recovery  < 85 % CSS speed          Z4 Threshold  100-105 % CSS speed
    Z2 Aerobic   85-95 % CSS speed         Z5 VO2 max    >= 105 % CSS speed
    Z3 Tempo     95-100 % CSS speed

Zones are reported as pace per 100 m. DIRECTION NOTE: a FASTER pace (fewer
seconds per 100 m) is a HIGHER intensity — the opposite direction from the
power (watts) and heart-rate (bpm) tables. Boundary paces are computed as
``pace(pct) = 100 / (pct / 100 * CSS)``; under the same continuous-partition
gap rule as the power zones, every boundary pace joins the FASTER
(higher-intensity) zone adjoining it.
"""

from typing import cast

import pytest

from app.engine.zones import (
    DEFAULT_SWIM_ZONE_BOUNDARY_PCTS,
    css_from_time_trials,
    swim_zone_for,
    swim_zones,
)


def pace_at(pct: float, css_mps: float) -> float:
    """Pace per 100 m (s) at ``pct`` % of CSS speed — same formula as the module."""
    return 100.0 / (pct / 100.0 * css_mps)


CSS_A: float = 200.0 / 190.0  # anchor (a): T400 360 s, T200 170 s


class TestCssFormula:
    def test_anchor_a_six_minutes_and_two_fifty(self) -> None:
        result = css_from_time_trials(360.0, 170.0)
        # CSS = 200 / 190 = 1.052631... m/s
        assert result.css_mps == pytest.approx(1.0526315789473684)
        # pace per 100 m = 100 * 190 / 200 = 95.0 s exactly
        assert result.pace_sec_per_100m == pytest.approx(95.0)
        assert result.pace_per_100m == "1:35 /100 m"
        assert result.t400_s == 360.0
        assert result.t200_s == 170.0

    def test_anchor_b_intervals_icu_owner_anchor(self) -> None:
        result = css_from_time_trials(480.0, 240.0)
        # CSS = 200 / 240 = 0.833333... m/s
        assert result.css_mps == pytest.approx(0.8333333333333334)
        # pace per 100 m = 100 * 240 / 200 = 120.0 s exactly
        assert result.pace_sec_per_100m == pytest.approx(120.0)
        assert result.pace_per_100m == "2:00 /100 m"

    def test_result_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        result = css_from_time_trials(360.0, 170.0)
        with pytest.raises(FrozenInstanceError):
            result.css_mps = 1.0  # type: ignore[misc]

    def test_docstring_shows_the_anchor_arithmetic(self) -> None:
        doc = css_from_time_trials.__doc__ or ""
        assert "200 / 190" in doc
        assert "1:35" in doc

    @pytest.mark.parametrize(
        ("t400", "t200"),
        [
            (0.0, 170.0),  # T400 not positive
            (-360.0, 170.0),
            (360.0, 0.0),  # T200 not positive
            (360.0, -170.0),
            (360.0, 360.0),  # denominator T400 - T200 = 0
            (360.0, 380.0),  # denominator negative
        ],
    )
    def test_invalid_time_trials_raise(self, t400: float, t200: float) -> None:
        with pytest.raises(ValueError):
            css_from_time_trials(t400, t200)


class TestSwimZoneTable:
    def test_five_zones_with_owner_chosen_names(self) -> None:
        zones = swim_zones(CSS_A)
        assert [zone.key for zone in zones] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
        assert [zone.name for zone in zones] == [
            "Recovery",
            "Aerobic",
            "Tempo",
            "Threshold",
            "VO2 max",
        ]

    def test_default_boundaries_are_the_documented_owner_choice(self) -> None:
        assert DEFAULT_SWIM_ZONE_BOUNDARY_PCTS == (85.0, 95.0, 100.0, 105.0)
        zones = swim_zones(CSS_A)
        expected = (
            ("Z1", None, False, 85.0, False),
            ("Z2", 85.0, True, 95.0, False),
            ("Z3", 95.0, True, 100.0, False),
            ("Z4", 100.0, True, 105.0, False),
            ("Z5", 105.0, True, None, False),
        )
        for zone, (key, lo, lo_inc, hi, hi_inc) in zip(zones, expected, strict=True):
            assert zone.key == key
            assert zone.min_pct_css == lo
            assert zone.min_pct_inclusive is lo_inc
            assert zone.max_pct_css == hi
            assert zone.max_pct_inclusive is hi_inc

    def test_pace_bounds_for_anchor_a_css(self) -> None:
        # pace(pct) = 100 / (pct / 100 * CSS); the pace axis inverts the speed
        # axis: a zone's slower pace edge (larger seconds) mirrors its speed
        # minimum and its faster pace edge mirrors its speed maximum.
        zones = swim_zones(CSS_A)
        expected = (
            # (key, min_pace, min_inclusive, max_pace, max_inclusive)
            ("Z1", pace_at(85.0, CSS_A), False, None, False),
            ("Z2", pace_at(95.0, CSS_A), False, pace_at(85.0, CSS_A), True),
            ("Z3", pace_at(100.0, CSS_A), False, pace_at(95.0, CSS_A), True),
            ("Z4", pace_at(105.0, CSS_A), False, pace_at(100.0, CSS_A), True),
            ("Z5", None, False, pace_at(105.0, CSS_A), True),
        )
        for zone, (key, lo, lo_inc, hi, hi_inc) in zip(zones, expected, strict=True):
            assert zone.key == key
            assert zone.min_pace_sec_per_100m == (
                None if lo is None else pytest.approx(lo)
            )
            assert zone.min_pace_inclusive is lo_inc
            assert zone.max_pace_sec_per_100m == (
                None if hi is None else pytest.approx(hi)
            )
            assert zone.max_pace_inclusive is hi_inc

    def test_css_pace_itself_classifies_as_threshold(self) -> None:
        # CSS IS the threshold intensity (critical-speed model): swimming at
        # exactly the anchor-(a) CSS pace of 95.0 s /100 m must be Z4.
        result = css_from_time_trials(360.0, 170.0)
        assert result.pace_sec_per_100m == pytest.approx(95.0)
        assert swim_zone_for(result.pace_sec_per_100m, result.css_mps).key == "Z4"

    def test_zones_are_documented_as_owner_choice_with_direction_note(self) -> None:
        doc = swim_zones.__doc__ or ""
        assert "owner" in doc.lower()  # provenance split is documented
        assert "faster" in doc.lower()  # the pace-vs-intensity direction note


class TestSwimZoneClassifier:
    @pytest.mark.parametrize(
        ("pace", "expected_key"),
        [
            # Interior hand values (anchor-a CSS: CSS pace = 95.0 s /100 m).
            (115.0, "Z1"),  # ~83 % CSS speed — slower than the 85 % edge
            (105.0, "Z2"),  # ~91 % CSS speed
            (97.0, "Z3"),  # ~98 % CSS speed
            (92.0, "Z4"),  # ~103 % CSS speed
            (88.0, "Z5"),  # ~108 % CSS speed
            # Hand-exact boundary paces: each boundary joins the FASTER
            # (higher-intensity) zone under the continuous-partition rule.
            (100.0, "Z3"),  # exactly 95 % CSS speed (100 * 190 / 0.95 / 200)
            (95.0, "Z4"),  # exactly 100 % CSS speed = the CSS pace itself
        ],
    )
    def test_pace_classification(self, pace: float, expected_key: str) -> None:
        assert swim_zone_for(pace, CSS_A).key == expected_key

    @pytest.mark.parametrize(
        ("boundary_pct", "expected_key"),
        [
            (85.0, "Z2"),
            (95.0, "Z3"),
            (100.0, "Z4"),
            (105.0, "Z5"),
        ],
    )
    def test_table_boundaries_classify_consistently(
        self, boundary_pct: float, expected_key: str
    ) -> None:
        # The table's own pace boundaries (computed with the module's formula)
        # land in the higher-intensity zone adjoining them.
        boundary_pace = pace_at(boundary_pct, CSS_A)
        assert swim_zone_for(boundary_pace, CSS_A).key == expected_key

    def test_faster_pace_is_higher_intensity(self) -> None:
        keys = [zone.key for zone in swim_zones(CSS_A)]
        slower = swim_zone_for(105.0, CSS_A)
        faster = swim_zone_for(92.0, CSS_A)
        assert keys.index(faster.key) > keys.index(slower.key)

    def test_boundaries_are_configurable_per_call(self) -> None:
        custom = (80.0, 90.0, 100.0, 110.0)
        zones = swim_zones(CSS_A, boundary_pcts_css=custom)
        assert [zone.key for zone in zones] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
        expected_max_pct = (80.0, 90.0, 100.0, 110.0, None)
        for zone, max_pct in zip(zones, expected_max_pct, strict=True):
            assert zone.max_pct_css == max_pct
        # The classifier honours the same custom table: 93 % of CSS speed is
        # inside the custom Z3 (90-100) but the default Z2 (85-95).
        pace = pace_at(93.0, CSS_A)
        assert swim_zone_for(pace, CSS_A, boundary_pcts_css=custom).key == "Z3"
        assert swim_zone_for(pace, CSS_A).key == "Z2"


class TestSwimZoneValidation:
    def test_non_positive_css_raises_for_the_table(self) -> None:
        with pytest.raises(ValueError, match="css"):
            swim_zones(0.0)
        with pytest.raises(ValueError, match="css"):
            swim_zones(-1.0)

    def test_non_positive_css_or_pace_raises_for_the_classifier(self) -> None:
        with pytest.raises(ValueError, match="css"):
            swim_zone_for(95.0, 0.0)
        with pytest.raises(ValueError, match="pace"):
            swim_zone_for(0.0, CSS_A)
        with pytest.raises(ValueError, match="pace"):
            swim_zone_for(-95.0, CSS_A)

    @pytest.mark.parametrize(
        "boundaries",
        [
            (85.0, 95.0, 100.0),  # wrong length: four boundaries define five zones
            (85.0, 95.0, 95.0, 105.0),  # not strictly increasing
            (95.0, 85.0, 100.0, 105.0),  # not strictly increasing
            (0.0, 95.0, 100.0, 105.0),  # non-positive boundary
        ],
    )
    def test_invalid_boundary_tables_raise(
        self, boundaries: tuple[float, ...]
    ) -> None:
        # cast: invalid tables (wrong length) cannot be expressed in the
        # strict parameter type — the runtime ValueError is the contract.
        table = cast("tuple[float, float, float, float]", boundaries)
        with pytest.raises(ValueError, match="boundar"):
            swim_zones(CSS_A, boundary_pcts_css=table)
