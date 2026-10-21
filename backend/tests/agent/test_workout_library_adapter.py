"""T2 of ODD feature workout-planner (spec S6): the library adapter.

The adapter matches a DECIDED session (the planner's output) to a REAL
workout from the curated library loaded by :func:`load_workout_library`
(the WLK-5 loader, ``odd/tasks/workout-library.md``). Matching rule, in
order: same sport, the workout's target zone overlaps the session's zone
keys, and its duration is within the session's cap when the session has
one. No match is ``None`` — never an invented reference (S6/S2: concrete
content is only ever REFERENCED from the owner's curated library).

These tests run against the COMMITTED canonical fixtures via the real
loader: they are repo data, not network, so the matched entries are the
owner's actual workouts (the test proves a real reference, not a shape).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.workout_library import WorkoutLibrary, load_workout_library
from app.agent.workout_library_adapter import match_library_workout


@pytest.fixture(scope="module")
def library() -> WorkoutLibrary:
    """The real curated library from the committed fixtures."""
    return load_workout_library()


class TestMatchLibraryWorkout:
    def test_quality_ride_session_matches_a_real_library_entry(
        self, library: WorkoutLibrary
    ) -> None:
        # A quality session prescribes zone Z3 with no duration cap, so any
        # cycling workout whose target zones overlap Z3 matches; the FIRST
        # such entry in the library's deterministic (parse) order wins.
        # The committed corpus's first Zone-3-Tempo session is E3.
        entry = match_library_workout(
            library, sport="ride", zone_keys=("Z3",), duration_cap_s=None
        )
        assert entry is not None
        # A REAL library entry, by its stable id and name — never invented.
        assert entry.id == "cycling-E3"
        assert entry.name == "E3"
        assert entry in library.entries

    def test_capped_recovery_session_matches_nothing_in_the_current_library(
        self, library: WorkoutLibrary
    ) -> None:
        # The corpus's Z1/Z2-only sessions are E1/E5 (7500 s, above the
        # 60-minute recovery cap) and two sessions with unresolved
        # ``Repetir N veces`` markers, whose true duration is UNKNOWN
        # (repetitions are never expanded) and so cannot honestly fit a
        # cap. No match is an honest None, not a wrong reference.
        entry = match_library_workout(
            library, sport="ride", zone_keys=("Z1", "Z2"), duration_cap_s=3600.0
        )
        assert entry is None

    def test_duration_cap_is_inclusive(self, library: WorkoutLibrary) -> None:
        # At exactly the cap the workout still fits (E1 is 7500 s).
        entry = match_library_workout(
            library, sport="ride", zone_keys=("Z1", "Z2"), duration_cap_s=7500.0
        )
        assert entry is not None
        assert entry.id == "cycling-E1"

    def test_sport_without_library_content_matches_nothing(
        self, library: WorkoutLibrary
    ) -> None:
        # The curated library has cycling and gym content only; a run
        # session references nothing rather than a cycling workout.
        entry = match_library_workout(
            library, sport="run", zone_keys=("Z3",), duration_cap_s=None
        )
        assert entry is None

    def test_rest_session_without_zone_keys_matches_nothing(
        self, library: WorkoutLibrary
    ) -> None:
        # The matching rule is zone overlap; a rest prescription names no
        # zones, so it references no library workout.
        entry = match_library_workout(
            library, sport="ride", zone_keys=None, duration_cap_s=None
        )
        assert entry is None

    def test_session_without_a_sport_matches_nothing(
        self, library: WorkoutLibrary
    ) -> None:
        # Without a concrete sport there is no library side to match.
        entry = match_library_workout(
            library, sport=None, zone_keys=("Z3",), duration_cap_s=None
        )
        assert entry is None

    def test_strength_session_matches_no_gym_section_because_gym_has_no_zones(
        self, library: WorkoutLibrary
    ) -> None:
        # Gym sections carry no zone targets, so the rule's zone-overlap
        # requirement can never hold for them — honest None, no special case.
        entry = match_library_workout(
            library, sport="weighttraining", zone_keys=("Z3",), duration_cap_s=None
        )
        assert entry is None

    def test_first_matching_entry_in_library_order_wins(
        self, library: WorkoutLibrary
    ) -> None:
        # Determinism: several cycling sessions target Z3 (E3 and the
        # session-NN entries); the adapter returns the first in the
        # library's stable order, so the same session always references
        # the same workout.
        first = match_library_workout(
            library, sport="ride", zone_keys=("Z3",), duration_cap_s=None
        )
        again = match_library_workout(
            library, sport="ride", zone_keys=("Z3",), duration_cap_s=None
        )
        assert first is not None and again is not None
        assert first.id == again.id
        entries: tuple[Any, ...] = library.entries
        z3_ids = [
            e.id
            for e in entries
            if e.source == "cycling"
            and {"Zone 3: Tempo"}
            & {
                item.zone
                for block in e.content.blocks
                for item in block.items
                if getattr(item, "zone", None)
            }
        ]
        assert first.id == z3_ids[0]
