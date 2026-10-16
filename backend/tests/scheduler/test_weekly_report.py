"""SCH-8 RED tests: the Sunday weekly report (§11).

Pinned contracts:

- **Window**: the report fired on a Sunday covers the COMPLETED ISO week
  (the Monday-Sunday week of ``as_of - 7 days``) — every one of its seven
  days can actually have data, never the still-running week.
- **Content, composed from PERSISTED engine data only**: the load summary
  (total TSS, days trained, end-of-week CTL/ATL/TSB from the ``daily_load``
  combined rows), the intensity distribution (the persisted
  ``weekly_intensity`` percentages verbatim, plus the engine's neutral
  descriptive pattern label over the week's persisted zone seconds), the
  threshold section and the next-week focus.
- **Threshold proposals**: the report SURFACES the PENDING proposals the
  agent's ``propose_threshold_update`` tool persisted (the owner-review
  seam) next to the CURRENT engine values — and NEVER applies anything:
  after a report, every proposal row is still ``pending`` and the
  athlete profile is untouched. The composer never invents a proposal
  from weekly aggregates: a weekly TSS total says nothing about a
  threshold, so no proposal is derived here (the engine's ZON-9
  detection over real best efforts remains the future seam).
- **Next-week focus**: the engine's own verdicts only — how many days of
  the week the multi-signal rule suggested reducing intensity. No
  invented advice.
- **≤ ~1,000 characters**, no citations, no chart (owner decision
  2026-10-14). The engine's descriptive-caveat text carries author-year
  citations and is deliberately NOT copied into the message.
- **Window/template/audit discipline exactly as the morning brief**:
  free-form inside the 24 h window, the configured template outside it,
  a loud skip when no template is configured, and an outbound audit row
  written BEFORE the Twilio call.
- **Nothing to report** (no load rows in the window): loud skip, never a
  fabricated report.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.settings import Settings
from app.db.models import (
    AthleteProfileRow,
    DailyLoadRow,
    MessageLogRow,
    ReadinessSnapshotRow,
    ThresholdProposalRow,
    WeeklyIntensityRow,
)
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

FROM = "whatsapp:+34600000001"
# A Sunday (2026-10-18 is a Sunday). The completed week is Mon 05-Sun 11.
NOW = dt.datetime(2026, 10, 18, 18, 0, tzinfo=dt.UTC)
WEEK_START = dt.date(2026, 10, 5)
WEEK_END = dt.date(2026, 10, 11)


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        twilio_whatsapp_allowlist=FROM,
        **overrides,
    )


class FakeTwilioMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return {"sid": "SMoutbound1"}


class FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = FakeTwilioMessages()


async def seed_week(
    session_factory: async_sessionmaker[Any],
    *,
    reduce_days: int = 0,
) -> None:
    """A completed week of combined load rows + one weekly_intensity row."""
    async with session_factory() as session:
        for offset in range(5):  # Mon..Fri trained, weekend off
            day = WEEK_START + dt.timedelta(days=offset)
            session.add(
                DailyLoadRow(
                    athlete_id=1,
                    date=day,
                    sport="combined",
                    tss=80.0 + offset,
                    ctl=45.0 + offset * 0.1,
                    atl=50.0 + offset * 0.2,
                    tsb=-5.0 - offset * 0.1,
                    methods={"ride": 1},
                    engine_version="0.1.0",
                    computed_at=NOW,
                )
            )
        for offset in range(5):
            day = WEEK_START + dt.timedelta(days=offset)
            session.add(
                ReadinessSnapshotRow(
                    athlete_id=1,
                    date=day,
                    signals=[],
                    adverse_signal_keys=[],
                    agreement_count=2 if offset < reduce_days else 0,
                    suggest_reduce_intensity=offset < reduce_days,
                    suggestion=(
                        "Reduce la intensidad mañana: dos señales adversas."
                        if offset < reduce_days
                        else None
                    ),
                    reasons=["tsb_very_negative"] if offset < reduce_days else [],
                    tsb=-6.0,
                    tsb_very_negative_below=-10.0,
                    subjective_fatigue_reported=offset < reduce_days,
                    acwr=1.4,
                    engine_version="0.1.0",
                    computed_at=NOW,
                )
            )
        session.add(
            WeeklyIntensityRow(
                athlete_id=1,
                iso_year=WEEK_START.isocalendar()[0],
                iso_week=WEEK_START.isocalendar()[1],
                week_start=WEEK_START,
                sport="ride",
                status="data",
                z1_seconds=14000.0,
                z2_seconds=3200.0,
                z3_seconds=800.0,
                total_seconds=18000.0,
                percentages=[77.8, 17.8, 4.4],
                engine_version="0.1.0",
                computed_at=NOW,
            )
        )
        session.add(
            AthleteProfileRow(
                athlete_id=1,
                ftp_watts=180.0,
                ftp_source="owner_configuration",
                css_mps=None,
                css_source=None,
                engine_version="0.1.0",
                updated_at=NOW,
            )
        )
        await session.commit()


async def seed_pending_proposal(
    session_factory: async_sessionmaker[Any],
) -> ThresholdProposalRow:
    """One PENDING threshold proposal, exactly as the agent tool writes it."""
    from app.db.repository import insert_threshold_proposal

    async with session_factory() as session:
        row = await insert_threshold_proposal(
            session,
            athlete_id=1,
            sport="ride",
            metric="bike_power",
            proposed_value=190.0,
            unit="watts",
            evidence="Best 20-min effort 275 W on 2026-10-10",
            engine_version="0.1.0",
            created_at=NOW,
        )
        await session.commit()
        await session.refresh(row)
        return row


async def seed_inbound(
    session_factory: async_sessionmaker[Any], sent_at: dt.datetime
) -> None:
    async with session_factory() as session:
        session.add(
            MessageLogRow(
                direction="inbound",
                message_sid=f"SMin{int(sent_at.timestamp())}",
                body="hola",
                payload={"from": FROM, "to": "whatsapp:+34600000000"},
                trace_id="trace-seed",
                agent_version="0.1.0",
            )
        )
        # The row's created_at defaults to now; the window tests need the
        # historical send time, so stamp it explicitly.
        await session.flush()
        await session.execute(
            update(MessageLogRow)
            .where(MessageLogRow.message_sid == f"SMin{int(sent_at.timestamp())}")
            .values(created_at=sent_at)
        )
        await session.commit()


async def outbound_rows(
    session_factory: async_sessionmaker[Any],
) -> list[MessageLogRow]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(MessageLogRow).where(MessageLogRow.direction == "outbound")
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[Any], None]:
    try:
        engine: AsyncEngine = await create_test_engine()
    except Exception:
        pytest.skip("Postgres unreachable; weekly-report tests require compose Postgres")
    from app.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def run_report(
    session_factory: async_sessionmaker[Any],
    twilio: FakeTwilioClient,
    **settings_kwargs: Any,
) -> dict[str, Any]:
    from app.scheduler.weekly_report import run_weekly_report

    return await run_weekly_report(
        settings=make_settings(**settings_kwargs),
        session_factory=session_factory,
        twilio_client=twilio,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# Pure functions (no DB, no network)
# ---------------------------------------------------------------------------


def test_completed_week_window_is_the_iso_week_of_as_of_minus_seven_days() -> None:
    from app.scheduler.weekly_report import completed_week

    week = completed_week(NOW.date())
    assert week.start == WEEK_START  # Monday
    assert week.end == WEEK_END  # Sunday
    assert week.start.weekday() == 0
    assert week.end.weekday() == 6


def test_composer_shape_limits_and_no_citations() -> None:
    from app.scheduler.weekly_report import WeeklyReportData, compose_weekly_report

    data = WeeklyReportData(
        week_start=WEEK_START,
        week_end=WEEK_END,
        total_tss=410.0,
        days_trained=5,
        ctl=45.5,
        atl=51.0,
        tsb=-5.5,
        distributions=(
            {
                "sport": "ride",
                "status": "data",
                "z1_seconds": 14000.0,
                "z2_seconds": 3200.0,
                "z3_seconds": 800.0,
                "total_seconds": 18000.0,
                "percentages": (77.8, 17.8, 4.4),
            },
        ),
        pattern_label="polarizada",
        currents=({"label": "FTP", "value": 180.0, "unit": "W"},),
        pending=(),
        reduce_days=0,
    )
    text = compose_weekly_report(data, athlete_name="Alberto")
    assert len(text) <= 1000
    assert "410" in text and "5" in text  # total TSS, days trained
    assert "45.5" in text and "51.0" in text and "-5.5" in text
    assert "77.8" in text  # persisted percentages verbatim
    assert "polarizada" in text
    assert "FTP" in text and "180" in text  # current thresholds
    assert "pendiente" in text.lower() or "sin propuestas" in text.lower()
    # Owner decision 2026-10-14: no citations, no charts.
    for forbidden in ("Seiler", "Stöggl", "et al.", "doi", "http", ".png"):
        assert forbidden not in text


def test_composer_lists_pending_proposals_and_states_nothing_is_applied() -> None:
    from app.scheduler.weekly_report import WeeklyReportData, compose_weekly_report

    data = WeeklyReportData(
        week_start=WEEK_START,
        week_end=WEEK_END,
        total_tss=410.0,
        days_trained=5,
        ctl=45.5,
        atl=51.0,
        tsb=-5.5,
        distributions=(),
        pattern_label=None,
        currents=({"label": "FTP", "value": 180.0, "unit": "W"},),
        pending=(
            {
                "label": "FTP",
                "proposed_value": 190.0,
                "unit": "W",
                "evidence": "Best 20-min effort 275 W on 2026-10-10",
            },
        ),
        reduce_days=0,
    )
    text = compose_weekly_report(data, athlete_name="")
    assert "190" in text
    assert "Best 20-min effort" in text  # the proposal's own evidence
    assert "aplic" in text.lower()  # "nada se aplica" is stated


def test_composer_focus_line_reports_only_engine_verdicts() -> None:
    from app.scheduler.weekly_report import WeeklyReportData, compose_weekly_report

    base: dict[str, Any] = dict(
        week_start=WEEK_START,
        week_end=WEEK_END,
        total_tss=410.0,
        days_trained=5,
        ctl=45.5,
        atl=51.0,
        tsb=-5.5,
        distributions=(),
        pattern_label=None,
        currents=(),
        pending=(),
    )
    quiet = compose_weekly_report(WeeklyReportData(**base, reduce_days=0))
    assert "no sugirió" in quiet  # no invented advice
    warned = compose_weekly_report(WeeklyReportData(**base, reduce_days=2))
    assert "2" in warned


def test_composer_reports_missing_distribution_as_missing() -> None:
    from app.scheduler.weekly_report import WeeklyReportData, compose_weekly_report

    data = WeeklyReportData(
        week_start=WEEK_START,
        week_end=WEEK_END,
        total_tss=None,
        days_trained=0,
        ctl=None,
        atl=None,
        tsb=None,
        distributions=(),
        pattern_label=None,
        currents=(),
        pending=(),
        reduce_days=0,
    )
    text = compose_weekly_report(data, athlete_name="")
    assert len(text) <= 1000
    assert "sin datos" in text.lower()


# ---------------------------------------------------------------------------
# The Sunday report against the dedicated test database
# ---------------------------------------------------------------------------


async def test_report_sends_freeform_and_surfaces_pending_proposals_without_applying(
    session_factory: async_sessionmaker[Any],
) -> None:
    """The threshold-proposal seam: the pending proposal is LISTED for the
    owner's review; the proposal row stays ``pending`` and the athlete
    profile keeps its value — the report never applies anything."""
    await seed_week(session_factory)
    proposal = await seed_pending_proposal(session_factory)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=1))
    twilio = FakeTwilioClient()

    summary = await run_report(session_factory, twilio)
    assert summary["sent"] is True
    assert summary["mode"] == "freeform"
    assert len(twilio.messages.calls) == 1
    text = str(twilio.messages.calls[0]["body"])
    assert "190" in text and "Best 20-min effort" in text

    # The proposal was NOT applied: still pending, profile untouched.
    async with session_factory() as session:
        stored = (
            (
                await session.execute(
                    select(ThresholdProposalRow).where(
                        ThresholdProposalRow.id == proposal.id
                    )
                )
            )
            .scalars()
            .one()
        )
        profile = (
            (
                await session.execute(
                    select(AthleteProfileRow).where(AthleteProfileRow.athlete_id == 1)
                )
            )
            .scalars()
            .one()
        )
    assert stored.status == "pending"
    assert profile.ftp_watts == 180.0

    # Audit row with the pipeline's payload discipline + the report kind.
    rows = await outbound_rows(session_factory)
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload["to"] == FROM
    assert payload["text"] == text
    assert payload["mode"] == "freeform"
    assert payload["kind"] == "weekly_report"
    assert payload["week_start"] == WEEK_START.isoformat()
    assert payload["week_end"] == WEEK_END.isoformat()


async def test_report_without_week_data_skips_loudly(
    session_factory: async_sessionmaker[Any],
) -> None:
    twilio = FakeTwilioClient()
    summary = await run_report(session_factory, twilio)
    assert twilio.messages.calls == []
    assert summary["sent"] is False
    assert summary["mode"] == "skip"
    assert summary["reason"]
    # No audit row: nothing was addressable to report (the brief's rule).
    assert await outbound_rows(session_factory) == []


async def test_report_outside_window_uses_template_or_skips(
    session_factory: async_sessionmaker[Any],
) -> None:
    """Outside the 24 h window: the configured template carries the
    report; with none configured, the loud skip of the morning brief."""
    await seed_week(session_factory)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=30))
    twilio = FakeTwilioClient()
    summary = await run_report(session_factory, twilio, weekly_report_template_sid="")
    assert twilio.messages.calls == []
    assert summary["mode"] == "skip"

    sid = "HXweekly0000000000000000000000000"
    twilio2 = FakeTwilioClient()
    summary2 = await run_report(session_factory, twilio2, weekly_report_template_sid=sid)
    assert summary2["mode"] == "template"
    call = twilio2.messages.calls[0]
    assert call["content_sid"] == sid
    assert "body" not in call
    rows = await outbound_rows(session_factory)
    modes = {(r.payload or {}).get("mode") for r in rows}
    assert modes == {"skip", "template"}


# ---------------------------------------------------------------------------
# Celery task wiring
# ---------------------------------------------------------------------------


def test_weekly_report_task_is_registered_with_its_name() -> None:
    import app.scheduler.weekly_report_tasks  # noqa: F401
    from app.scheduler.celery_app import celery_app

    name = "app.scheduler.weekly_report_tasks.weekly_report"
    assert name in celery_app.tasks
