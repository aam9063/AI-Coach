"""Weekly report composer + Sunday send (SCH-8, §11).

A PURE composer (:func:`compose_weekly_report`) over PERSISTED engine
data for the COMPLETED ISO week (the Monday-Sunday week of
``as_of - 7 days`` — fired on a Sunday, every one of its seven days can
actually have data; the still-running week would report an incomplete
seven as if it were final):

- **Load summary**: total TSS, days trained and the end-of-week
  CTL/ATL/TSB from the ``daily_load`` ``combined`` rows of the window.
- **Intensity distribution**: the persisted ``weekly_intensity`` rows of
  that week — the engine's percentages VERBATIM, never re-aggregated —
  plus the engine's own neutral pattern label
  (:func:`app.engine.intensity.descriptive_pattern_comparison`, evaluated
  over the week's persisted zone seconds, never a new computation of
  shares). The engine's ``detail``/``caveat`` texts are deliberately NOT
  copied: they carry author-year citations, and scheduled messages carry
  NO citations (OWNER DECISION 2026-10-14, which supersedes the ODD
  plan's older "same citation rule as the agent" line for the brief and,
  by the same decision, for every scheduled message).
- **Threshold proposals — the owner-review seam**: the report lists the
  CURRENT engine thresholds (the ``athlete_profile`` value of record,
  with owner configuration for what the profile does not store) next to
  the PENDING ``threshold_proposal`` rows the agent's
  ``propose_threshold_update`` tool persisted, presented as awaiting the
  owner's review. The report NEVER applies a proposal and NEVER derives
  one of its own: a weekly aggregate says nothing about a threshold, so
  inventing one here would fabricate exactly the kind of unbacked number
  §3 forbids. The engine's ZON-9 margin detection over real best efforts
  remains the future seam when an effort-extraction caller exists. After
  a report, every proposal row is still ``pending`` and the profile is
  untouched (pinned by tests).
- **Next-week focus**: the engine's own verdicts only — how many days of
  the week the multi-signal rule suggested reducing intensity (§7.4).
  No invented advice; ACWR alone is never an input.

Nothing to report (no ``daily_load`` combined rows in the window): the
send is SKIPPED with the reason logged and returned — a "no data" report
would be a fabrication (the same honesty rule as the morning brief; no
audit row, since nothing was addressable to report).

**Send discipline — exactly the morning brief's** (:mod:
`app.scheduler.morning_brief`, reused, not duplicated): free-form inside
the 24 h free-form window, the configured template
(``weekly_report_template_sid``) outside it, a loud skip when no template
is configured, and the outbound audit row (payload ``kind=
"weekly_report"`` with the week bounds) committed BEFORE the Twilio call.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agent.pipeline import AGENT_VERSION, make_twilio_client
from app.core.settings import Settings, get_settings
from app.db.models import (
    AthleteProfileRow,
    DailyLoadRow,
    MessageLogRow,
    ReadinessSnapshotRow,
    ThresholdProposalRow,
    WeeklyIntensityRow,
)
from app.db.session import create_db_engine, make_session_factory
from app.engine.intensity import PatternLabel, descriptive_pattern_comparison
from app.scheduler.morning_brief import (
    SessionFactory,
    brief_recipient,
    decide_send_mode,
    last_inbound_message_at,
)

logger = logging.getLogger(__name__)

REPORT_MARKER_KIND = "weekly_report"

# Neutral Spanish pattern labels, citation-free by construction (the
# engine's PatternLabel values are machine keys; the caveat text is NOT
# carried — see the module docstring's citation decision).
_PATTERN_LABELS_ES: dict[PatternLabel, str] = {
    "polarized": "polarizada",
    "pyramidal": "piramidal",
    "mixed": "mixta",
    "insufficient_volume": "volumen insuficiente",
}

_THRESHOLD_LABELS: dict[str, str] = {
    "bike_power": "FTP",
    "run_hr": "LTHR",
    "swim_pace": "CSS",
}

_THRESHOLD_UNITS: dict[str, str] = {
    "bike_power": "W",
    "run_hr": "bpm",
    "swim_pace": "m/s",
}


@dataclass(frozen=True)
class WeekWindow:
    """The completed Monday-Sunday window the report covers."""

    start: dt.date
    end: dt.date


def completed_week(as_of: dt.date) -> WeekWindow:
    """The ISO week of ``as_of - 7 days``: the last COMPLETED Mon-Sun.

    Fired on Sunday, this is the week that ended YESTERDAY-plus-nothing…
    precisely: the ISO week of the date exactly 7 days back, whose Monday
    through Sunday are all in the past. Never the still-running week.
    """
    reference = as_of - dt.timedelta(days=7)
    year, week, _ = reference.isocalendar()
    start = dt.date.fromisocalendar(year, week, 1)
    return WeekWindow(start=start, end=start + dt.timedelta(days=6))


@dataclass(frozen=True)
class WeeklyReportData:
    """The persisted engine data one weekly report is composed from.

    Every field that CAN be missing IS optional: a week with no intensity
    data, no configured threshold or no pending proposal is reported as
    such — never zero-filled, never padded.
    """

    week_start: dt.date
    week_end: dt.date
    # daily_load combined rows of the window.
    total_tss: float | None
    days_trained: int
    ctl: float | None
    atl: float | None
    tsb: float | None
    # weekly_intensity rows of the window, per sport (persisted values).
    distributions: tuple[dict[str, Any], ...]
    # The engine's neutral label over the week's combined zone seconds.
    pattern_label: str | None
    # Current engine thresholds (athlete_profile value of record).
    currents: tuple[dict[str, Any], ...]
    # PENDING threshold proposals awaiting the owner's review.
    pending: tuple[dict[str, Any], ...]
    # Days this week the engine's multi-signal rule suggested reducing.
    reduce_days: int


def compose_weekly_report(data: WeeklyReportData, *, athlete_name: str = "") -> str:
    """Compose the weekly report from the persisted data (PURE, ≤ ~1,000 chars).

    No citations, no chart (owner decision 2026-10-14); suggestions come
    only from the engine's persisted verdicts (§7.4); pending threshold
    proposals are presented as proposals — nothing is applied.
    """
    greeting = f"Hola, {athlete_name}." if athlete_name else "Hola."
    week_label = f"{data.week_start.isoformat()} - {data.week_end.isoformat()}"
    lines = [greeting, f"Informe semanal {week_label}:", ""]
    if data.total_tss is not None:
        load = (
            f"Carga: TSS total {data.total_tss:.0f} en {data.days_trained} "
            "día(s) entrenados"
        )
        if data.ctl is not None and data.atl is not None and data.tsb is not None:
            load += (
                f" · CTL {data.ctl:.1f} · ATL {data.atl:.1f} · TSB {data.tsb:+.1f}"
            )
        lines.append(load)
    else:
        lines.append("Carga: sin datos esta semana.")
    if data.distributions:
        parts = [
            f"{dist['sport']}: {dist['percentages'][0]:g}% Z1 · "
            f"{dist['percentages'][1]:g}% Z2 · {dist['percentages'][2]:g}% Z3"
            for dist in data.distributions
            if dist.get("percentages") is not None
        ]
        if parts:
            lines.append("Distribución (tiempo en zona): " + "; ".join(parts))
        else:
            lines.append("Distribución: sin datos de intensidad esta semana.")
    else:
        lines.append("Distribución: sin datos de intensidad esta semana.")
    if data.pattern_label is not None:
        lines.append(f"Patrón de la semana: {data.pattern_label} (descriptivo)")
    lines.append("")
    if data.currents:
        currents = " · ".join(
            f"{cur['label']} {cur['value']:g} {cur['unit']}" for cur in data.currents
        )
    else:
        currents = "sin valores configurados"
    if data.pending:
        lines.append(f"Umbrales actuales: {currents}")
        lines.append("Propuestas PENDIENTES de tu revisión (nada se aplica):")
        for proposal in data.pending:
            lines.append(
                f"- {proposal['label']} → {proposal['proposed_value']:g} "
                f"{proposal['unit']}: {proposal['evidence']}"
            )
    else:
        lines.append(f"Umbrales: {currents} · sin propuestas pendientes.")
    lines.append("")
    if data.reduce_days > 0:
        lines.append(
            f"Enfoque: el motor sugirió reducir intensidad en "
            f"{data.reduce_days} día(s) esta semana."
        )
    else:
        lines.append("Enfoque: el motor no sugirió ajustes esta semana.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gather: the persisted rows of the completed week
# ---------------------------------------------------------------------------


async def gather_weekly_data(
    session_factory: SessionFactory,
    *,
    as_of: dt.date,
    athlete_id: int = 1,
) -> WeeklyReportData | None:
    """Read the PERSISTED engine rows for the completed week (SCH-8).

    Returns ``None`` when the window has no ``daily_load`` combined rows
    at all — a report would have to fabricate its numbers, so the caller
    skips loudly instead (the morning brief's rule).
    """
    week = completed_week(as_of)
    async with session_factory() as session:
        loads = (
            (
                await session.execute(
                    select(DailyLoadRow)
                    .where(
                        DailyLoadRow.athlete_id == athlete_id,
                        DailyLoadRow.sport == "combined",
                        DailyLoadRow.date >= week.start,
                        DailyLoadRow.date <= week.end,
                    )
                    .order_by(DailyLoadRow.date)
                )
            )
            .scalars()
            .all()
        )
        if not loads:
            return None
        intensity = (
            (
                await session.execute(
                    select(WeeklyIntensityRow).where(
                        WeeklyIntensityRow.athlete_id == athlete_id,
                        WeeklyIntensityRow.week_start == week.start,
                    )
                )
            )
            .scalars()
            .all()
        )
        snapshots = (
            (
                await session.execute(
                    select(ReadinessSnapshotRow).where(
                        ReadinessSnapshotRow.athlete_id == athlete_id,
                        ReadinessSnapshotRow.date >= week.start,
                        ReadinessSnapshotRow.date <= week.end,
                        ReadinessSnapshotRow.suggest_reduce_intensity,
                    )
                )
            )
            .scalars()
            .all()
        )
        profile = (
            (
                await session.execute(
                    select(AthleteProfileRow).where(
                        AthleteProfileRow.athlete_id == athlete_id
                    )
                )
            )
            .scalars()
            .first()
        )
        pending_rows = (
            (
                await session.execute(
                    select(ThresholdProposalRow)
                    .where(
                        ThresholdProposalRow.athlete_id == athlete_id,
                        ThresholdProposalRow.status == "pending",
                    )
                    .order_by(ThresholdProposalRow.created_at)
                )
            )
            .scalars()
            .all()
        )

    # Intensity: the persisted per-sport rows verbatim; the pattern label
    # comes from the ENGINE evaluated over the week's persisted seconds.
    distributions = [
        {
            "sport": row.sport,
            "status": row.status,
            "z1_seconds": row.z1_seconds,
            "z2_seconds": row.z2_seconds,
            "z3_seconds": row.z3_seconds,
            "total_seconds": row.total_seconds,
            "percentages": None if row.percentages is None else list(row.percentages),
        }
        for row in intensity
    ]
    pattern_label = _combined_pattern_label(distributions)
    currents = _current_thresholds(profile=profile, settings=get_settings())
    pending = [
        {
            "label": _THRESHOLD_LABELS.get(row.metric, row.metric),
            "proposed_value": row.proposed_value,
            "unit": row.unit,
            "evidence": row.evidence,
        }
        for row in pending_rows
    ]
    last = loads[-1]
    return WeeklyReportData(
        week_start=week.start,
        week_end=week.end,
        total_tss=sum(load.tss for load in loads),
        days_trained=sum(1 for load in loads if load.tss > 0),
        ctl=last.ctl,
        atl=last.atl,
        tsb=last.tsb,
        distributions=tuple(distributions),
        pattern_label=pattern_label,
        currents=currents,
        pending=tuple(pending),
        reduce_days=len(snapshots),
    )


def _combined_pattern_label(distributions: list[dict[str, Any]]) -> str | None:
    """The engine's neutral label over the week's persisted zone seconds.

    Summing the persisted per-sport seconds combines the week's zones;
    the LABEL still comes from the engine's own
    ``descriptive_pattern_comparison`` (minimum-volume rule included) —
    the composer never classifies by itself. Below the minimum volume the
    engine refuses a label and none is reported.
    """
    if not distributions:
        return None
    z1 = sum(float(dist["z1_seconds"]) for dist in distributions)
    z2 = sum(float(dist["z2_seconds"]) for dist in distributions)
    z3 = sum(float(dist["z3_seconds"]) for dist in distributions)
    if z1 + z2 + z3 <= 0:
        return None
    settings = get_settings()
    comparison = descriptive_pattern_comparison(
        z1_seconds=z1,
        z2_seconds=z2,
        z3_seconds=z3,
        min_pattern_week_seconds=settings.engine_min_pattern_week_seconds,
    )
    label = _PATTERN_LABELS_ES[comparison.label]
    return None if comparison.label == "insufficient_volume" else label


def _current_thresholds(
    *, profile: AthleteProfileRow | None, settings: Settings
) -> tuple[dict[str, Any], ...]:
    """The CURRENT engine thresholds: the profile value of record first,
    the owner configuration for what the profile does not store (LTHR).
    Missing values are omitted — never defaulted, never zero."""
    currents: list[dict[str, Any]] = []
    if profile is not None and profile.ftp_watts is not None:
        currents.append(
            {"label": "FTP", "value": profile.ftp_watts, "unit": _THRESHOLD_UNITS["bike_power"]}
        )
    if settings.athlete_lthr_bpm is not None:
        currents.append(
            {
                "label": "LTHR",
                "value": settings.athlete_lthr_bpm,
                "unit": _THRESHOLD_UNITS["run_hr"],
            }
        )
    if profile is not None and profile.css_mps is not None:
        currents.append(
            {"label": "CSS", "value": profile.css_mps, "unit": _THRESHOLD_UNITS["swim_pace"]}
        )
    return tuple(currents)


# ---------------------------------------------------------------------------
# The Sunday send
# ---------------------------------------------------------------------------


async def run_weekly_report(
    *,
    settings: Settings,
    session_factory: SessionFactory,
    twilio_client: Any,
    now: dt.datetime | None = None,
    athlete_id: int = 1,
) -> dict[str, Any]:
    """Compose and deliver (or honestly skip) one Sunday weekly report.

    Every dependency is injectable (settings, session factory, Twilio
    client, the clock); the tests pass fakes and a fixed ``now``. The
    as-of day is ``now`` in the owner timezone (Europe/Madrid, §14).
    """
    moment = now if now is not None else dt.datetime.now(dt.UTC)
    as_of = moment.astimezone(ZoneInfo(settings.timezone)).date()

    data = await gather_weekly_data(session_factory, as_of=as_of, athlete_id=athlete_id)
    if data is None:
        week = completed_week(as_of)
        reason = (
            f"no daily_load combined rows for the completed week "
            f"{week.start.isoformat()}..{week.end.isoformat()}: the load model "
            "has not computed this week — the report is skipped, never "
            "composed from invented data"
        )
        logger.warning("Weekly report skipped: %s", reason)
        return {"mode": "skip", "sent": False, "reason": reason, "to": None, "report": None}

    recipient = brief_recipient(settings)
    async with session_factory() as session:
        last_inbound = await last_inbound_message_at(session, sender=recipient)
    decision = decide_send_mode(
        now=moment,
        last_inbound_at=last_inbound,
        template_sid=settings.weekly_report_template_sid,
        template_setting_label="WEEKLY_REPORT_TEMPLATE_SID",
    )

    report = compose_weekly_report(data, athlete_name=settings.athlete_name)
    trace_id = uuid4().hex

    # Audit row BEFORE the send (the pipeline's discipline).
    payload: dict[str, Any] = {
        "to": recipient,
        "part": 1,
        "total": 1,
        "mode": decision.mode,
        "kind": REPORT_MARKER_KIND,
        "week_start": data.week_start.isoformat(),
        "week_end": data.week_end.isoformat(),
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
            "Weekly report for %s..%s skipped (no send attempted): %s",
            data.week_start.isoformat(),
            data.week_end.isoformat(),
            decision.reason,
        )
        return {
            "mode": "skip",
            "sent": False,
            "reason": decision.reason,
            "to": recipient,
            "report": None,
        }

    payload["text"] = report
    if decision.mode == "template":
        payload["content_sid"] = settings.weekly_report_template_sid
        payload["content_variables"] = {"1": report}

    async with session_factory() as session:
        session.add(
            MessageLogRow(
                direction="outbound",
                body=report if decision.mode == "freeform" else None,
                payload=payload,
                trace_id=trace_id,
                agent_version=AGENT_VERSION,
            )
        )
        await session.commit()

    if decision.mode == "freeform":
        twilio_client.messages.create(
            to=recipient, from_=settings.twilio_whatsapp_from, body=report
        )
    else:
        twilio_client.messages.create(
            to=recipient,
            from_=settings.twilio_whatsapp_from,
            content_sid=settings.weekly_report_template_sid,
            content_variables=json.dumps({"1": report}),
        )
    logger.info(
        "Weekly report for %s..%s sent via %s to %s (trace %s)",
        data.week_start.isoformat(),
        data.week_end.isoformat(),
        decision.mode,
        recipient,
        trace_id,
    )
    return {
        "mode": decision.mode,
        "sent": True,
        "reason": None,
        "to": recipient,
        "report": report,
    }


def run_weekly_report_sync(
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
        run_weekly_report(
            settings=config,
            session_factory=sessions,
            twilio_client=client,
            now=now,
            athlete_id=athlete_id,
        )
    )


__all__ = [
    "REPORT_MARKER_KIND",
    "WeekWindow",
    "WeeklyReportData",
    "completed_week",
    "compose_weekly_report",
    "gather_weekly_data",
    "run_weekly_report",
    "run_weekly_report_sync",
]
