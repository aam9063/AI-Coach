"""Coggan power zone table and classifier (ZON-4, PROJECT_BRIEF sections 7.3/12.4).

The published table (Allen & Coggan, % of FTP) is asserted EXACTLY as
printed in section 7.3:

    Z1 < 55, Z2 56-75, Z3 76-90, Z4 91-105, Z5 106-120, Z6 121-150, Z7 > 150.

Gap rule (documented, tested — not left undefined): the printed bounds leave
gaps for fractional percentages (e.g. 55.5%). The implemented rule is the
continuous partition at the printed boundary values themselves: each of
Z2-Z6 spans from its printed lower boundary value (inclusive) to the next
zone's boundary value (exclusive), i.e. Z2 = [55, 76), Z3 = [76, 91),
Z4 = [91, 106), Z5 = [106, 121), Z6 = [121, 150]; Z1 stays strictly below 55
and Z7 strictly above 150, exactly as printed. So 55.0% and 55.5% are Z2,
75.5% is still Z2, 76.0% is Z3, and 150.5% is Z7. Both strict printed
inequalities (Z1 < 55, Z7 > 150) are preserved; the fractional gaps join the
zone whose printed range they adjoin from below.

Watt arithmetic for the owner's FTP of 180 W (watts = pct * 180 / 100):

    55 % ->  99.0 W     76 % -> 136.8 W     91 % -> 163.8 W    106 % -> 190.8 W
    56 % -> 100.8 W     75 % -> 135.0 W     90 % -> 162.0 W    105 % -> 189.0 W
   120 % -> 216.0 W    121 % -> 217.8 W    150 % -> 270.0 W    151 % -> 271.8 W
"""

from typing import Final

import pytest

from app.engine.zones import power_zone_for, power_zones

FTP: Final = 180.0  # the owner's manual FTP of record


# Every printed percentage boundary and the zone it must classify into.
PRINTED_BOUNDARIES: Final[tuple[tuple[float, str], ...]] = (
    (55.0, "Z2"),  #  99.0 W — gap value: joins Z2 (Z1 is strictly < 55)
    (56.0, "Z2"),  # 100.8 W
    (75.0, "Z2"),  # 135.0 W
    (76.0, "Z3"),  # 136.8 W
    (90.0, "Z3"),  # 162.0 W
    (91.0, "Z4"),  # 163.8 W
    (105.0, "Z4"),  # 189.0 W
    (106.0, "Z5"),  # 190.8 W
    (120.0, "Z5"),  # 216.0 W
    (121.0, "Z6"),  # 217.8 W
    (150.0, "Z6"),  # 270.0 W — Z7 is strictly > 150
    (151.0, "Z7"),  # 271.8 W
)

# Fractional values strictly between the printed whole numbers: the gap rule.
GAP_VALUES: Final[tuple[tuple[float, str], ...]] = (
    (54.999, "Z1"),  #  98.9982 W
    (55.0, "Z2"),  #  99.0 W
    (55.5, "Z2"),  #  99.9 W
    (75.5, "Z2"),  # 135.9 W
    (76.0, "Z3"),  # 136.8 W
    (90.5, "Z3"),  # 162.9 W
    (91.0, "Z4"),  # 163.8 W
    (105.5, "Z4"),  # 189.9 W
    (106.0, "Z5"),  # 190.8 W
    (120.5, "Z5"),  # 216.9 W
    (121.0, "Z6"),  # 217.8 W
    (150.0, "Z6"),  # 270.0 W
    (150.5, "Z7"),  # 270.9 W
    (151.0, "Z7"),  # 271.8 W
)


class TestPublishedTable:
    def test_seven_coggan_zones_with_published_names(self) -> None:
        zones = power_zones(FTP)
        assert [zone.key for zone in zones] == [
            "Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7",
        ]
        assert [zone.name for zone in zones] == [
            "Active recovery",
            "Endurance",
            "Tempo",
            "Threshold",
            "VO2 max",
            "Anaerobic capacity",
            "Neuromuscular power",
        ]

    def test_printed_pct_bounds_match_the_published_table(self) -> None:
        zones = power_zones(FTP)
        expected = (
            ("Z1", None, False, 55.0, False),
            ("Z2", 55.0, True, 76.0, False),
            ("Z3", 76.0, True, 91.0, False),
            ("Z4", 91.0, True, 106.0, False),
            ("Z5", 106.0, True, 121.0, False),
            ("Z6", 121.0, True, 150.0, True),
            ("Z7", 150.0, False, None, False),
        )
        for zone, (key, lo, lo_inc, hi, hi_inc) in zip(zones, expected, strict=True):
            assert zone.key == key
            assert zone.min_pct_ftp == lo
            assert zone.min_pct_inclusive is lo_inc
            assert zone.max_pct_ftp == hi
            assert zone.max_pct_inclusive is hi_inc

    def test_watt_bounds_for_ftp_180w(self) -> None:
        # watts = pct * 180 / 100 (hand arithmetic in the module docstring).
        zones = power_zones(FTP)
        expected = (
            ("Z1", None, 99.0),
            ("Z2", 99.0, 136.8),
            ("Z3", 136.8, 163.8),
            ("Z4", 163.8, 190.8),
            ("Z5", 190.8, 217.8),
            ("Z6", 217.8, 270.0),
            ("Z7", 270.0, None),
        )
        for zone, (key, lo, hi) in zip(zones, expected, strict=True):
            assert zone.key == key
            assert zone.min_watts == (None if lo is None else pytest.approx(lo))
            assert zone.max_watts == (None if hi is None else pytest.approx(hi))

    def test_gap_rule_is_documented_in_the_docstring(self) -> None:
        doc = power_zones.__doc__ or ""
        assert "55.5" in doc  # the documented fractional example
        assert "gap" in doc.lower()


class TestClassifierOnPrintedBoundaries:
    @pytest.mark.parametrize(("pct", "expected_key"), PRINTED_BOUNDARIES)
    def test_printed_boundary_pcts(self, pct: float, expected_key: str) -> None:
        watts = pct * FTP / 100.0  # arithmetic shown in the module docstring
        assert power_zone_for(watts, FTP).key == expected_key

    @pytest.mark.parametrize(("pct", "expected_key"), GAP_VALUES)
    def test_fractional_gap_values(self, pct: float, expected_key: str) -> None:
        watts = pct * FTP / 100.0
        assert power_zone_for(watts, FTP).key == expected_key

    def test_zero_watts_is_zone1(self) -> None:
        assert power_zone_for(0.0, FTP).key == "Z1"

    def test_classifier_returns_the_matching_table_entry(self) -> None:
        zones = power_zones(FTP)
        assert power_zone_for(99.0, FTP) == zones[1]  # Z2 starts at 55 % = 99.0 W
        assert power_zone_for(270.0, FTP) == zones[5]  # 150 % = 270.0 W is Z6
        assert power_zone_for(271.8, FTP) == zones[6]  # 151 % = 271.8 W is Z7


class TestValidation:
    def test_non_positive_ftp_raises_for_the_table(self) -> None:
        with pytest.raises(ValueError, match="ftp"):
            power_zones(0.0)
        with pytest.raises(ValueError, match="ftp"):
            power_zones(-180.0)

    def test_non_positive_ftp_raises_for_the_classifier(self) -> None:
        with pytest.raises(ValueError, match="ftp"):
            power_zone_for(100.0, 0.0)
        with pytest.raises(ValueError, match="ftp"):
            power_zone_for(100.0, -180.0)

    def test_negative_watts_raise(self) -> None:
        with pytest.raises(ValueError, match="watts"):
            power_zone_for(-1.0, FTP)
