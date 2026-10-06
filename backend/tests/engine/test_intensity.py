"""3-zone intensity distribution (RID-5/6/7, PROJECT_BRIEF section 7.5).

The 3-zone model maps Feature 4's published 5/7-zone tables (Coggan power,
Friel HR per sport, swim CSS bands) onto the polarized-literature 3-zone
construct (Seiler 2010):

    Z1 below the first threshold, Z2 between the thresholds,
    Z3 above the second threshold.

The mapping rule applied uniformly to every source table (documented in
``app.engine.intensity``):

    first threshold  = TOP OF SOURCE Z2 (last fully aerobic zone)
    second threshold = TOP OF SOURCE Z4 (top of the published Threshold zone)

which yields, per modality:

    bike_power (% FTP) : t1 = 76.0  (top of Coggan Z2),  t2 = 106.0 (top of Z4)
    run_hr     (% LTHR): t1 = 90.0  (top of Friel Z2),   t2 = 100.0 (top of Z4)
    bike_hr    (% LTHR): t1 = 90.0  (top of Friel Z2),   t2 = 100.0 (top of Z4)
    swim_pace  (% CSS) : t1 = 95.0  (top of Aerobic),    t2 = 100.0 (CSS itself)

so the source-to-3-zone mapping is:

    bike_power : Z1->Z1, Z2->Z1, Z3->Z2, Z4->Z2, Z5->Z3, Z6->Z3, Z7->Z3
    run_hr     : Z1->Z1, Z2->Z1, Z3->Z2, Z4->Z2, Z5a->Z3, Z5b->Z3, Z5c->Z3
    bike_hr    : same as run_hr
    swim_pace  : Z1->Z1, Z2->Z1, Z3->Z2, Z4->Z3, Z5->Z3
                 (pace axis INVERTS: above the second threshold = FASTER
                 than CSS, i.e. swim source Z4/Z5)

Weekly aggregation (RID-5) uses ISO 8601 weeks (Monday start) with
whole-session attribution to the ISO week of the session date; weeks with
no data are reported as "no_data" and never as a fabricated 0% split.

The pattern comparison (RID-7) is DESCRIPTIVE only: it states how the
observed split relates to the polarized (~80/20, Seiler 2010) and
pyramidal (Stöggl & Sperlich 2014) reference bands — never advice, never
moralising — and refuses to label low-volume weeks.
"""

import dataclasses
import datetime as dt
import math

import pytest

from app.engine.intensity import (
    DEFAULT_MIN_PATTERN_WEEK_SECONDS,
    DEFAULT_POLARIZED_BANDS,
    DEFAULT_PYRAMIDAL_BANDS,
    INTENSITY_MODALITY_KEYS,
    INTENSITY_ZONE_KEYS,
    SPORT_KEYS,
    IntensityModalityKey,
    ThreeZoneModel,
    WeeklyIntensity,
    WeeklySportZones,
    descriptive_pattern_comparison,
    has_usable_samples,
    iso_week_start,
    map_to_three_zones,
    select_intensity_modality,
    three_zone_model,
    weekly_time_in_zone,
    zone_session,
)

# The owner's real anchors of record (same as the zones tests).
FTP_W: float = 180.0  # manual FTP 180 W (ZON real-data limitation)
LTHR: float = 169.0
CSS: float = 200.0 / 190.0  # CSS from T400=360 s, T200=170 s (ZON-7 anchor A)


# ---------------------------------------------------------------------------
# RID-6: the 3-zone model and its documented mapping
# ---------------------------------------------------------------------------


class TestThreeZoneModelMapping:
    def test_every_source_zone_maps_for_each_modality(self) -> None:
        expected: dict[IntensityModalityKey, tuple[tuple[str, str], ...]] = {
            "bike_power": (
                ("Z1", "Z1"), ("Z2", "Z1"), ("Z3", "Z2"), ("Z4", "Z2"),
                ("Z5", "Z3"), ("Z6", "Z3"), ("Z7", "Z3"),
            ),
            "run_hr": (
                ("Z1", "Z1"), ("Z2", "Z1"), ("Z3", "Z2"), ("Z4", "Z2"),
                ("Z5a", "Z3"), ("Z5b", "Z3"), ("Z5c", "Z3"),
            ),
            "bike_hr": (
                ("Z1", "Z1"), ("Z2", "Z1"), ("Z3", "Z2"), ("Z4", "Z2"),
                ("Z5a", "Z3"), ("Z5b", "Z3"), ("Z5c", "Z3"),
            ),
            "swim_pace": (
                ("Z1", "Z1"), ("Z2", "Z1"), ("Z3", "Z2"), ("Z4", "Z3"),
                ("Z5", "Z3"),
            ),
        }
        for modality in INTENSITY_MODALITY_KEYS:
            model = three_zone_model(modality)
            assert model.mapping == expected[modality], modality

    def test_thresholds_match_the_documented_cut_points(self) -> None:
        expected: dict[IntensityModalityKey, tuple[float, float]] = {
            "bike_power": (76.0, 106.0),
            "run_hr": (90.0, 100.0),
            "bike_hr": (90.0, 100.0),
            "swim_pace": (95.0, 100.0),
        }
        for modality, (t1, t2) in expected.items():
            model = three_zone_model(modality)
            assert model.first_threshold_pct == t1, modality
            assert model.second_threshold_pct == t2, modality

    def test_provenance_split_is_explicit_and_owner_reviewable(self) -> None:
        # LITERATURE where a published cut point exists; OWNER CHOICE where
        # the brief publishes none. The tags must say which is which.
        expected: dict[IntensityModalityKey, tuple[str, str]] = {
            "bike_power": ("owner_choice", "literature"),
            "run_hr": ("owner_choice", "literature"),
            "bike_hr": ("owner_choice", "literature"),
            "swim_pace": ("owner_choice", "literature"),
        }
        for modality, (p1, p2) in expected.items():
            model = three_zone_model(modality)
            assert model.first_threshold_provenance == p1, modality
            assert model.second_threshold_provenance == p2, modality
            # Every threshold carries a citation so the owner can review it.
            assert model.detail, modality

    def test_exact_boundary_keys_map_to_the_expected_side(self) -> None:
        # The LAST source-zone key of each band must land on the correct
        # side of each threshold.
        cases: dict[IntensityModalityKey, tuple[tuple[str, str], ...]] = {
            "bike_power": (("Z2", "Z1"), ("Z4", "Z2"), ("Z7", "Z3")),
            "run_hr": (("Z2", "Z1"), ("Z4", "Z2"), ("Z5c", "Z3")),
            "bike_hr": (("Z2", "Z1"), ("Z4", "Z2"), ("Z5c", "Z3")),
            "swim_pace": (("Z2", "Z1"), ("Z3", "Z2"), ("Z5", "Z3")),
        }
        for modality, boundaries in cases.items():
            model = three_zone_model(modality)
            for source_key, want in boundaries:
                assert model.zone_for(source_key) == want, (modality, source_key)

    def test_swim_direction_note_is_part_of_the_model(self) -> None:
        # The pace axis inverts: above the second threshold = faster than
        # CSS, so swim Z4/Z5 (>= 100% CSS) map to 3-zone Z3.
        model = three_zone_model("swim_pace")
        assert model.zone_for("Z4") == "Z3"
        assert "faster" in model.detail.lower() or "invert" in model.detail.lower()

    def test_unknown_source_zone_key_raises_never_silently_dropped(self) -> None:
        model = three_zone_model("bike_power")
        with pytest.raises(ValueError, match="Z5c"):
            model.zone_for("Z5c")

    def test_unknown_modality_raises(self) -> None:
        with pytest.raises(ValueError, match="modality"):
            three_zone_model("rowing")  # type: ignore[arg-type]


class TestThreeZoneMappingIsConfigurable:
    def test_overriding_thresholds_recomputes_the_mapping(self) -> None:
        # Move the first threshold to the top of Coggan Z3 (91.0 % FTP):
        # source Z3 must move from 3-zone Z2 into 3-zone Z1.
        model = three_zone_model("bike_power", first_threshold_pct=91.0)
        assert model.first_threshold_pct == 91.0
        assert model.zone_for("Z3") == "Z1"
        assert model.zone_for("Z4") == "Z2"
        # The published Z2/Z3 boundary is gone: Z2 still Z1, Z5 still Z3.
        assert model.zone_for("Z2") == "Z1"
        assert model.zone_for("Z5") == "Z3"

    def test_overriding_the_second_threshold(self) -> None:
        # Move the second threshold to the top of Coggan Z5 (121.0 % FTP):
        # source Z5 must move from 3-zone Z3 into 3-zone Z2.
        model = three_zone_model("bike_power", second_threshold_pct=121.0)
        assert model.zone_for("Z4") == "Z2"
        assert model.zone_for("Z5") == "Z2"
        assert model.zone_for("Z6") == "Z3"

    def test_threshold_inside_a_source_zone_raises(self) -> None:
        # 80.0 % FTP falls INSIDE Coggan Z3 (76-91): a threshold that does
        # not sit on a source-zone boundary would straddle a zone, so it
        # must raise instead of splitting zone seconds.
        with pytest.raises(ValueError, match="boundary"):
            three_zone_model("bike_power", first_threshold_pct=80.0)

    def test_inverted_or_equal_thresholds_raise(self) -> None:
        with pytest.raises(ValueError):
            three_zone_model("run_hr", first_threshold_pct=100.0,
                             second_threshold_pct=90.0)
        with pytest.raises(ValueError):
            three_zone_model("run_hr", first_threshold_pct=100.0,
                             second_threshold_pct=100.0)


# ---------------------------------------------------------------------------
# RID-6: the three zones partition time exactly
# ---------------------------------------------------------------------------


class TestExactPartition:
    def test_mapping_partitions_source_seconds_exactly_per_modality(self) -> None:
        # Distinct, arithmetic-provable second counts for every source
        # zone; the 3-zone totals must equal the exact sums of the mapped
        # source bands, and z1 + z2 + z3 must equal the grand total (no
        # lost or double-counted seconds).
        cases: dict[IntensityModalityKey, tuple[float, ...]] = {
            "bike_power": (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0),
            "run_hr": (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0),
            "bike_hr": (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0),
            "swim_pace": (10.0, 20.0, 30.0, 40.0, 50.0),
        }
        for modality, counts in cases.items():
            model = three_zone_model(modality)
            keys = model.source_zone_keys
            source = dict(zip(keys, counts, strict=True))
            got = map_to_three_zones(model, source)
            expected_z1 = math.fsum(
                s for k, s in source.items() if model.zone_for(k) == "Z1"
            )
            expected_z2 = math.fsum(
                s for k, s in source.items() if model.zone_for(k) == "Z2"
            )
            expected_z3 = math.fsum(
                s for k, s in source.items() if model.zone_for(k) == "Z3"
            )
            assert (got.z1_seconds, got.z2_seconds, got.z3_seconds) == (
                expected_z1, expected_z2, expected_z3,
            ), modality
            assert got.total_seconds == math.fsum(counts), modality
            assert got.z1_seconds + got.z2_seconds + got.z3_seconds == (
                got.total_seconds
            ), modality

    def test_empty_mapping_gives_all_zero_seconds(self) -> None:
        model = three_zone_model("run_hr")
        got = map_to_three_zones(model, {})
        assert (got.z1_seconds, got.z2_seconds, got.z3_seconds) == (0.0, 0.0, 0.0)
        assert got.total_seconds == 0.0

    def test_negative_seconds_raise(self) -> None:
        model = three_zone_model("bike_power")
        with pytest.raises(ValueError, match="negative"):
            map_to_three_zones(model, {"Z2": -1.0})

    def test_non_finite_seconds_raise(self) -> None:
        model = three_zone_model("bike_power")
        with pytest.raises(ValueError, match="finite"):
            map_to_three_zones(model, {"Z2": math.inf})
        with pytest.raises(ValueError, match="finite"):
            map_to_three_zones(model, {"Z2": math.nan})


# ---------------------------------------------------------------------------
# RID-5: weekly time-in-zone aggregation per sport
# ---------------------------------------------------------------------------

# Concrete ISO weeks around the owner-real anchors:
#   2026-01-05 Mon (ISO week 2), 2026-01-11 Sun (week 2),
#   2026-01-12 Mon (week 3),     2026-01-19 Mon (week 4).
W2_MON = dt.date(2026, 1, 5)
W2_SUN = dt.date(2026, 1, 11)
W4_MON = dt.date(2026, 1, 19)


class TestWeeklyAggregation:
    def test_week_boundary_is_iso_monday_start(self) -> None:
        # Sunday 2026-01-11 closes ISO week 2; Monday 2026-01-12 opens
        # week 3. Two sessions one day apart must land in DIFFERENT weeks.
        sessions = [
            zone_session(W2_SUN, "bike", {"Z1": 100.0}),
            zone_session(W2_MON, "bike", {"Z3": 100.0}),
        ]
        weeks = weekly_time_in_zone(sessions, sports=["bike"])
        assert len(weeks) == 1  # both are in ISO week 2
        assert weeks[0].iso_year == 2026 and weeks[0].iso_week == 2
        assert weeks[0].week_start == dt.date(2026, 1, 5)
        bike = weeks[0].sports[0]
        assert bike.total_seconds == 200.0

    def test_iso_week_start_returns_the_monday(self) -> None:
        assert iso_week_start(2026, 2) == dt.date(2026, 1, 5)
        with pytest.raises(ValueError):
            iso_week_start(2026, 60)

    def test_multi_week_sessions_are_attributed_wholly_to_their_start_week(
        self,
    ) -> None:
        # A 5-hour "long ride" is stored as ONE session dated on its start
        # date; the documented rule attributes ALL of its seconds to the
        # ISO week of that date — never split across weeks.
        long_ride = zone_session(W2_SUN, "bike", {"Z1": 9000.0, "Z2": 6000.0,
                                                  "Z3": 3000.0})
        weeks = weekly_time_in_zone([long_ride], sports=["bike"])
        assert len(weeks) == 1
        bike = weeks[0].sports[0]
        assert bike.total_seconds == 18000.0
        # 3-zone Z1 = source Z1 + Z2 = 15000; Z2 = source Z3 = 3000.
        assert bike.z1_seconds == 15000.0
        assert bike.z2_seconds == 3000.0

    def test_three_zone_breakdown_and_total_per_sport_per_week(self) -> None:
        sessions = [
            zone_session(W2_MON, "bike", {"Z1": 1200.0, "Z2": 400.0,
                                          "Z3": 300.0, "Z4": 100.0}),
            zone_session(W2_SUN, "run", {"Z1": 500.0, "Z5a": 700.0}),
            zone_session(W4_MON, "swim", {"Z2": 800.0, "Z4": 250.0}),
        ]
        weeks = weekly_time_in_zone(sessions)
        assert [w.iso_week for w in weeks] == [2, 3, 4]

        w2 = weeks[0]
        by_sport = {s.sport: s for s in w2.sports}
        # bike: source Z1+Z2 -> 3z Z1 = 1600; Z3+Z4 -> 3z Z2 = 400; Z3 = 0.
        assert by_sport["bike"].z1_seconds == 1600.0
        assert by_sport["bike"].z2_seconds == 400.0
        assert by_sport["bike"].z3_seconds == 0.0
        assert by_sport["bike"].total_seconds == 2000.0
        assert by_sport["bike"].status == "data"
        # run: source Z1 -> 3z Z1 = 500; Z5a -> 3z Z3 = 700.
        assert by_sport["run"].z1_seconds == 500.0
        assert by_sport["run"].z3_seconds == 700.0
        assert by_sport["run"].total_seconds == 1200.0

        w4 = weeks[2]
        swim = w4.sports[2]
        assert swim.sport == "swim"
        assert swim.z1_seconds == 800.0  # swim Z2 -> 3-zone Z1
        assert swim.z3_seconds == 250.0  # swim Z4 -> 3-zone Z3
        assert swim.total_seconds == 1050.0

    def test_interior_no_data_week_reports_no_data_never_fabricated_zero(
        self,
    ) -> None:
        sessions = [
            zone_session(W2_MON, "bike", {"Z1": 100.0}),
            zone_session(W4_MON, "bike", {"Z3": 100.0}),
        ]
        weeks = weekly_time_in_zone(sessions, sports=["bike"])
        assert [w.iso_week for w in weeks] == [2, 3, 4]
        gap = weeks[1]
        assert gap.iso_week == 3
        bike = gap.sports[0]
        assert bike.status == "no_data"
        assert bike.percentages is None  # never a fabricated 0% split
        assert bike.total_seconds == 0.0
        # A data week DOES carry exact percentages.
        assert weeks[0].sports[0].percentages == (100.0, 0.0, 0.0)

    def test_sport_without_sessions_is_no_data_in_a_data_week(self) -> None:
        sessions = [zone_session(W2_MON, "bike", {"Z1": 100.0})]
        weeks = weekly_time_in_zone(sessions)
        run = {s.sport: s for s in weeks[0].sports}["run"]
        assert run.status == "no_data"
        assert run.percentages is None

    def test_no_seconds_lost_or_double_counted_across_weeks_and_sports(
        self,
    ) -> None:
        sessions = [
            zone_session(W2_MON, "bike", {"Z1": 100.0, "Z4": 50.0}),
            zone_session(W2_SUN, "run", {"Z2": 30.0, "Z5b": 20.0}),
            zone_session(W4_MON, "swim", {"Z3": 40.0}),
            zone_session(W4_MON, "bike", {"Z7": 10.0}),
        ]
        weeks = weekly_time_in_zone(sessions)
        reported = math.fsum(
            s.total_seconds for w in weeks for s in w.sports
        )
        expected = math.fsum(s.total_seconds for s in sessions)
        assert reported == expected
        for week in weeks:
            for sport in week.sports:
                assert (
                    sport.z1_seconds + sport.z2_seconds + sport.z3_seconds
                    == sport.total_seconds
                )

    def test_unknown_sport_in_filter_raises(self) -> None:
        with pytest.raises(ValueError, match="sport"):
            weekly_time_in_zone([], sports=["row"])  # type: ignore[list-item]

    def test_unknown_sport_in_session_raises(self) -> None:
        with pytest.raises(ValueError, match="sport"):
            zone_session(W2_MON, "row", {"Z1": 10.0})  # type: ignore[arg-type]

    def test_unknown_zone_key_for_the_sport_raises(self) -> None:
        # Run sessions use the Friel run table: a Coggan power key is not
        # a valid run zone key and must raise, never be dropped.
        with pytest.raises(ValueError, match="zone"):
            zone_session(W2_MON, "run", {"Z6": 10.0})
        with pytest.raises(ValueError, match="zone"):
            zone_session(W2_MON, "swim", {"Z5a": 10.0})

    def test_negative_and_non_finite_session_seconds_raise(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            zone_session(W2_MON, "bike", {"Z2": -5.0})
        with pytest.raises(ValueError, match="finite"):
            zone_session(W2_MON, "bike", {"Z2": math.nan})
        with pytest.raises(ValueError, match="finite"):
            zone_session(W2_MON, "bike", {"Z2": math.inf})

    def test_empty_session_list_raises(self) -> None:
        with pytest.raises(ValueError, match="session"):
            weekly_time_in_zone([])


# ---------------------------------------------------------------------------
# RID-7: descriptive comparison vs reference patterns
# ---------------------------------------------------------------------------

POLARIZED_TOTAL = 36000.0  # 10 h
MIN_VOLUME = DEFAULT_MIN_PATTERN_WEEK_SECONDS


class TestDescriptivePatternComparison:
    def test_polarized_week_is_labelled_polarized(self) -> None:
        # 80 / 5 / 15 — the classic polarized split (Seiler 2010 ~80/20).
        result = descriptive_pattern_comparison(
            z1_seconds=0.80 * POLARIZED_TOTAL,
            z2_seconds=0.05 * POLARIZED_TOTAL,
            z3_seconds=0.15 * POLARIZED_TOTAL,
        )
        assert result.label == "polarized"
        assert result.polarized_within_bands is True
        assert result.pyramidal_within_bands is False
        assert (result.z1_pct, result.z2_pct, result.z3_pct) == (80.0, 5.0, 15.0)

    def test_pyramidal_week_is_labelled_pyramidal(self) -> None:
        # ~65 / 25 / 10 — the pyramidal shape (Stöggl & Sperlich 2014).
        result = descriptive_pattern_comparison(
            z1_seconds=0.65 * POLARIZED_TOTAL,
            z2_seconds=0.25 * POLARIZED_TOTAL,
            z3_seconds=0.10 * POLARIZED_TOTAL,
        )
        assert result.label == "pyramidal"
        assert result.pyramidal_within_bands is True
        assert result.polarized_within_bands is False

    def test_neither_pattern_is_labelled_mixed(self) -> None:
        # 50 / 30 / 20: too much Z2/Z3 for polarized, too much Z3 for
        # pyramidal — a descriptive "mixed", never a judgement.
        result = descriptive_pattern_comparison(
            z1_seconds=0.50 * POLARIZED_TOTAL,
            z2_seconds=0.30 * POLARIZED_TOTAL,
            z3_seconds=0.20 * POLARIZED_TOTAL,
        )
        assert result.label == "mixed"
        assert result.polarized_within_bands is False
        assert result.pyramidal_within_bands is False

    def test_reference_bands_are_documented_owner_reviewable_constants(
        self,
    ) -> None:
        for bands in (DEFAULT_POLARIZED_BANDS, DEFAULT_PYRAMIDAL_BANDS):
            assert len(bands) == 3
            for lo, hi in bands:
                assert 0.0 <= lo <= hi <= 100.0

    def test_custom_bands_are_accepted_and_validated(self) -> None:
        result = descriptive_pattern_comparison(
            z1_seconds=9000.0, z2_seconds=600.0, z3_seconds=400.0,
            polarized_bands=((89.0, 91.0), (0.0, 10.0), (0.0, 20.0)),
        )
        assert result.label == "polarized"
        with pytest.raises(ValueError, match="band"):
            descriptive_pattern_comparison(
                z1_seconds=1.0, z2_seconds=1.0, z3_seconds=1.0,
                polarized_bands=((10.0, 5.0), (0.0, 10.0), (0.0, 10.0)),
            )
        with pytest.raises(ValueError, match="band"):
            descriptive_pattern_comparison(
                z1_seconds=1.0, z2_seconds=1.0, z3_seconds=1.0,
                pyramidal_bands=((0.0, 10.0), (0.0, 10.0), (120.0, 130.0)),
            )

    def test_minimum_volume_rule_refuses_to_label_low_volume_weeks(
        self,
    ) -> None:
        # 1 h 30 m of perfectly polarized-looking time is still BELOW the
        # minimum pattern volume: no pattern label at all.
        low = descriptive_pattern_comparison(
            z1_seconds=4320.0, z2_seconds=270.0, z3_seconds=810.0,
        )
        assert low.total_seconds == 5400.0
        assert low.total_seconds < MIN_VOLUME
        assert low.label == "insufficient_volume"
        assert low.polarized_within_bands is None
        assert low.pyramidal_within_bands is None
        # The same split at sufficient volume IS labelled.
        high = descriptive_pattern_comparison(
            z1_seconds=43200.0, z2_seconds=2700.0, z3_seconds=8100.0,
        )
        assert high.label == "polarized"

    def test_minimum_volume_threshold_is_configurable(self) -> None:
        result = descriptive_pattern_comparison(
            z1_seconds=900.0, z2_seconds=60.0, z3_seconds=40.0,
            min_pattern_week_seconds=600.0,
        )
        assert result.label != "insufficient_volume"

    def test_zero_total_seconds_is_insufficient_volume(self) -> None:
        result = descriptive_pattern_comparison(
            z1_seconds=0.0, z2_seconds=0.0, z3_seconds=0.0,
        )
        assert result.label == "insufficient_volume"
        assert result.z1_pct is None

    def test_negative_and_non_finite_seconds_raise(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            descriptive_pattern_comparison(
                z1_seconds=-1.0, z2_seconds=1.0, z3_seconds=1.0,
            )
        with pytest.raises(ValueError, match="finite"):
            descriptive_pattern_comparison(
                z1_seconds=math.nan, z2_seconds=1.0, z3_seconds=1.0,
            )
        with pytest.raises(ValueError, match="finite"):
            descriptive_pattern_comparison(
                z1_seconds=1.0, z2_seconds=math.inf, z3_seconds=1.0,
            )

    def test_output_is_descriptive_only_no_moralising_language(self) -> None:
        # Section 7.5: "Describe, do not moralize." No output field may
        # carry advice, recommendation or judgement language at all.
        forbidden = (
            "should", "must", "recommend", "advice", "advise", "ideal",
            "optimal", "improve", "avoid", "warn", "moral", "good", "bad",
            "better", "worse", "correct", "wrong", "prescri",
        )
        results = [
            descriptive_pattern_comparison(
                z1_seconds=0.80 * POLARIZED_TOTAL,
                z2_seconds=0.05 * POLARIZED_TOTAL,
                z3_seconds=0.15 * POLARIZED_TOTAL,
            ),
            descriptive_pattern_comparison(
                z1_seconds=0.65 * POLARIZED_TOTAL,
                z2_seconds=0.25 * POLARIZED_TOTAL,
                z3_seconds=0.10 * POLARIZED_TOTAL,
            ),
            descriptive_pattern_comparison(
                z1_seconds=0.50 * POLARIZED_TOTAL,
                z2_seconds=0.30 * POLARIZED_TOTAL,
                z3_seconds=0.20 * POLARIZED_TOTAL,
            ),
            descriptive_pattern_comparison(
                z1_seconds=100.0, z2_seconds=100.0, z3_seconds=100.0,
            ),  # insufficient volume
        ]
        for result in results:
            text = " | ".join(
                field for field in (result.detail, result.caveat) if field
            )
            lowered = text.lower()
            for word in forbidden:
                assert word not in lowered, f"forbidden language: {word!r}"

    def test_caveat_always_present_and_names_descriptive_nature(self) -> None:
        result = descriptive_pattern_comparison(
            z1_seconds=0.80 * POLARIZED_TOTAL,
            z2_seconds=0.05 * POLARIZED_TOTAL,
            z3_seconds=0.15 * POLARIZED_TOTAL,
        )
        assert result.caveat
        assert "descriptive" in result.caveat.lower()

    def test_convenience_wrapper_over_a_weekly_sport_entry(self) -> None:
        # 80 / 10 / 10: source Z1 (3z Z1) + source Z3 (3z Z2) + source Z5
        # (3z Z3) — a polarized-looking bike week straight from the
        # aggregation output.
        sessions = [
            zone_session(W2_MON, "bike", {"Z1": 8000.0, "Z3": 1000.0,
                                          "Z5": 1000.0}),
        ]
        weeks = weekly_time_in_zone(sessions, sports=["bike"])
        bike = weeks[0].sports[0]
        result = descriptive_pattern_comparison(
            z1_seconds=bike.z1_seconds,
            z2_seconds=bike.z2_seconds,
            z3_seconds=bike.z3_seconds,
        )
        assert result.label == "polarized"
        assert result.total_seconds == 10000.0


# ---------------------------------------------------------------------------
# Structural conventions (pure module, primitive-output shape)
# ---------------------------------------------------------------------------


class TestModuleConventions:
    def test_result_objects_are_frozen(self) -> None:
        model = three_zone_model("run_hr")
        with pytest.raises(dataclasses.FrozenInstanceError):
            model.first_threshold_pct = 1.0  # type: ignore[misc]
        weeks = weekly_time_in_zone(
            [zone_session(W2_MON, "bike", {"Z1": 60.0})], sports=["bike"]
        )
        week: WeeklyIntensity = weeks[0]
        sport: WeeklySportZones = week.sports[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            sport.total_seconds = 0.0  # type: ignore[misc]

    def test_sport_and_zone_keys_are_the_stable_namespaces(self) -> None:
        assert SPORT_KEYS == ("run", "bike", "swim")
        assert INTENSITY_ZONE_KEYS == ("Z1", "Z2", "Z3")
        assert set(INTENSITY_MODALITY_KEYS) == {
            "bike_power", "run_hr", "bike_hr", "swim_pace",
        }
        model: ThreeZoneModel = three_zone_model("swim_pace")
        assert model.source_zone_keys == ("Z1", "Z2", "Z3", "Z4", "Z5")


# ---------------------------------------------------------------------------
# Modality selection (power-less bike fallback; mirrors the load engine's
# power-first preference, app.engine.load.select_load_method)
# ---------------------------------------------------------------------------


class TestIntensityModalitySelection:
    def test_bike_prefers_power_when_usable_power_samples_exist(self) -> None:
        """Power wins over HR on the bike: the same preference order the
        load engine's method selection fixes (power -> HR)."""
        assert (
            select_intensity_modality(
                "bike", has_power_samples=True, has_hr_samples=True
            )
            == "bike_power"
        )
        assert (
            select_intensity_modality(
                "bike", has_power_samples=True, has_hr_samples=False
            )
            == "bike_power"
        )

    def test_bike_falls_back_to_bike_hr_without_power(self) -> None:
        """A power-less ride with heart rate classifies on the Friel bike
        HR table (bike_hr: LT1 90% LTHR, LT2 100% LTHR)."""
        assert (
            select_intensity_modality(
                "bike", has_power_samples=False, has_hr_samples=True
            )
            == "bike_hr"
        )

    def test_bike_without_power_or_hr_raises_explicitly(self) -> None:
        """No usable modality is an EXPLICIT outcome (the engine never
        silently drops a session): a ValueError naming both candidate
        modalities."""
        with pytest.raises(ValueError, match="bike_power"):
            select_intensity_modality(
                "bike", has_power_samples=False, has_hr_samples=False
            )
        with pytest.raises(ValueError, match="bike_hr"):
            select_intensity_modality(
                "bike", has_power_samples=False, has_hr_samples=False
            )

    def test_run_and_swim_are_unchanged(self) -> None:
        """The selector refines only the bike: run stays run_hr and swim
        stays swim_pace regardless of the flags (their source tables do
        not depend on stream availability)."""
        assert (
            select_intensity_modality(
                "run", has_power_samples=False, has_hr_samples=True
            )
            == "run_hr"
        )
        assert (
            select_intensity_modality(
                "swim", has_power_samples=True, has_hr_samples=True
            )
            == "swim_pace"
        )

    def test_unknown_sport_raises(self) -> None:
        with pytest.raises(ValueError, match="sport"):
            select_intensity_modality(
                "row",  # type: ignore[arg-type]
                has_power_samples=True,
                has_hr_samples=True,
            )


class TestHasUsableSamples:
    def test_absent_empty_or_all_none_stream_is_not_usable(self) -> None:
        """The load engine's usable-sample rule verbatim: a stream that is
        absent (``None``), empty, or ENTIRELY ``None`` (all-gap) is not
        usable — a stream that exists but holds no valid sample must not
        select the modality that classifies it."""
        assert has_usable_samples(None) is False
        assert has_usable_samples([]) is False
        assert has_usable_samples([None, None, None]) is False

    def test_one_valid_sample_makes_the_stream_usable(self) -> None:
        assert has_usable_samples([None, 120.0, None]) is True
        assert has_usable_samples([0.0]) is True  # 0 is a value, not a gap


class TestBikeHrSessionsFlowThroughTheWeeklyAggregation:
    def test_bike_hr_zone_keys_validate_against_the_bike_hr_table(
        self,
    ) -> None:
        """A bike session classified on the bike_hr table carries the
        Friel bike HR zone keys (Z5a/Z5b/Z5c, not the Coggan Z5-Z7): the
        weekly aggregation must validate and map it against the bike_hr
        model, not the sport's default bike_power table."""
        session = zone_session(
            W2_MON,
            "bike",
            {"Z1": 600.0, "Z3": 300.0, "Z5a": 100.0},
            modality="bike_hr",
        )
        assert session.modality == "bike_hr"
        weeks = weekly_time_in_zone([session], sports=["bike"])
        bike = weeks[0].sports[0]
        assert bike.status == "data"
        # bike_hr 3-zone map: Z1->Z1, Z3->Z2, Z5a->Z3.
        assert bike.z1_seconds == pytest.approx(600.0)
        assert bike.z2_seconds == pytest.approx(300.0)
        assert bike.z3_seconds == pytest.approx(100.0)
        assert bike.percentages == pytest.approx([60.0, 30.0, 10.0])

    def test_default_modality_still_the_canonical_map(self) -> None:
        """Without an explicit modality the session keeps the sport's
        default SPORT_MODALITY table (bike -> bike_power)."""
        session = zone_session(W2_MON, "bike", {"Z7": 60.0})
        assert session.modality == "bike_power"
        weeks = weekly_time_in_zone([session], sports=["bike"])
        assert weeks[0].sports[0].z3_seconds == pytest.approx(60.0)

    def test_mixed_bike_modalities_in_one_week(self) -> None:
        """A power ride and an HR ride in the same week each aggregate on
        their own source table (per-session modality, no cross-table
        key confusion)."""
        sessions = [
            zone_session(W2_MON, "bike", {"Z1": 100.0}),  # bike_power Z1
            zone_session(W2_SUN, "bike", {"Z5a": 50.0}, modality="bike_hr"),
        ]
        weeks = weekly_time_in_zone(sessions, sports=["bike"])
        bike = weeks[0].sports[0]
        assert bike.total_seconds == pytest.approx(150.0)
        # bike_power Z1 -> Z1; bike_hr Z5a -> Z3.
        assert bike.z1_seconds == pytest.approx(100.0)
        assert bike.z3_seconds == pytest.approx(50.0)
        assert bike.z2_seconds == pytest.approx(0.0)
