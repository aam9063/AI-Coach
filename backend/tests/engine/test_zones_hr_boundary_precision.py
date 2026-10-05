"""Boundary-precision regression for the Friel HR zone classifier.

The zone table rounds its absolute bpm bounds to two decimals, which makes
an exact equality check fragile: a caller who computes the boundary himself
(``0.95 * 169``) gets a double one ULP away from the table's rounded literal
(``160.55``), and the classifier then contradicted its own table — 95% of
LTHR landed in Z3 instead of Z4, and 106% landed in Z5c instead of Z5b.

Real heart-rate samples are whole bpm and never sit exactly on those
fractional boundaries, so this is a robustness fix for a documented promise
("179.14 bpm is still Z5b because Z5c is strictly > 106%"), not a change of
the zone tables themselves.
"""

from __future__ import annotations

import pytest

from app.engine.zones import hr_zone_for

LTHR = 169.0

# Boundary percentages whose float product does NOT equal the rounded literal
# the table stores. Each entry: percentage, expected zone at that boundary.
COMPUTED_BOUNDARIES = [
    (85.0, "Z2"),  # 143.65
    (90.0, "Z3"),  # 152.10
    (95.0, "Z4"),  # 160.55 — was Z3 before the fix
    (100.0, "Z5a"),  # 169.00
    (103.0, "Z5b"),  # 174.07
    (106.0, "Z5b"),  # 179.14 — was Z5c before the fix
]


@pytest.mark.parametrize(("pct", "expected"), COMPUTED_BOUNDARIES)
def test_boundary_computed_as_float_classifies_like_the_table(
    pct: float, expected: str
) -> None:
    bpm = pct / 100.0 * LTHR

    assert hr_zone_for("run", bpm, LTHR).key == expected


@pytest.mark.parametrize(("pct", "expected"), COMPUTED_BOUNDARIES)
def test_boundary_written_as_literal_classifies_the_same(
    pct: float, expected: str
) -> None:
    """The rounded literal from the table must agree with the computed value."""
    literal = round(pct / 100.0 * LTHR, 2)

    assert hr_zone_for("run", literal, LTHR).key == expected


def test_bike_table_boundaries_behave_the_same_way() -> None:
    assert hr_zone_for("bike", 0.81 * LTHR, LTHR).key == "Z2"
    assert hr_zone_for("bike", 0.94 * LTHR, LTHR).key == "Z4"
    assert hr_zone_for("bike", 1.06 * LTHR, LTHR).key == "Z5b"


def test_values_just_inside_a_zone_are_unaffected() -> None:
    """The tolerance must not swallow genuinely different heart rates."""
    assert hr_zone_for("run", 160.0, LTHR).key == "Z3"  # below 160.55
    assert hr_zone_for("run", 161, LTHR).key == "Z4"  # above 160.55
    assert hr_zone_for("run", 179, LTHR).key == "Z5b"
    assert hr_zone_for("run", 180, LTHR).key == "Z5c"  # above 179.14
