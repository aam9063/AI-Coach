"""Morning brief composer + free-form window/template rule (SCH-5/SCH-6).

**SCH-5 — the composer.** A PURE function (:func:`compose_morning_brief`)
that assembles the daily brief from PERSISTED engine data only — the
``readiness_snapshot`` row and the ``daily_load`` ``combined`` row that the
06:55 ``daily-load-recompute`` beat entry just refreshed — with:

- the athlete's numbers (TSS of the load window day, CTL/ATL/TSB, and each
  readiness signal's observed value);
- a suggested adjustment ONLY when the persisted snapshot says
  ``suggest_reduce_intensity`` — i.e. only when the ENGINE's multi-signal
  warning rule fired (§7.4: two or more adverse signals agree). A single
  adverse signal never produces a reduction suggestion, and ACWR alone
  never does: ACWR is context-only (§7.2) and is NOT an input of the
  composer at all (pinned by tests). The suggestion text is carried
  VERBATIM from the engine — never rephrased, never invented;
- at most ~1,000 characters (§9.2, the WhatsApp per-message budget);
- NO source citations and NO bibliographic references: OWNER DECISION
  2026-10-14 — replies must not display them, and scheduled messages
  follow the same decision (this supersedes the ODD plan's older "same
  citation rule as the agent" line);
- NO chart: Feature 7 does not exist yet, so "one chart if relevant" is
  deliberately not implemented and a chart is never fabricated.

**Nothing to brief** (no readiness snapshot for the day, or no
``daily_load`` combined row for it): the send is SKIPPED with the reason
LOGGED and returned in the task summary. A "no data" message would be a
fabrication; silence would be dishonest. Skipping loudly is the honest
outcome (documented choice).

**SCH-6 — the window rule.** The WhatsApp free-form window is THE SAME 24 h
window the agent's budget already models (``app.agent.budget
.CONVERSATION_WINDOW``): the 24 h after the athlete's last INBOUND message
(read from ``message_log``). :func:`decide_send_mode` reuses that constant
— a second definition of the same window is exactly the bug the tests pin
against:

- inside the window → free-form send (Twilio ``body``);
- outside the window with ``settings.morning_brief_template_sid``
  configured → template send (Twilio ``content_sid`` +
  ``content_variables`` carrying the composed brief; NO ``body`` — a
  WhatsApp template send does not carry free-form text);
- outside the window with NO template SID configured → SKIP with the
  reason stated in the log, the task summary AND the audit trail. Never a
  send that cannot work, never silent: the WhatsApp sandbox has no
  approved templates at all, so this branch is the sandbox's real
  behaviour today;
- no inbound message ever → by definition outside the window (the
  template/skip branch).

**Boundary convention** (the project's strict rule, as in the engine
constants): exactly 24 h after the last inbound message counts as OUTSIDE
the window — a template (or the skip) is required.

**Audit discipline** (§6, the outbound-row discipline of
:mod:`app.agent.pipeline`): every send AND every deliberate skip writes an
outbound ``message_log`` row BEFORE the Twilio call, with ``to``/``text``/
``part``/``total`` next to the send ``mode`` (and, for a skip, the
``skip_reason``; for a template send, the ``content_sid``) — the trail
answers "what did we actually (try to) send, and why?" by itself. A
no-data skip happens before any recipient/addressing exists and writes no
row: nothing was addressable to audit (logged + returned in the summary
instead, documented choice).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.budget import CONVERSATION_WINDOW
from app.agent.pipeline import AGENT_VERSION, make_twilio_client
from app.api.webhooks_whatsapp import normalize_whatsapp_number
from app.core.settings import Settings, get_settings
from app.db.models import DailyLoadRow, MessageLogRow, ReadinessSnapshotRow
from app.db.session import create_db_engine, make_session_factory

logger = logging.getLogger(__name__)

# The WhatsApp free-form window (§9.1): the 24 h after the athlete's last
# message — the SAME constant the agent budget uses. One window, one
# definition (pinned by tests/scheduler/test_morning_brief_send.py).
FREEFORM_WINDOW = CONVERSATION_WINDOW

WHATSAPP_PREFIX = "whatsapp:"

SendMode = Literal["freeform", "template", "skip"]

SessionFactory = async_sessionmaker[AsyncSession]


# ---------------------------------------------------------------------------
# The composer (SCH-5) — pure, persisted data in, brief text out
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefData:
    """The persisted engine data one brief is composed from (SCH-5).

    Built by :func:`gather_brief_data` from the ``readiness_snapshot`` and
    ``daily_load`` ``combined`` rows. Deliberately NO ``acwr`` field: ACWR
    is context-only (§7.2) and can never influence the brief.
    """

    date: dt.date
    # daily_load combined row (the load model's output, Feature 3).
    tss: float
    ctl: float
    atl: float
    tsb: float
    # The snapshot's structured per-signal dicts (stable SIGNAL_KEYS order).
    signals: tuple[dict[str, Any], ...]
    # The engine's multi-signal verdict (§7.4), persisted on the snapshot.
    suggest_reduce_intensity: bool
    suggestion: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class GatheredBrief:
    """The gather outcome: brief data, or an explicit skip reason."""

    data: BriefData | None
    skip_reason: str | None


# Human label + unit formatter per readiness signal key. Unknown keys fall
# back to the generic formatter — a new engine signal is reported, never
# dropped (missing days are reported AS missing: "sin datos").
_SIGNAL_LABELS: dict[str, str] = {
    "hrv_ln_rmssd": "HRV",
    "resting_hr": "FC reposo",
    "sleep_duration": "Sueño",
}


def _format_signal(signal: dict[str, Any]) -> str:
    """One signal's human fragment, from the PERSISTED dict only."""
    label = _SIGNAL_LABELS.get(str(signal.get("key")), str(signal.get("key")))
    if signal.get("status") != "assessed" or signal.get("observed") is None:
        return f"{label}: sin datos"
    observed = float(signal["observed"])
    if signal.get("key") == "hrv_ln_rmssd":
        return f"{label}: {observed:.2f}"
    if signal.get("key") == "resting_hr":
        return f"{label}: {observed:.0f} bpm"
    if signal.get("key") == "sleep_duration":
        return f"{label}: {observed / 60.0:.1f} h"
    return f"{label}: {observed:.2f}"


def compose_morning_brief(data: BriefData, *, athlete_name: str = "") -> str:
    """Compose the daily brief from the persisted engine data (PURE).

    The suggested adjustment appears ONLY when the persisted snapshot says
    ``suggest_reduce_intensity`` AND carries a suggestion — the engine's
    multi-signal verdict, verbatim (§7.2/§7.4). No citations, no chart
    (see the module docstring for the two owner decisions).
    """
    greeting = f"Buenos días, {athlete_name}." if athlete_name else "Buenos días."
    lines = [
        f"{greeting} Brief de {data.date.isoformat()}:",
        "",
        (
            f"Carga: CTL {data.ctl:.1f} · ATL {data.atl:.1f} · "
            f"TSB {data.tsb:+.1f} · TSS {data.tss:.0f}"
        ),
    ]
    signals_line = " · ".join(_format_signal(signal) for signal in data.signals)
    if signals_line:
        lines.append(signals_line)
    if data.suggest_reduce_intensity and data.suggestion is not None:
        lines.append("")
        lines.append(f"Sugerencia: {data.suggestion}")
    return "\n".join(lines)


async def gather_brief_data(
    session_factory: SessionFactory,
    *,
    athlete_id: int = 1,
    as_of: dt.date,
) -> GatheredBrief:
    """Read the PERSISTED engine rows for one athlete-day (SCH-5).

    Returns the :class:`BriefData` for the ``readiness_snapshot`` row of
    ``as_of`` plus the same-day ``daily_load`` ``combined`` row. Nothing
    is recomputed and nothing is fabricated: a missing snapshot or a
    missing combined load row yields ``data=None`` with an explicit skip
    reason (the TSB/CTL numbers would otherwise have to be invented).
    """
    async with session_factory() as session:
        snapshot = (
            (
                await session.execute(
                    select(ReadinessSnapshotRow).where(
                        ReadinessSnapshotRow.athlete_id == athlete_id,
                        ReadinessSnapshotRow.date == as_of,
                    )
                )
            )
            .scalars()
            .first()
        )
        if snapshot is None:
            return GatheredBrief(
                data=None,
                skip_reason=(
                    f"no readiness snapshot for {as_of.isoformat()}: the "
                    "readiness engine has not assessed this day — the brief "
                    "is skipped, never composed from invented data"
                ),
            )
        load = (
            (
                await session.execute(
                    select(DailyLoadRow).where(
                        DailyLoadRow.athlete_id == athlete_id,
                        DailyLoadRow.date == as_of,
                        DailyLoadRow.sport == "combined",
                    )
                )
            )
            .scalars()
            .first()
        )
        if load is None:
            return GatheredBrief(
                data=None,
                skip_reason=(
                    f"no daily_load combined row for {as_of.isoformat()}: the "
                    "load model has not computed this day — CTL/ATL/TSB would "
                    "have to be fabricated, so the brief is skipped"
                ),
            )
        return GatheredBrief(
            data=BriefData(
                date=as_of,
                tss=load.tss,
                ctl=load.ctl,
                atl=load.atl,
                tsb=load.tsb,
                signals=tuple(snapshot.signals or ()),
                suggest_reduce_intensity=snapshot.suggest_reduce_intensity,
                suggestion=snapshot.suggestion,
                reasons=tuple(snapshot.reasons or ()),
            ),
            skip_reason=None,
        )


# ---------------------------------------------------------------------------
# The window rule (SCH-6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendDecision:
    """The window/template decision for one brief send."""

    mode: SendMode
    reason: str | None


def decide_send_mode(
    *,
    now: dt.datetime,
    last_inbound_at: dt.datetime | None,
    template_sid: str,
    template_setting_label: str = "MORNING_BRIEF_TEMPLATE_SID",
) -> SendDecision:
    """Free-form inside the 24 h window; template outside it if configured;
    skip outside it when no template SID is configured (the sandbox's real
    behaviour today — stated, never silent).

    ``last_inbound_at`` is the athlete's last INBOUND message timestamp
    (UTC, from ``message_log``); ``None`` (the athlete never wrote) is by
    definition outside the window. The boundary is STRICT (project
    convention): exactly 24 h after the last inbound message counts as
    outside.
    """
    if last_inbound_at is not None and now < last_inbound_at + FREEFORM_WINDOW:
        return SendDecision(mode="freeform", reason=None)
    if template_sid:
        return SendDecision(
            mode="template",
            reason=None,
        )
    return SendDecision(
        mode="skip",
        reason=(
            "send time is outside the 24 h free-form window (§9.1) and no "
            "WhatsApp template SID is configured "
            f"({template_setting_label}); the WhatsApp sandbox has no "
            "approved templates, so no send is attempted"
        ),
    )


async def last_inbound_message_at(
    session: AsyncSession, *, sender: str
) -> dt.datetime | None:
    """Timestamp of the athlete's last inbound message (``message_log``).

    The window is measured from the last INBOUND row whose payload names
    this sender (``payload["from"]``) — the same message_log the pipeline
    writes. ``None`` when the athlete never messaged.
    """
    row = (
        (
            await session.execute(
                select(MessageLogRow)
                .where(
                    MessageLogRow.direction == "inbound",
                    MessageLogRow.payload["from"].astext == sender,
                )
                .order_by(MessageLogRow.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return row.created_at if row is not None else None


def brief_recipient(settings: Settings) -> str:
    """The brief's recipient: the first allowlisted WhatsApp number.

    The allowlist IS the owner configuration (§9.1); the first entry is
    the athlete the coach serves. An empty allowlist fails loudly — never
    a send to nowhere.
    """
    for entry in settings.twilio_whatsapp_allowlist.split(","):
        cleaned = normalize_whatsapp_number(entry)
        if cleaned:
            return WHATSAPP_PREFIX + cleaned
    raise ValueError(
        "no WhatsApp recipient configured: TWILIO_WHATSAPP_ALLOWLIST is "
        "empty, so the morning brief has nowhere to go"
    )


# ---------------------------------------------------------------------------
# Task summary
# ---------------------------------------------------------------------------


def skip_summary(reason: str) -> dict[str, Any]:
    """JSON-serializable summary of a skipped brief (Celery contract)."""
    return {"mode": "skip", "sent": False, "reason": reason, "to": None, "brief": None}


def _sent_summary(mode: SendMode, recipient: str, brief: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "sent": True,
        "reason": None,
        "to": recipient,
        "brief": brief,
    }


# ---------------------------------------------------------------------------
# The orchestrator: gather -> decide -> audit -> send/skip
# ---------------------------------------------------------------------------


async def run_morning_brief(
    *,
    settings: Settings,
    session_factory: SessionFactory,
    twilio_client: Any,
    now: dt.datetime | None = None,
    athlete_id: int = 1,
) -> dict[str, Any]:
    """Compose and deliver (or honestly skip) one morning brief.

    Every dependency is injectable (settings, session factory, Twilio
    client, the clock); the tests pass fakes and a fixed ``now``. The
    as-of day is ``now`` in the owner timezone (Europe/Madrid, §14).
    """
    moment = now if now is not None else dt.datetime.now(dt.UTC)
    as_of = moment.astimezone(ZoneInfo(settings.timezone)).date()

    # 1. Gather the persisted engine data. Nothing to brief -> skip loudly,
    #    before any addressing exists (no audit row: nothing was
    #    addressable; the reason is logged and returned instead).
    outcome = await gather_brief_data(session_factory, athlete_id=athlete_id, as_of=as_of)
    if outcome.data is None:
        logger.warning(
            "Morning brief skipped for %s: %s", as_of.isoformat(), outcome.skip_reason
        )
        return skip_summary(outcome.skip_reason or "no data to brief")
    data = outcome.data

    recipient = brief_recipient(settings)

    # 2. The window rule (SCH-6): free-form inside the 24 h window after
    #    the athlete's last inbound message; template outside it when one
    #    is configured; skip outside it when not.
    async with session_factory() as session:
        last_inbound = await last_inbound_message_at(session, sender=recipient)
    decision = decide_send_mode(
        now=moment,
        last_inbound_at=last_inbound,
        template_sid=settings.morning_brief_template_sid,
    )

    brief = compose_morning_brief(data, athlete_name=settings.athlete_name)
    trace_id = uuid4().hex

    # 3. Audit row BEFORE the send (the pipeline's discipline): the trail
    #    must answer what we sent — or why we deliberately did not.
    payload: dict[str, Any] = {
        "to": recipient,
        "part": 1,
        "total": 1,
        "mode": decision.mode,
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
            "Morning brief skipped for %s (no send attempted): %s",
            as_of.isoformat(),
            decision.reason,
        )
        return skip_summary(decision.reason or "skipped")

    payload["text"] = brief
    if decision.mode == "template":
        payload["content_sid"] = settings.morning_brief_template_sid
        payload["content_variables"] = {"1": brief}

    async with session_factory() as session:
        session.add(
            MessageLogRow(
                direction="outbound",
                body=brief if decision.mode == "freeform" else None,
                payload=payload,
                trace_id=trace_id,
                agent_version=AGENT_VERSION,
            )
        )
        await session.commit()

    # 4. Send. Free-form carries the brief in ``body``; a template send
    #    carries it in the template variables (no free-form body exists
    #    outside the window — that is the point of the rule).
    if decision.mode == "freeform":
        twilio_client.messages.create(
            to=recipient, from_=settings.twilio_whatsapp_from, body=brief
        )
    else:
        twilio_client.messages.create(
            to=recipient,
            from_=settings.twilio_whatsapp_from,
            content_sid=settings.morning_brief_template_sid,
            content_variables=json.dumps({"1": brief}),
        )
    logger.info(
        "Morning brief sent for %s via %s to %s (trace %s)",
        as_of.isoformat(),
        decision.mode,
        recipient,
        trace_id,
    )
    return _sent_summary(decision.mode, recipient, brief)


def run_morning_brief_sync(
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
        run_morning_brief(
            settings=config,
            session_factory=sessions,
            twilio_client=client,
            now=now,
            athlete_id=athlete_id,
        )
    )


__all__ = [
    "FREEFORM_WINDOW",
    "BriefData",
    "GatheredBrief",
    "SendDecision",
    "brief_recipient",
    "compose_morning_brief",
    "decide_send_mode",
    "gather_brief_data",
    "last_inbound_message_at",
    "run_morning_brief",
    "run_morning_brief_sync",
    "skip_summary",
]
