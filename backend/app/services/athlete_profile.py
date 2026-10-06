"""Threshold confirmation flow (ZON-10, brief §7.3): record what the athlete
decided about a :class:`app.engine.zones.ThresholdChangeProposal`.

§7.3: threshold updates are PROPOSED, never applied silently; the athlete
confirms; history is stored. The pure engine (``app.engine.zones``) is
incapable of applying anything (ZON-9); THIS module is the caller-side half
of that contract — it applies a change only after an explicit acceptance
and records every decision in the append-only
``athlete_threshold_history`` (ZON-10).

Feature-6 seam (WhatsApp): these functions are CHANNEL-AGNOSTIC. Feature 6
will parse the athlete's WhatsApp reply (interactive proposal flow) and
call :func:`record_threshold_acceptance` / :func:`record_threshold_decline`
with the already-resolved proposal and the actor identifier
(``confirmed_by`` e.g. ``"owner_whatsapp"``). Nothing here sends or
receives messages — the thin flow records the owner's decision.

Guards (both functions REFUSE, never partially apply):

- any input that is not an explicit ``ThresholdChangeProposal`` instance —
  including a :class:`app.engine.zones.NoThresholdChange` — is refused;
- a proposal whose ``status`` is not the single-valued literal
  ``"proposal_not_applied"`` is refused (defence in depth: the type system
  already pins it, this re-checks at runtime);
- acceptance refuses an unknown ``source`` key (provenance must reuse the
  engine's vocabulary, see :data:`PROFILE_SOURCE_KEYS`);
- acceptance refuses a STALE proposal: when the profile's current value for
  the metric no longer matches the proposal's ``prior_value``, applying
  would silently overwrite an unrelated newer value — refused instead.
  This also makes re-recording one proposal impossible (after the first
  acceptance the current value IS the proposed value).

All writes flush without committing; the caller owns the transaction.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import AthleteProfileRow, AthleteThresholdHistoryRow
from app.engine.zones import (
    FTP_SOURCE_KEYS,
    THRESHOLD_METRIC_KEYS,
    ThresholdChangeProposal,
    ThresholdMetricKey,
)

__all__ = [
    "PROFILE_SOURCE_KEYS",
    "record_threshold_acceptance",
    "record_threshold_decline",
]

PROFILE_SOURCE_KEYS: Final[tuple[str, ...]] = (
    *FTP_SOURCE_KEYS,
    "fit_critical_power",
    "fit_critical_speed",
    "css_from_time_trials",
)
"""Valid provenance keys for a profile threshold value.

REUSES the engine's own source vocabulary: the three
:data:`app.engine.zones.FTP_SOURCE_KEYS` (``manual`` / ``cp_derived`` /
``twenty_min_power``) plus the name of the engine function that produced a
fitted value. ``manual`` is valid for every metric (an owner-entered
value). Stored in ``athlete_profile.*_source`` and on accepted history
rows."""

# metric -> (profile value column, profile source column)
_PROFILE_METRIC_COLUMNS: Final[dict[ThresholdMetricKey, tuple[str, str]]] = {
    "ftp_watts": ("ftp_watts", "ftp_source"),
    "cp_watts": ("cp_watts", "cp_source"),
    "cs_mps": ("cs_mps", "cs_source"),
    "css_mps": ("css_mps", "css_source"),
}

# Tolerance for the stale-proposal comparison: the prior value round-trips
# through a DOUBLE column, so an exact equality would be brittle. 1e-12
# relative is far tighter than any real threshold difference (mirrors the
# engine's own boundary tolerance, _same_boundary).
_PRIOR_VALUE_REL_TOL: Final = 1e-12


def _require_explicit_proposal(outcome: object) -> ThresholdChangeProposal:
    """Refuse anything that is not an explicit ``proposal_not_applied`` proposal.

    A :class:`~app.engine.zones.NoThresholdChange` is an explicit NO-CHANGE
    outcome — there is nothing to accept or decline. Any other object (a
    string, ``None``, a foreign type) is equally refused. A
    :class:`~app.engine.zones.ThresholdChangeProposal` carrying a status
    other than ``"proposal_not_applied"`` (runtime-crafted; the Literal
    type already forbids it statically) is refused too: only an explicit,
    still-unapplied proposal may drive a decision.
    """
    if not isinstance(outcome, ThresholdChangeProposal):
        raise ValueError(
            "refusing non-proposal input: threshold decisions can only be "
            "recorded against an explicit ThresholdChangeProposal with "
            "status 'proposal_not_applied'; got "
            f"{type(outcome).__name__!r} (a NoThresholdChange is an explicit "
            "no-change outcome and cannot be accepted or declined)"
        )
    if outcome.status != "proposal_not_applied":
        raise ValueError(
            "refusing proposal with status "
            f"{outcome.status!r}: only an explicit 'proposal_not_applied' "
            "proposal can be accepted or declined"
        )
    if outcome.metric not in THRESHOLD_METRIC_KEYS:
        raise ValueError(  # pragma: no cover - the engine validates the metric
            f"unknown threshold metric {outcome.metric!r}"
        )
    return outcome


def _current_value(profile: AthleteProfileRow | None, metric: ThresholdMetricKey) -> float | None:
    """The profile's current value for ``metric`` (None when no profile)."""
    if profile is None:
        return None
    value_column, _ = _PROFILE_METRIC_COLUMNS[metric]
    value = getattr(profile, value_column)
    return None if value is None else float(value)


async def record_threshold_acceptance(
    session: AsyncSession,
    *,
    proposal: ThresholdChangeProposal,
    athlete_id: int = 1,
    source: str,
    confirmed_by: str,
    engine_version: str,
    recorded_at: datetime | None = None,
    intervals_athlete_id: str | None = None,
) -> tuple[AthleteProfileRow, AthleteThresholdHistoryRow]:
    """Record the athlete's ACCEPTANCE of a threshold proposal (ZON-10).

    Guards (``ValueError``, nothing partially applied): the input must be
    an explicit :class:`~app.engine.zones.ThresholdChangeProposal` with
    status ``"proposal_not_applied"`` (a
    :class:`~app.engine.zones.NoThresholdChange` or any other object is
    refused — see :func:`_require_explicit_proposal`); ``source`` must be
    one of :data:`PROFILE_SOURCE_KEYS` (the engine's source vocabulary);
    and the proposal must not be STALE — when the profile already holds a
    current value for the metric that differs from the proposal's
    ``prior_value`` (beyond float tolerance), the proposal describes an
    outdated baseline and is refused, which also makes re-recording the
    same proposal impossible.

    Effect on acceptance:

    - the profile's value for ``proposal.metric`` becomes
      ``proposal.proposed_value`` with provenance ``source`` (the other
      metrics are preserved), stamped with ``engine_version`` and
      ``updated_at = recorded_at``;
    - one APPEND-ONLY history row is written with the prior value, the new
      value, the proposal's evidence, ``confirmed_by`` and the
      ``engine_version``.

    Flushes without committing; the caller owns the transaction.
    Returns ``(updated_profile, history_row)``.

    Feature-6 seam: the WhatsApp interaction (Feature 6) will resolve the
    athlete's reply to a proposal and call this function; nothing here is
    channel-specific.
    """
    checked = _require_explicit_proposal(proposal)
    if source not in PROFILE_SOURCE_KEYS:
        raise ValueError(
            f"unknown threshold source {source!r}: provenance must be one "
            f"of {PROFILE_SOURCE_KEYS} (the engine's source keys)"
        )
    stamp = recorded_at if recorded_at is not None else datetime.now(UTC)

    result = await session.execute(
        select(AthleteProfileRow).where(AthleteProfileRow.athlete_id == athlete_id)
    )
    profile = result.scalar_one_or_none()
    current = _current_value(profile, checked.metric)
    if current is not None and not math.isclose(
        current, checked.prior_value, rel_tol=_PRIOR_VALUE_REL_TOL
    ):
        raise ValueError(
            f"refusing stale proposal for {checked.metric!r}: the profile's "
            f"current value {current!r} no longer matches the proposal's "
            f"prior value {checked.prior_value!r}; regenerate the proposal "
            "against the current value"
        )

    value_column, _ = _PROFILE_METRIC_COLUMNS[checked.metric]
    updated = await repository.upsert_athlete_profile(
        session,
        athlete_id=athlete_id,
        intervals_athlete_id=(
            intervals_athlete_id
            if intervals_athlete_id is not None
            else (profile.intervals_athlete_id if profile is not None else None)
        ),
        # Preserve every metric not being accepted (the upsert clears
        # unpassed columns by convention — pass the full current set).
        ftp_watts=(
            checked.proposed_value
            if checked.metric == "ftp_watts"
            else (profile.ftp_watts if profile is not None else None)
        ),
        ftp_source=(
            source
            if checked.metric == "ftp_watts"
            else (profile.ftp_source if profile is not None else None)
        ),
        cp_watts=(
            checked.proposed_value
            if checked.metric == "cp_watts"
            else (profile.cp_watts if profile is not None else None)
        ),
        cp_source=(
            source
            if checked.metric == "cp_watts"
            else (profile.cp_source if profile is not None else None)
        ),
        w_prime_joules=profile.w_prime_joules if profile is not None else None,
        cs_mps=(
            checked.proposed_value
            if checked.metric == "cs_mps"
            else (profile.cs_mps if profile is not None else None)
        ),
        cs_source=(
            source
            if checked.metric == "cs_mps"
            else (profile.cs_source if profile is not None else None)
        ),
        d_prime_meters=profile.d_prime_meters if profile is not None else None,
        css_mps=(
            checked.proposed_value
            if checked.metric == "css_mps"
            else (profile.css_mps if profile is not None else None)
        ),
        css_source=(
            source
            if checked.metric == "css_mps"
            else (profile.css_source if profile is not None else None)
        ),
        engine_version=engine_version,
        updated_at=stamp,
    )
    if profile is not None:
        # The upsert's RETURNING row merges into the identity-mapped object
        # loaded by the select above WITHOUT overwriting its loaded
        # attributes; refresh so the returned profile reflects the update.
        await session.refresh(updated)
    assert getattr(updated, value_column) == checked.proposed_value

    history = await repository.append_threshold_history(
        session,
        athlete_id=athlete_id,
        metric=checked.metric,
        decision="accepted",
        prior_value=checked.prior_value,
        new_value=checked.proposed_value,
        proposed_value=checked.proposed_value,
        evidence=checked.evidence,
        confirmed_by=confirmed_by,
        source=source,
        engine_version=engine_version,
        recorded_at=stamp,
    )
    return updated, history


async def record_threshold_decline(
    session: AsyncSession,
    *,
    proposal: ThresholdChangeProposal,
    athlete_id: int = 1,
    confirmed_by: str,
    engine_version: str,
    recorded_at: datetime | None = None,
) -> AthleteThresholdHistoryRow:
    """Record the athlete's DECLINE of a threshold proposal (ZON-10).

    Same explicitness guards as :func:`record_threshold_acceptance` (a
    :class:`~app.engine.zones.NoThresholdChange` or a wrong-status
    proposal is refused). The profile is LEFT UNTOUCHED — the current
    value and its provenance stay exactly as they are; only the append-only
    history records the decision (``decision="declined"``, ``new_value``
    NULL, ``proposed_value`` = what was refused, plus prior value,
    evidence, ``confirmed_by`` and ``engine_version``).

    Flushes without committing; returns the history row.
    """
    checked = _require_explicit_proposal(proposal)
    stamp = recorded_at if recorded_at is not None else datetime.now(UTC)
    return await repository.append_threshold_history(
        session,
        athlete_id=athlete_id,
        metric=checked.metric,
        decision="declined",
        prior_value=checked.prior_value,
        new_value=None,
        proposed_value=checked.proposed_value,
        evidence=checked.evidence,
        confirmed_by=confirmed_by,
        source=None,
        engine_version=engine_version,
        recorded_at=stamp,
    )
