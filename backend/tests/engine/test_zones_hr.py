"""Friel heart-rate zones per sport (ZON-8, PROJECT_BRIEF sections 7.3/12.4).

The two published tables are asserted EXACTLY as printed in section 7.3
(Friel, % of LTHR) as SEPARATE per-sport tables:

    Run : Z1 < 85, Z2 85-89, Z3 90-94, Z4 95-99, Z5a 100-102, Z5b 103-106, Z5c > 106
    Bike: Z1 < 81, Z2 81-89, Z3 90-93, Z4 94-99, Z5a 100-102, Z5b 103-106, Z5c > 106

The tables DIFFER for Z1/Z2/Z3 (run 85/89/94 vs bike 81/89/93) and the tests
assert that difference explicitly, so a copy-paste mistake cannot pass.

Gap rule (same documented continuous-partition rule as the Coggan power
zones): each zone spans from its printed lower boundary value (inclusive) to
the next zone's boundary value (exclusive); Z1 stays strictly below its first
printed boundary and Z5c strictly above its last printed boundary, exactly as
printed. So Z2 = [85, 90) for run, Z5b = [103, 106] (the printed 106 stays in
Z5b because Z5c is strictly > 106), and a fractional percentage such as 89.5%
joins the zone whose printed range it adjoins from below (Z2).

Absolute bpm arithmetic for the owner's LTHR of 169 bpm (bpm = pct x 169/100):

     81 % -> 136.89     85 % -> 143.65     89 % -> 150.41     90 % -> 152.10
     93 % -> 157.17     94 % -> 158.86     95 % -> 160.55     99 % -> 167.31
    100 % -> 169.00    102 % -> 172.38    103 % -> 174.07    106 % -> 179.14
"""

import pytest

from app.engine.zones import HR_SPORT_KEYS, HrZone, hr_zone_for, hr_zones

LTHR: float = 169.0  # the owner's run/bike LTHR of record


class TestPublishedTables:
    def test_seven_zones_with_keys_per_sport(self) -> None:
        for sport in HR_SPORT_KEYS:
            zones = hr_zones(sport, LTHR)
            assert [zone.key for zone in zones] == [
                "Z1", "Z2", "Z3", "Z4", "Z5a", "Z5b", "Z5c",
            ]

    def test_run_pct_bounds_match_the_published_table(self) -> None:
        zones = hr_zones("run", LTHR)
        expected = (
            ("Z1", None, False, 85.0, False),
            ("Z2", 85.0, True, 90.0, False),
            ("Z3", 90.0, True, 95.0, False),
            ("Z4", 95.0, True, 100.0, False),
            ("Z5a", 100.0, True, 103.0, False),
            ("Z5b", 103.0, True, 106.0, True),
            ("Z5c", 106.0, False, None, False),
        )
        for zone, (key, lo, lo_inc, hi, hi_inc) in zip(zones, expected, strict=True):
            assert zone.key == key
            assert zone.min_pct_lthr == lo
            assert zone.min_pct_inclusive is lo_inc
            assert zone.max_pct_lthr == hi
            assert zone.max_pct_inclusive is hi_inc

    def test_bike_pct_bounds_match_the_published_table(self) -> None:
        zones = hr_zones("bike", LTHR)
        expected = (
            ("Z1", None, False, 81.0, False),
            ("Z2", 81.0, True, 90.0, False),
            ("Z3", 90.0, True, 94.0, False),
            ("Z4", 94.0, True, 100.0, False),
            ("Z5a", 100.0, True, 103.0, False),
            ("Z5b", 103.0, True, 106.0, True),
            ("Z5c", 106.0, False, None, False),
        )
        for zone, (key, lo, lo_inc, hi, hi_inc) in zip(zones, expected, strict=True):
            assert zone.key == key
            assert zone.min_pct_lthr == lo
            assert zone.min_pct_inclusive is lo_inc
            assert zone.max_pct_lthr == hi
            assert zone.max_pct_inclusive is hi_inc

    def test_run_and_bike_tables_differ_where_friel_differs(self) -> None:
        # Copy-paste guard: the run and bike tables are SEPARATE constants and
        # must differ exactly where the published tables differ (Z1 and Z3
        # bounds: run 85/94 vs bike 81/93), while Z5a/Z5b/Z5c are identical.
        run = hr_zones("run", LTHR)
        bike = hr_zones("bike", LTHR)
        assert run[0].max_pct_lthr == 85.0
        assert bike[0].max_pct_lthr == 81.0
        assert run[2].max_pct_lthr == 95.0  # printed Z3 90-94 -> [90, 95)
        assert bike[2].max_pct_lthr == 94.0  # printed Z3 90-93 -> [90, 94)
        assert [z.max_pct_lthr for z in run[:4]] != [
            z.max_pct_lthr for z in bike[:4]
        ]
        assert [z.min_pct_lthr for z in run[4:]] == [
            z.min_pct_lthr for z in bike[4:]
        ]

    def test_gap_rule_is_documented_in_the_docstring(self) -> None:
        doc = hr_zones.__doc__ or ""
        assert "89.5" in doc  # the documented fractional example
        assert "gap" in doc.lower()


class TestAbsoluteBpmBounds:
    def test_run_bounds_for_lthr_169(self) -> None:
        # bpm = pct * 169 / 100 (hand arithmetic in the module docstring).
        zones = hr_zones("run", LTHR)
        expected = (
            ("Z1", None, 143.65),  #  85 % = 143.65 (exclusive)
            ("Z2", 143.65, 152.10),  #  85-89 % (+fractional gap up to the 90 % boundary)
            ("Z3", 152.10, 160.55),  #  90-94 % (+gap up to the 95 % boundary)
            ("Z4", 160.55, 169.00),  #  95-99 % (+gap up to the 100 % boundary)
            ("Z5a", 169.00, 174.07),  #  100-102 %
            ("Z5b", 174.07, 179.14),  #  103-106 %
            ("Z5c", 179.14, None),  #  > 106 % (exclusive)
        )
        for zone, (key, lo, hi) in zip(zones, expected, strict=True):
            assert zone.key == key
            assert zone.min_bpm == (None if lo is None else pytest.approx(lo))
            assert zone.max_bpm == (None if hi is None else pytest.approx(hi))

    def test_bike_bounds_for_lthr_169(self) -> None:
        zones = hr_zones("bike", LTHR)
        expected = (
            ("Z1", None, 136.89),  #  81 % (exclusive) — bike-only lower table
            ("Z2", 136.89, 152.10),  #  81-89 % (+gap up to the 90 % boundary)
            ("Z3", 152.10, 158.86),  #  90-93 % (+gap up to the 94 % boundary)
            ("Z4", 158.86, 169.00),  #  94-99 % (+gap up to the 100 % boundary)
            ("Z5a", 169.00, 174.07),
            ("Z5b", 174.07, 179.14),
            ("Z5c", 179.14, None),
        )
        for zone, (key, lo, hi) in zip(zones, expected, strict=True):
            assert zone.key == key
            assert zone.min_bpm == (None if lo is None else pytest.approx(lo))
            assert zone.max_bpm == (None if hi is None else pytest.approx(hi))

    def test_result_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        zone = hr_zones("run", LTHR)[0]
        assert isinstance(zone, HrZone)
        with pytest.raises(FrozenInstanceError):
            zone.max_bpm = 0.0  # type: ignore[misc]


class TestClassifier:
    @pytest.mark.parametrize(
        ("bpm", "expected_key"),
        [
            # Interior hand values (run, LTHR 169).
            (140.0, "Z1"),  # ~82.8 % LTHR
            (145.0, "Z2"),  # ~85.8 % LTHR
            (151.0, "Z2"),  # ~89.3 % LTHR — still Z2 (Z3 starts at 90 %)
            (153.0, "Z3"),  # ~90.5 % LTHR
            (161.0, "Z4"),  # ~95.3 % LTHR
            (170.0, "Z5a"),  # ~100.6 % LTHR
            (175.0, "Z5b"),  # ~103.6 % LTHR
            (180.0, "Z5c"),  # ~106.5 % LTHR
            # Hand-exact printed boundary bpm values: 85/90/95/100/103/106 %.
            (143.65, "Z2"),
            (152.10, "Z3"),
            (160.55, "Z4"),
            (169.00, "Z5a"),
            (174.07, "Z5b"),
            (179.14, "Z5b"),  # printed Z5b upper bound stays in Z5b (Z5c > 106)
        ],
    )
    def test_run_boundary_and_interior_bpm(
        self, bpm: float, expected_key: str
    ) -> None:
        assert hr_zone_for("run", bpm, LTHR).key == expected_key

    @pytest.mark.parametrize(
        ("bpm", "expected_key"),
        [
            (135.0, "Z1"),  # ~79.9 % LTHR — below the bike-only 81 % bound
            (136.89, "Z2"),  # exactly 81 % of 169 — the bike-only boundary
            (157.17, "Z3"),  # exactly 93 % of 169 — the bike-only Z3 upper edge
            (169.00, "Z5a"),
            (179.14, "Z5b"),
        ],
    )
    def test_bike_boundary_and_interior_bpm(
        self, bpm: float, expected_key: str
    ) -> None:
        assert hr_zone_for("bike", bpm, LTHR).key == expected_key

    def test_fractional_gap_percentages_follow_the_partition_rule(self) -> None:
        # 89.5 % of LTHR sits in the printed gap between Z2 (85-89) and
        # Z3 (90-94): it joins Z2, whose absolute range extends to the 90 %
        # boundary (152.10 bpm, exclusive) under the partition rule.
        assert hr_zone_for("run", 89.5 * LTHR / 100.0, LTHR).key == "Z2"
        # 106.5 % is strictly above the printed 106 % -> Z5c.
        assert hr_zone_for("run", 106.5 * LTHR / 100.0, LTHR).key == "Z5c"

    def test_classifier_returns_the_matching_table_entry(self) -> None:
        run = hr_zones("run", LTHR)
        assert hr_zone_for("run", 143.65, LTHR) == run[1]  # Z2 starts at 85 %
        assert hr_zone_for("run", 179.14, LTHR) == run[5]  # Z5b ends at 106 %


class TestValidation:
    def test_non_positive_lthr_raises_for_the_table(self) -> None:
        with pytest.raises(ValueError, match="lthr"):
            hr_zones("run", 0.0)
        with pytest.raises(ValueError, match="lthr"):
            hr_zones("bike", -169.0)

    def test_non_positive_lthr_raises_for_the_classifier(self) -> None:
        with pytest.raises(ValueError, match="lthr"):
            hr_zone_for("run", 150.0, 0.0)
        with pytest.raises(ValueError, match="lthr"):
            hr_zone_for("bike", 150.0, -169.0)

    def test_unknown_sport_raises(self) -> None:
        with pytest.raises(ValueError, match="sport"):
            hr_zones("swim", LTHR)  # type: ignore[arg-type]

    def test_negative_bpm_raises_for_the_classifier(self) -> None:
        with pytest.raises(ValueError, match="bpm"):
            hr_zone_for("run", -1.0, LTHR)
