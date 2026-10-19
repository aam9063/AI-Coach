"""Post-activity review composer + idempotent review pass (SCH-7, §11).

**The trigger, defined honestly.** §11 asks for a review "triggered when a
new activity is ingested", but the sync CANNOT report newness: the upsert
path counts a re-synced (unchanged) activity as ``activities_synced`` —
the same limitation already recorded for the every-30-minutes recompute in
:mod:`app.scheduler.celery_app`. A trigger keyed on the sync's return
would therefore fire on every sync without distinguishing anything, and a
naïve "review on activities_synced > 0" would be meaningless. The honest
implementation is an IDEMPOTENT REVIEW PASS:

- each run finds the activities that STARTED within the look-back window
  (``Settings.post_activity_review_lookback_hours``, default 48 h, OWNER
  CHOICE) and have NO review marker yet in the ``message_log`` audit
  trail, and reviews them;
- the marker IS the outbound audit row the pipeline discipline already
  requires (payload ``kind="post_activity_review"`` + ``activity_id``,
  written BEFORE the Twilio call) — no extra table, no schema change;
- because the marker is the audit trail itself, the pass can fire any
  number of times (beat, manual retry, a Twilio failure re-run) and an
  activity is reviewed EXACTLY ONCE — a failed send costs at most one
  missed review, never a duplicated one;
- the look-back bound keeps a first 30-day backfill from becoming 30
  proactive messages: old history is never "new activity" for the owner.

**Content.** A short analysis from PERSISTED data only — the activity
summary row (sport, name, duration, distance, owner-entered RPE when the
source carries one), the day's ``daily_load`` ``combined`` row and the
session's ``session_durability`` engine row — plus the request for the
RPE/sensations that feeds ``log_subjective``: the review only ASKS (with
the scales stated); the athlete's reply goes to the agent, which logs it.
A missing engine output is stated AS missing and never fabricated as zero. The
common case is not a missing output, though: the recompute runs at :25 and :55, a
sync at :00 and :30, and this pass at :05 and :35, so a freshly synced activity
routinely reaches the review BEFORE its ``daily_load`` row can exist. Reviewing
it anyway sent "TSS: not available yet" every single time and, because the audit
row is the once-only marker, burned the one review that activity would ever get.
Such an activity is therefore DEFERRED -- nothing composed, NO marker written --
and the next pass reviews it WITH its numbers. Only once the activity is older
than ``LOAD_SETTLE_WINDOW`` does a missing output mean the engine genuinely has
nothing, and then the honest wording is the right answer.

The suggested adjustment appears ONLY when the persisted readiness
snapshot for the activity's day says ``suggest_reduce_intensity`` with a
suggestion — the ENGINE's multi-signal verdict, verbatim (§7.4). ACWR is
not an input of the composer at all. No citations, no chart (OWNER
DECISION 2026-10-14), at most ~1,000 characters.

**Send discipline — exactly the morning brief's** (:mod:
`app.scheduler.morning_brief`, reused, not duplicated): free-form inside
the 24 h free-form window, the configured template
(``post_activity_review_template_sid``) outside it, a loud skip when no
template is configured, and the audit row committed BEFORE the Twilio
call. A deliberate skip also MARKS the activity: the skip is the recorded
outcome (audited with its reason) and the pass never re-attempts a send
that cannot work — the audit trail stays bounded.

The activity's "day" for engine outputs is its LOCAL date (parsed from
``start_time_local``, falling back to the UTC start date): daily load and
readiness are local-time concepts, like everywhere else in the project.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.pipeline import AGENT_VERSION, make_twilio_client
from app.core.settings import Settings, get_settings
from app.db.models import (
    ActivityRow,
    DailyLoadRow,
    MessageLogRow,
    ReadinessSnapshotRow,
    SessionDurabilityRow,
)
from app.db.session import create_db_engine, make_session_factory
from app.scheduler.morning_brief import (
    SessionFactory,
    brief_recipient,
    decide_send_mode,
    last_inbound_message_at,
)

logger = logging.getLogger(__name__)

# The review marker, carried by the outbound audit row's payload. The
# marker is the audit row itself: no extra table, and "reviewed once" is
# answerable from the trail alone.
REVIEW_MARKER_KIND = "post_activity_review"

# Default look-back for the pass (mirrored in Settings as
# ``post_activity_review_lookback_hours``; the setting is authoritative).
DEFAULT_LOOKBACK_HOURS = 48

# How long an activity may wait for its ``daily_load`` row before the review
# gives up on it and states the output as missing.
#
# The load recompute runs at :25 and :55 while this pass fires at :05 and :35, so
# an activity synced at :30 reaches the review at :35 -- about twenty minutes
# BEFORE its load can exist. Measured 2026-10-19: that is the common path, not
# the edge case the docstring above imagined, and because the audit row is the
# once-only marker, the athlete read "TSS: not available yet" in EVERY review and
# that activity was never reviewed with its numbers. Two hours covers a full
# sync-plus-recompute cycle with room to spare; past it the engine genuinely has
# no output and the honest missing-output wording is the right answer.
LOAD_SETTLE_WINDOW = dt.timedelta(hours=2)


# ---------------------------------------------------------------------------
# The persisted data one review is composed from
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DurabilityFacts:
    """The session's persisted durability engine output (Feature 5)."""

    status: str
    ef_first_half: float | None
    ef_second_half: float | None
    decoupling_pct: float | None
    within_reference_band: bool | None


@dataclass(frozen=True)
class ActivityFacts:
    """One activity plus its persisted engine outputs (all optional)."""

    activity_id: int
    sport: str
    name: str
    start_time: dt.datetime
    duration_s: int | None
    distance_m: float | None
    rpe: float | None
    # daily_load combined row for the activity's LOCAL date.
    tss: float | None
    tsb: float | None
    # session_durability row for the activity.
    durability: DurabilityFacts | None
    # The engine's multi-signal verdict for the day (§7.4), verbatim.
    engine_suggestion: str | None


def _activity_local_date(row: ActivityRow) -> dt.date:
    """The activity's local date; daily load is a local-time concept."""
    if row.start_time_local:
        try:
            return dt.datetime.fromisoformat(row.start_time_local).date()
        except ValueError:
            pass
    return row.start_time.date()


def compose_post_activity_review(facts: ActivityFacts, *, athlete_name: str = "") -> str:
    """Compose the review text from the persisted facts (PURE, ≤ ~1,000 chars).

    Missing engine outputs are stated AS missing — never zero, never a
    fabricated number (§9.3 discipline). The RPE request names the scales
    so the athlete's reply can be logged through ``log_subjective``.
    """
    greeting = f"Hola, {athlete_name}." if athlete_name else "Hola."
    lines = [greeting, "Sesión registrada:"]
    summary = f"{facts.name or facts.sport} ({facts.sport})"
    if facts.duration_s:
        summary += f" — {facts.duration_s // 60} min"
    if facts.distance_m:
        summary += f", {facts.distance_m / 1000.0:.1f} km"
    lines.append(summary)
    if facts.rpe is not None:
        lines.append(f"RPE registrado por la fuente: {facts.rpe:.0f}/10")
    if facts.tss is not None and facts.tsb is not None:
        lines.append(f"Carga del día: TSS {facts.tss:.0f} · TSB {facts.tsb:+.1f}")
    else:
        lines.append(
            "La carga de esta sesión aún no está computada por el motor."
        )
    if facts.durability is not None:
        if facts.durability.status == "ok" and (
            facts.durability.ef_first_half is not None
            and facts.durability.ef_second_half is not None
        ):
            band = ""
            if facts.durability.within_reference_band is True:
                band = " (dentro de banda)"
            elif facts.durability.within_reference_band is False:
                band = " (fuera de banda)"
            decoupling = (
                f" · desacople {facts.durability.decoupling_pct:.1f}%"
                if facts.durability.decoupling_pct is not None
                else ""
            )
            lines.append(
                f"Durabilidad: EF {facts.durability.ef_first_half:.2f} → "
                f"{facts.durability.ef_second_half:.2f}{decoupling}{band}"
            )
        else:
            lines.append(
                "Durabilidad: el motor no la considera constante; EF no calculado."
            )
    if facts.engine_suggestion is not None:
        lines.append("")
        lines.append(f"Sugerencia: {facts.engine_suggestion}")
    lines.append("")
    lines.append(
        "¿Cómo fue? Respóndeme con tu RPE (1-10), tu fatiga (1-10) y las "
        "sensaciones de la sesión, y lo registro."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The idempotent pass: find unreviewed, gather, compose, send/skip
# ---------------------------------------------------------------------------


async def find_unreviewed_activity_ids(
    session: AsyncSession,
    *,
    now: dt.datetime,
    athlete_id: int = 1,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> list[int]:
    """Ids of recently started activities without a review marker.

    Candidates: ``activity`` rows with ``start_time >= now - lookback``.
    Reviewed: an outbound ``message_log`` row carrying the review marker
    (``kind`` + ``activity_id``) for that id — the marker is the audit
    row the send discipline writes anyway, so the trail answers
    "was this activity reviewed, and what was sent?" by itself.
    """
    cutoff = now - dt.timedelta(hours=lookback_hours)
    candidates = (
        (
            await session.execute(
                select(ActivityRow.id)
                .where(
                    ActivityRow.start_time >= cutoff,
                )
                .order_by(ActivityRow.start_time)
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return []
    reviewed_rows = (
        (
            await session.execute(
                select(MessageLogRow.payload["activity_id"].astext).where(
                    MessageLogRow.direction == "outbound",
                    MessageLogRow.payload["kind"].astext == REVIEW_MARKER_KIND,
                    MessageLogRow.payload["activity_id"].astext.in_(
                        [str(candidate) for candidate in candidates]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    reviewed = {int(text) for text in reviewed_rows if text is not None}
    return [candidate for candidate in candidates if candidate not in reviewed]


async def gather_review_data(
    session_factory: SessionFactory,
    *,
    activity_id: int,
    athlete_id: int = 1,
) -> ActivityFacts | None:
    """Read the persisted activity row + its engine outputs (SCH-7).

    Returns ``None`` when the id is unknown (nothing to compose from —
    the caller reports it, never invents facts).
    """
    async with session_factory() as session:
        activity = (
            (
                await session.execute(
                    select(ActivityRow).where(ActivityRow.id == activity_id)
                )
            )
            .scalars()
            .first()
        )
        if activity is None:
            return None
        day = _activity_local_date(activity)
        load = (
            (
                await session.execute(
                    select(DailyLoadRow).where(
                        DailyLoadRow.athlete_id == athlete_id,
                        DailyLoadRow.date == day,
                        DailyLoadRow.sport == "combined",
                    )
                )
            )
            .scalars()
            .first()
        )
        snapshot = (
            (
                await session.execute(
                    select(ReadinessSnapshotRow).where(
                        ReadinessSnapshotRow.athlete_id == athlete_id,
                        ReadinessSnapshotRow.date == day,
                    )
                )
            )
            .scalars()
            .first()
        )
        durability = (
            (
                await session.execute(
                    select(SessionDurabilityRow).where(
                        SessionDurabilityRow.activity_id == activity.id
                    )
                )
            )
            .scalars()
            .first()
        )
        return ActivityFacts(
            activity_id=activity.id,
            sport=activity.type,
            name=activity.name,
            start_time=activity.start_time,
            duration_s=activity.duration_s,
            distance_m=activity.distance_m,
            rpe=activity.rpe,
            tss=load.tss if load is not None else None,
            tsb=load.tsb if load is not None else None,
            durability=(
                DurabilityFacts(
                    status=durability.status,
                    ef_first_half=durability.ef_first_half,
                    ef_second_half=durability.ef_second_half,
                    decoupling_pct=durability.decoupling_pct,
                    within_reference_band=durability.within_reference_band,
                )
                if durability is not None
                else None
            ),
            # The engine's multi-signal verdict ONLY (§7.4): suggestion
            # exists on the snapshot only when the rule fired; both are
            # checked so a partial row can never leak a lone reason.
            engine_suggestion=(
                snapshot.suggestion
                if snapshot is not None and snapshot.suggest_reduce_intensity
                else None
            ),
        )


async def run_post_activity_review(
    *,
    settings: Settings,
    session_factory: SessionFactory,
    twilio_client: Any,
    now: dt.datetime | None = None,
    athlete_id: int = 1,
) -> dict[str, Any]:
    """Review every not-yet-reviewed recent activity (or honestly skip it).

    Every dependency is injectable (settings, session factory, Twilio
    client, the clock); the tests pass fakes and a fixed ``now``. Returns
    a JSON-serializable summary: how many activities were reviewed, how
    many sends/skips that produced, and per-activity detail.
    """
    moment = now if now is not None else dt.datetime.now(dt.UTC)
    lookback = settings.post_activity_review_lookback_hours
    async with session_factory() as session:
        activity_ids = await find_unreviewed_activity_ids(
            session, now=moment, athlete_id=athlete_id, lookback_hours=lookback
        )
    details: list[dict[str, Any]] = []
    sent = 0
    skipped = 0
    recipient = brief_recipient(settings)
    for activity_id in activity_ids:
        facts = await gather_review_data(
            session_factory, activity_id=activity_id, athlete_id=athlete_id
        )
        if facts is None:
            details.append(
                {
                    "activity_id": activity_id,
                    "mode": "skip",
                    "reason": (
                        f"activity {activity_id} not found; nothing composed, "
                        "nothing marked"
                    ),
                }
            )
            skipped += 1
            continue

        # Defer instead of reviewing without the numbers (see
        # LOAD_SETTLE_WINDOW): send nothing and write NO marker, so the next
        # pass picks this activity up WITH its daily_load row. Burn the marker
        # now and the once-only review is gone for good.
        started_at = facts.start_time
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=dt.UTC)
        if facts.tss is None and moment - started_at < LOAD_SETTLE_WINDOW:
            details.append(
                {
                    "activity_id": activity_id,
                    "mode": "defer",
                    "reason": (
                        "daily_load row not computed yet; nothing composed, "
                        "nothing marked, next pass retries"
                    ),
                }
            )
            skipped += 1
            continue

        async with session_factory() as session:
            last_inbound = await last_inbound_message_at(session, sender=recipient)
        decision = decide_send_mode(
            now=moment,
            last_inbound_at=last_inbound,
            template_sid=settings.post_activity_review_template_sid,
            template_setting_label="POST_ACTIVITY_REVIEW_TEMPLATE_SID",
        )
        review = compose_post_activity_review(facts, athlete_name=settings.athlete_name)
        trace_id = uuid4().hex

        # The audit row doubles as the review marker (kind + activity_id)
        # and is committed BEFORE the Twilio call: a failed send costs at
        # most one missed review, never a duplicated one. A deliberate
        # skip marks the activity too: the skip is the recorded outcome
        # and the pass never re-attempts a send that cannot work.
        payload: dict[str, Any] = {
            "to": recipient,
            "part": 1,
            "total": 1,
            "mode": decision.mode,
            "kind": REVIEW_MARKER_KIND,
            "activity_id": facts.activity_id,
        }
        if decision.mode == "skip":
            payload["skip_reason"] = decision.reason
            async with session_factory() as session:
                session.add(
                    MessageLogRow(
                        direction="outbound",
                        body=None,
                        payload=payload,
                        trace_id=trace_id,
                        agent_version=AGENT_VERSION,
                    )
                )
                await session.commit()
            logger.warning(
                "Post-activity review of activity %d skipped (no send "
                "attempted): %s",
                facts.activity_id,
                decision.reason,
            )
            details.append(
                {
                    "activity_id": facts.activity_id,
                    "mode": "skip",
                    "reason": decision.reason,
                }
            )
            skipped += 1
            continue

        payload["text"] = review
        if decision.mode == "template":
            payload["content_sid"] = settings.post_activity_review_template_sid
            payload["content_variables"] = {"1": review}

        async with session_factory() as session:
            session.add(
                MessageLogRow(
                    direction="outbound",
                    body=review if decision.mode == "freeform" else None,
                    payload=payload,
                    trace_id=trace_id,
                    agent_version=AGENT_VERSION,
                )
            )
            await session.commit()

        if decision.mode == "freeform":
            twilio_client.messages.create(
                to=recipient, from_=settings.twilio_whatsapp_from, body=review
            )
        else:
            twilio_client.messages.create(
                to=recipient,
                from_=settings.twilio_whatsapp_from,
                content_sid=settings.post_activity_review_template_sid,
                content_variables=json.dumps({"1": review}),
            )
        logger.info(
            "Post-activity review of activity %d sent via %s to %s (trace %s)",
            facts.activity_id,
            decision.mode,
            recipient,
            trace_id,
        )
        details.append({"activity_id": facts.activity_id, "mode": decision.mode})
        sent += 1
    return {
        "reviewed": len(activity_ids),
        "sent": sent,
        "skipped": skipped,
        "details": details,
    }


def run_post_activity_review_sync(
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    twilio_client: Any | None = None,
    now: dt.datetime | None = None,
    athlete_id: int = 1,
) -> dict[str, Any]:
    """Synchronous entry point the Celery task calls; runs the async body.

    Every dependency can be injected for tests; the defaults derive each
    from configuration (``get_settings``, the async DB engine, the Twilio
    REST client) so the production path needs no scaffolding and the tests
    stay network-free.
    """
    config = settings if settings is not None else get_settings()
    sessions = (
        session_factory
        if session_factory is not None
        else make_session_factory(create_db_engine(config.database_url))
    )
    client = twilio_client if twilio_client is not None else make_twilio_client(config)
    return asyncio.run(
        run_post_activity_review(
            settings=config,
            session_factory=sessions,
            twilio_client=client,
            now=now,
            athlete_id=athlete_id,
        )
    )


__all__ = [
    "DEFAULT_LOOKBACK_HOURS",
    "REVIEW_MARKER_KIND",
    "ActivityFacts",
    "DurabilityFacts",
    "compose_post_activity_review",
    "find_unreviewed_activity_ids",
    "gather_review_data",
    "run_post_activity_review",
    "run_post_activity_review_sync",
]
