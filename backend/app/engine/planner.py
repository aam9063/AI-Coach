"""One-day training-session planner: the ordered decision table (T1, specs
S4/S5 of ``odd/tasks/workout-planner.md``).

This module is part of the pure-function science engine (``app/engine/``):
no I/O, no DB, no network, no clock, no imports of ``app.db`` /
``app.ingest`` / ``app.core`` / ``app.agent`` (PROJECT_BRIEF sections 6 and
14). Every input arrives as an explicit plain value — the readiness
verdict, the day's TSB, whether the zone thresholds are configured, the
dominant sport's share of the last 7 days and the athlete's explicitly
requested sport — and the result is a frozen dataclass. Persistence,
settings reads and "today" belong to the caller (the ``plan_workout`` tool,
T3), never here.

The engine decides, the LLM explains (S4)
-----------------------------------------
``plan_session`` is the whole decision: it returns the finished session
(sport, human session type, zone KEYS, duration, kind) plus one factual
``reason`` sentence quoting the numbers it used. The tool layer (T3) hands
that sentence to the model verbatim; the model explains it and may not
alter it. Quoting the numbers in ``reason`` is what lets the §13
numeric-faithfulness gate check the model's reply against the tool output,
so every reason below states the quantities that produced the decision.

Insufficient data is a result, never a plan (S5)
------------------------------------------------
Same contract as ``get_readiness`` / ``get_load_status``: when a required
input is absent the planner returns ``status="insufficient_data"`` with
``missing`` naming exactly what to connect or wait for — never a
fabricated session, never a substituted default. Row 5 of the decision
table is evaluated BEFORE the session rows, so no session can be produced
from incomplete evidence.

Constants and their provenance
------------------------------
- ``RECOVERY_MAX_DURATION_S = 3600``: the spec's 60-minute recovery cap
  (decision-table row 1). A recovery day limits stimulus, it does not
  chase load; the cap is a ceiling, not a target, and the numeric zone
  values still come from the athlete's own thresholds (T3).
- ``SPORT_SHARE_THRESHOLD = 0.70``: the spec's imbalance threshold — when
  one sport covers more than 70% of the last 7 days of activity, the OTHER
  sport is preferred unless the athlete named one explicitly (row 4).
  Exactly at 0.70 the rule does NOT fire (strict comparison, the ZON-9
  margin precedent used across the engine).
- The overreached-TSB band is REUSED, not redefined: the band is TSB
  strictly below ``DEFAULT_TSB_VERY_NEGATIVE`` (-10.0) imported from
  ``app.engine.readiness`` — the same threshold the multi-signal warning
  rule (RID-3) already treats as "very negative" TSB, with its documented
  OWNER CHOICE provenance there (TrainingPeaks/Friel convention: below
  about -10 is the onset of the high-fatigue zone). One band, one source
  of truth: the planner must not drift from the readiness rule that fires
  on the same number. Exactly at -10.0 is NOT overreached (strict, same
  precedent).

Sport naming (deliberate limitation of the pure module)
-------------------------------------------------------
The planner never invents a sport name (S2). ``sport`` in the result is
the athlete's EXPLICIT request, or ``None``. When the imbalance rule (row
4) prefers "the other sport", the module cannot name it — the activity
rows that define the sports live in the caller — so it returns
``sport=None`` and states the imbalance with its numbers in ``reason``;
the tool layer (T3), which holds the last-7-days rows, resolves the
concrete sport name. The spec's row 4 is under-specified on this point
("the OTHER sport" without a sport vocabulary); this implementation keeps
the spec's decision and leaves the name resolution to the caller rather
than guessing a sport here.

Zone keys, never numeric targets
--------------------------------
``zones`` carries stable zone KEYS (``"Z1"``..``"Z5"``). The numeric
min/max targets are attached by the tool layer (T3) from the athlete's
configured thresholds — this module has none of them (the caller only
reports whether they are configured), so emitting numbers here would be
invention, exactly what S2 forbids.
"""

import math
from dataclasses import dataclass
from typing import Final, Literal

from app.engine.readiness import DEFAULT_TSB_VERY_NEGATIVE

__all__ = [
    "RECOVERY_MAX_DURATION_S",
    "SPORT_SHARE_THRESHOLD",
    "PlanResult",
    "PlanSessionKind",
    "PlanStatus",
    "plan_session",
]


RECOVERY_MAX_DURATION_S: Final[int] = 3600
"""Recovery-session duration cap in seconds: the spec's 60-minute cap
(decision-table row 1). Provenance in the module docstring."""

SPORT_SHARE_THRESHOLD: Final[float] = 0.70
"""Dominant-sport share of the last 7 days above which the OTHER sport is
preferred (decision-table row 4). Strict comparison: exactly at 0.70 the
rule does not fire. Provenance in the module docstring."""

PlanStatus = Literal["ok", "insufficient_data"]
"""Result status: a decided session ("ok") or an explicit data gap (S5)."""

PlanSessionKind = Literal["recovery", "rest", "quality"]
"""Stable machine-readable kind of the decided session (decision table)."""


@dataclass(frozen=True, slots=True)
class PlanResult:
    """The finished one-day session decision (or the explicit data gap).

    ``status`` is ``"ok"`` when a session was decided and
    ``"insufficient_data"`` otherwise (S5). For ``"ok"``: ``sport`` is the
    athlete's explicit request or ``None`` (module docstring — the planner
    never invents a sport name), ``session_type`` is the human label,
    ``zones`` the stable zone KEYS (never numeric targets — T3 attaches
    the values from the athlete's thresholds), ``duration_s`` the decided
    duration in seconds or ``None`` when the table fixes none, and ``kind``
    the stable machine-readable kind. ``reason`` is the factual engine
    sentence the model may quote verbatim (S4). ``missing`` is empty for
    ``"ok"``; for ``"insufficient_data"`` every session field is ``None``
    and ``missing`` names exactly what to connect or wait for.
    """

    status: PlanStatus
    sport: str | None
    session_type: str | None
    zones: tuple[str, ...] | None
    duration_s: float | None
    kind: PlanSessionKind | None
    reason: str
    missing: tuple[str, ...]


_MISSING_READINESS: Final[str] = (
    "readiness verdict (Feature 5) — connect the wellness/HRV source or "
    "wait for today's readiness assessment"
)
"""What is missing when the readiness suggestion is unknown (S5)."""

_MISSING_TSB: Final[str] = (
    "today's TSB (Feature 4 daily_load) — sync activities or wait for the "
    "nightly load aggregation"
)
"""What is missing when the day's TSB is unknown (S5)."""

_MISSING_THRESHOLDS: Final[str] = (
    "zone thresholds — set the two intensity anchors athlete_ftp_w (FTP) "
    "and athlete_lthr_bpm (LTHR) in settings"
)
"""What is missing when the zone thresholds are unconfigured (S5): the two
anchors the zones of Feature 3 are derived from. The spec says "naming the
two thresholds to set" without naming them; FTP and LTHR are the owner's
configured bike/HR intensity anchors (``app.core.settings``), the pair the
Z1-Z3 keys of the decision table resolve against."""

_RECOVERY_SESSION_TYPE: Final[str] = "recovery ride"
_REST_SESSION_TYPE: Final[str] = "rest or mobility"
_QUALITY_SESSION_TYPE: Final[str] = "quality session"


def plan_session(
    *,
    readiness_suggests_reduce_intensity: bool | None,
    tsb: float | None,
    thresholds_configured: bool,
    sport_share: float | None,
    requested_sport: str | None,
) -> PlanResult:
    """Decide ONE training session from the ordered decision table.

    Inputs (all caller-supplied, never read here — module docstring):

    - ``readiness_suggests_reduce_intensity``: the readiness assessment's
      ``suggest_reduce_intensity`` verdict (Feature 5); ``None`` when the
      readiness verdict is unavailable.
    - ``tsb``: the day's Training Stress Balance from the load model
      (Feature 4); ``None`` when unknown.
    - ``thresholds_configured``: whether the owner's zone thresholds
      (Feature 3) are set; ``False`` means intensity cannot be expressed.
    - ``sport_share``: the dominant sport's share of the last 7 days of
      activity in [0, 1]; ``None`` when there is no activity mix to judge
      (row 4 is then skipped, per the spec's input table).
    - ``requested_sport``: the sport the athlete explicitly asked for, if
      any; it overrides the imbalance rule (row 4).

    The table, in order, first match wins (module docstring): row 5
    (missing inputs -> ``insufficient_data``) is evaluated FIRST so no
    session is ever produced from incomplete evidence; then row 1
    (reduce-intensity verdict -> recovery, Z1-Z2, capped at
    :data:`RECOVERY_MAX_DURATION_S`), row 2 (TSB strictly below
    ``DEFAULT_TSB_VERY_NEGATIVE`` from ``app.engine.readiness`` -> rest,
    no zones), row 3 (TSB neutral/positive AND readiness ok -> quality,
    Z3); row 4 (sport share strictly above :data:`SPORT_SHARE_THRESHOLD`)
    refines the sport of the session rows 1-3 chose. The reason sentence
    quotes the numbers each decision used (S4).

    Raises ``ValueError`` on a non-finite ``tsb`` or a non-finite /
    out-of-[0, 1] ``sport_share`` (engine convention: validate, never
    clamp or silently default).
    """
    if tsb is not None and not math.isfinite(tsb):
        raise ValueError(f"tsb must be finite, got {tsb!r}")
    if sport_share is not None:
        if not math.isfinite(sport_share):
            raise ValueError(f"sport_share must be finite, got {sport_share!r}")
        if not 0.0 <= sport_share <= 1.0:
            raise ValueError(
                "sport_share must be within [0, 1] (a share of the last 7 "
                f"days), got {sport_share!r}"
            )

    # --- row 5, evaluated FIRST: no session from incomplete evidence (S5) ---
    # Thresholds are deliberately NOT part of this gate: they are checked below,
    # only for the rows that NAME ZONES. A rest prescription expresses no
    # intensity, so demanding FTP/LTHR before telling an overreached athlete to
    # rest would invert the product -- the engine would answer "set your FTP" to
    # someone who must not train.
    missing: list[str] = []
    if readiness_suggests_reduce_intensity is None:
        missing.append(_MISSING_READINESS)
    if tsb is None:
        missing.append(_MISSING_TSB)
    if missing:
        return PlanResult(
            status="insufficient_data",
            sport=None,
            session_type=None,
            zones=None,
            duration_s=None,
            kind=None,
            reason=f"No session can be planned yet: {'; '.join(missing)}.",
            missing=tuple(missing),
        )

    # --- rows 1-3: the session itself (readiness and tsb are now known) ----
    assert readiness_suggests_reduce_intensity is not None
    assert tsb is not None
    if readiness_suggests_reduce_intensity:
        kind: PlanSessionKind = "recovery"
        session_type = _RECOVERY_SESSION_TYPE
        zones: tuple[str, ...] | None = ("Z1", "Z2")
        duration_s: float | None = float(RECOVERY_MAX_DURATION_S)
        base_reason = (
            "Readiness suggests reducing intensity, so the engine "
            "prescribes a recovery ride in zones Z1-Z2 capped at "
            f"{RECOVERY_MAX_DURATION_S} s (60 min)."
        )
    elif tsb < DEFAULT_TSB_VERY_NEGATIVE:
        kind = "rest"
        session_type = _REST_SESSION_TYPE
        zones = None
        duration_s = None
        base_reason = (
            f"TSB {tsb:+.1f} sits below the very-negative threshold "
            f"{DEFAULT_TSB_VERY_NEGATIVE:+.1f} (overreached band), so the "
            "engine prescribes rest or mobility."
        )
    else:
        kind = "quality"
        session_type = _QUALITY_SESSION_TYPE
        zones = ("Z3",)
        duration_s = None
        base_reason = (
            f"TSB {tsb:+.1f} is neutral or positive and readiness shows no "
            "reduce-intensity signal, so the engine prescribes a quality "
            "session in zone Z3."
        )

    # Thresholds gate only the zone-bearing rows (see the row-5 note above).
    if zones is not None and not thresholds_configured:
        return PlanResult(
            status="insufficient_data",
            sport=None,
            session_type=None,
            zones=None,
            duration_s=None,
            kind=None,
            reason=f"No session can be planned yet: {_MISSING_THRESHOLDS}.",
            missing=(_MISSING_THRESHOLDS,),
        )

    # --- row 4: sport refinement of the session rows 1-3 chose -------------
    sport: str | None = None
    if requested_sport is not None:
        # The athlete named a sport explicitly: the request always wins.
        sport = requested_sport
        reason = base_reason
    elif sport_share is not None and sport_share > SPORT_SHARE_THRESHOLD:
        # The dominant sport is overrepresented; the other sport is
        # preferred, but this module cannot name it (the activity rows live
        # in the caller) — state the imbalance with its numbers instead.
        reason = (
            f"{base_reason.rstrip('.')} (sport share {sport_share:.2f} of "
            f"the last 7 days is above the {SPORT_SHARE_THRESHOLD:.2f} "
            "balance threshold, so the engine prefers the other sport)."
        )
    else:
        reason = base_reason

    return PlanResult(
        status="ok",
        sport=sport,
        session_type=session_type,
        zones=zones,
        duration_s=duration_s,
        kind=kind,
        reason=reason,
        missing=(),
    )
