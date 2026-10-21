"""The 7-day sport mix must count REAL sports, not their source spellings.

Measured in production on 2026-10-21: `plan_workout`'s coverage came back as
``{"ride": 1, "virtualride": 2}`` for an athlete who had only cycled indoors and
outdoors. The imbalance rule therefore saw TWO sports where there was one, and "the
other sport" it resolves could offer an indoor ride as the alternative to an outdoor
ride -- advice that is not advice.

Normalised at the accounting boundary, not in the engine's vocabulary: the data source
spells things its own way and the engine must keep accepting what it is handed.
"""

from __future__ import annotations

from app.agent.tools import _sport_mix


def test_virtual_rides_count_as_rides() -> None:
    """The exact production shape: one outdoor ride and two virtual ones."""
    assert _sport_mix(["ride", "virtualride", "virtualride"]) == {"ride": 3}


def test_virtual_and_treadmill_runs_count_as_runs() -> None:
    assert _sport_mix(["run", "virtualrun", "treadmillrun"]) == {"run": 3}


def test_ebike_counts_as_ride() -> None:
    assert _sport_mix(["ebikeride"]) == {"ride": 1}


def test_case_and_surrounding_whitespace_are_normalised() -> None:
    assert _sport_mix([" Ride ", "VIRTUALRIDE"]) == {"ride": 2}


def test_unknown_types_never_invent_a_sport() -> None:
    """An unknown type must not create a sport the engine cannot balance against."""
    assert _sport_mix(["skydiving", "ride"]) == {"ride": 1}
