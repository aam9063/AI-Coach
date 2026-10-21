"""Library adapter (T2, spec S6 of ``odd/tasks/workout-planner.md``): match
a DECIDED session to a REAL workout from the curated library.

The planner (``app.engine.planner``, T1) decides the session; this adapter
is the seam that grounds its zone prescription in the owner's curated data
bank loaded by the WLK-5 loader (:func:`app.agent.workout_library.
load_workout_library`). It answers one question — *is there a REAL library
workout that fits this decided session?* — and returns the matching
:class:`~app.agent.workout_library.WorkoutEntry` (id + name) or ``None``.
It never invents content (S2/S6): the returned reference is always an
existing entry the agent may cite, never a synthesised workout.

Matching rule (spec T2, in order):

1. **Same sport.** The library's two sources serve different sport
   families: ``cycling`` entries serve the engine's cycling sports and
   ``gym`` sections serve the strength sports. The families are IMPORTED
   from ``app.engine.load`` — the engine's closed sport vocabulary is the
   single source of truth, so a new engine sport family is matched (or
   left unmatched) automatically and the mapping cannot drift.
2. **Zone overlap.** The workout's target zones (the verbatim source zone
   labels of its steps, mapped to the engine's zone keys) must overlap the
   session's zone keys. A workout that targets none of the session's
   zones is not a match; sessions without zone keys (``rest``) and the
   zone-less gym sections can therefore never match, honestly.
3. **Duration within the session's cap.** Only when the session has a cap
   (the planner fixes one for recovery days): the workout's duration must
   fit. The parser owns the accounting: :attr:`Session.total_duration_s`
   is the written step sum when every ``Repetir N veces`` marker is one the
   source's convention settles (``times == 1`` — the one count every
   consistent reading agrees means "this block occurs once, as written"),
   and ``None`` when any marker or marker-shaped line leaves the total
   ambiguous. The honest rule is unchanged: claiming "within the cap" from
   an UNKNOWN total would be exactly the kind of unsourced claim S2
   forbids, so a session whose total is ``None`` never matches a capped
   one.

When several entries match, the FIRST in the library's deterministic parse
order wins, so the same decided session always references the same
workout. No match is ``None`` — an honest empty answer, never a guess.
"""

from __future__ import annotations

from app.agent.workout_library import (
    Session,
    WorkoutEntry,
    WorkoutLibrary,
)
from app.engine.load import _CYCLING_SPORTS, _STRENGTH_SPORTS

__all__ = ["match_library_workout"]


# The engine's zone keys are the planner's vocabulary ("Z1".."Z5c"); the
# library stores the source files' VERBATIM labels. This table is the
# translation between the two, one row per label of the documented
# ZONE_VOCABULARY — 5A/5B/5C map to the Friel HR keys Z5a/Z5b/Z5c the
# engine's hr_zones() prints.
_ENGINE_ZONE_KEY_BY_LABEL: dict[str, str] = {
    "Zone 1: Recovery": "Z1",
    "Zone 2: Aerobic": "Z2",
    "Zone 3: Tempo": "Z3",
    "Zone 4: SubThreshold": "Z4",
    "Zone 5A: SuperThreshold": "Z5a",
    "Zone 5B: Aerobic Capacity": "Z5b",
    "Zone 5C: Anaerobic Capacity": "Z5c",
}

# Imported, not copied: the engine's private family sets are the single
# source of truth for which sports each library source serves. If the
# engine grows a cycling or strength sport, the adapter follows it.
_SOURCE_SPORTS: dict[str, frozenset[str]] = {
    "cycling": _CYCLING_SPORTS,
    "gym": _STRENGTH_SPORTS,
}


def _serves_sport(entry: WorkoutEntry, sport: str) -> bool:
    """Whether the entry's source serves the engine sport (rule 1)."""
    return sport in _SOURCE_SPORTS.get(entry.source, frozenset())


def _workout_zone_keys(entry: WorkoutEntry) -> frozenset[str]:
    """The engine zone keys the entry's steps target (rule 2).

    Cycling sessions carry the verbatim source labels on their steps; gym
    sections carry none (they have no zone vocabulary), so a gym entry's
    key set is empty and can never overlap a zoned session.
    """
    content = entry.content
    if not isinstance(content, Session):
        return frozenset()
    keys: set[str] = set()
    for block in content.blocks:
        for item in block.items:
            zone = getattr(item, "zone", None)
            if zone is not None:
                key = _ENGINE_ZONE_KEY_BY_LABEL.get(zone)
                # An unknown label maps to nothing rather than guessing a
                # zone key (the parser surfaces such labels as issues).
                if key is not None:
                    keys.add(key)
    return frozenset(keys)


def _workout_duration_known_and_within(
    entry: WorkoutEntry, duration_cap_s: float
) -> bool:
    """Whether the entry's duration is known and fits the cap (rule 3).

    The parser owns the duration accounting:
    :attr:`Session.total_duration_s` is the written step sum when every
    ``Repetir`` marker is one the source's convention settles
    (``times == 1``) and ``None`` when any marker or marker-shaped line
    leaves the total ambiguous. The honest-None rule is preserved exactly:
    an UNKNOWN total can never honestly fit a cap (a lower bound would be
    an unsourced claim), so such sessions still never match a capped one.
    """
    content = entry.content
    if not isinstance(content, Session):
        return False
    total_s = content.total_duration_s
    if total_s is None:
        # Unknown total: refuse rather than guess (honest-None rule).
        return False
    return total_s <= duration_cap_s


def match_library_workout(
    library: WorkoutLibrary,
    *,
    sport: str | None,
    zone_keys: tuple[str, ...] | None,
    duration_cap_s: float | None,
) -> WorkoutEntry | None:
    """The first library workout matching the decided session, or ``None``.

    ``sport`` is the session's concrete engine sport (``None`` matches
    nothing — without a sport there is no library side); ``zone_keys`` are
    the planner's zone KEYS (``None`` for a rest session, which references
    no content); ``duration_cap_s`` is the session's duration cap, when it
    has one. See the module docstring for the ordered rule and why no
    match is ``None`` rather than a best-effort reference.
    """
    if sport is None or zone_keys is None:
        return None
    wanted = frozenset(zone_keys)
    for entry in library.entries:
        if not _serves_sport(entry, sport):
            continue
        if not (_workout_zone_keys(entry) & wanted):
            continue
        if duration_cap_s is not None and not _workout_duration_known_and_within(
            entry, duration_cap_s
        ):
            continue
        return entry
    return None
