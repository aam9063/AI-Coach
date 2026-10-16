"""SCH-6 RED tests: the free-form window / template rule (§9.1, §11).

The WhatsApp free-form window is THE SAME 24 h window the agent's budget
already models (``app.agent.budget.CONVERSATION_WINDOW``): the 24 h after
the athlete's last inbound message. The brief reuses that constant — a
second definition of the same window is exactly the bug this test pins
against.

Pinned contracts:

- **Inside the window** (last inbound < 24 h ago): the brief is sent
  FREE-FORM (Twilio ``body``, no ``content_sid``).
- **Outside the window** with a template SID configured: the brief is
  sent as the CONFIGURED approved WhatsApp template (Twilio
  ``content_sid`` + ``content_variables``, no ``body`` — a WhatsApp
  template send does not carry a free-form body).
- **Outside the window with NO template SID configured**: the send is
  SKIPPED with the reason stated — never a send that cannot work (the
  WhatsApp sandbox has no approved templates today), never silent.
- **No inbound message at all** is, by definition, outside the window.
- **Boundary**: exactly 24 h after the last inbound message counts as
  OUTSIDE (the project's strict rule: exactly at a limit counts as
  beyond it).
- **Audit discipline**: every send AND every deliberate skip is visible
  as an outbound ``message_log`` row following the pipeline's payload
  discipline (``to``/``text``/``part``/``total`` + mode provenance),
  persisted BEFORE the Twilio call.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.settings import Settings
from app.db.models import DailyLoadRow, MessageLogRow, ReadinessSnapshotRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

FROM = "whatsapp:+34600000001"  # the athlete/owner (also the recipient)
NOW = dt.datetime(2026, 10, 15, 7, 30, tzinfo=dt.UTC)


def make_settings(template_sid: str = "") -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        twilio_whatsapp_allowlist=FROM,
        morning_brief_template_sid=template_sid,
    )


class FakeTwilioMessages:
    """Captures what the scheduler asks the Twilio REST API to send."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return {"sid": "SMoutbound1"}


class FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = FakeTwilioMessages()


SIGNALS: list[dict[str, Any]] = [
    {
        "key": key,
        "status": "assessed",
        "direction": "stable",
        "confidence": 0.9,
        "observed": observed,
        "baseline_mean": observed - 1.0,
        "baseline_sd": 1.0,
        "deviation": 1.0,
        "deviation_in_sd": 1.0,
        "flagged": False,
        "band_half_width_sd": 0.5,
        "window_days": 7,
        "baseline_days": 60,
        "n_window_valid": 7,
        "n_baseline_valid": 58,
        "detail": "inside the baseline band.",
    }
    for key, observed in (
        ("hrv_ln_rmssd", 7.12),
        ("resting_hr", 52.0),
        ("sleep_duration", 450.0),
    )
]
COMPUTED = dt.datetime(2026, 10, 15, 6, 55, tzinfo=dt.UTC)


async def seed_brief_rows(session_factory: async_sessionmaker[Any]) -> None:
    """Persist readiness + combined load so the brief has data to send."""
    async with session_factory() as session:
        session.add(
            DailyLoadRow(
                athlete_id=1,
                date=NOW.date(),
                sport="combined",
                tss=67.4,
                ctl=45.3,
                atl=51.8,
                tsb=-6.5,
                methods={"ride": 1},
                engine_version="0.1.0",
                computed_at=COMPUTED,
            )
        )
        session.add(
            ReadinessSnapshotRow(
                athlete_id=1,
                date=NOW.date(),
                signals=SIGNALS,
                adverse_signal_keys=[],
                agreement_count=0,
                suggest_reduce_intensity=False,
                suggestion=None,
                reasons=[],
                tsb=-6.5,
                tsb_very_negative_below=-10.0,
                subjective_fatigue_reported=False,
                acwr=None,
                engine_version="0.1.0",
                computed_at=COMPUTED,
            )
        )
        await session.commit()


async def seed_inbound(
    session_factory: async_sessionmaker[Any], sent_at: dt.datetime
) -> None:
    """One inbound message from the athlete at ``sent_at`` (UTC)."""
    sid = f"SMinbound{sent_at.strftime('%Y%m%d%H%M%S%f')}"
    async with session_factory() as session:
        session.add(
            MessageLogRow(
                direction="inbound",
                message_sid=sid,
                body="¿cómo voy?",
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
            .where(MessageLogRow.message_sid == sid)
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
    """Session factory on the dedicated test database."""
    try:
        engine: AsyncEngine = await create_test_engine()
    except Exception:
        pytest.skip("Postgres unreachable; brief-send tests require the compose Postgres")
    from app.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Pure decision: window boundaries (no DB, no network)
# ---------------------------------------------------------------------------


def test_window_reuses_the_budget_conversation_window_constant() -> None:
    """One definition of the 24 h window, shared with the agent budget."""
    from app.agent.budget import CONVERSATION_WINDOW
    from app.scheduler.morning_brief import FREEFORM_WINDOW

    assert FREEFORM_WINDOW is CONVERSATION_WINDOW


def test_inside_window_is_freeform() -> None:
    from app.scheduler.morning_brief import decide_send_mode

    last = dt.datetime(2026, 10, 15, 5, 0, tzinfo=dt.UTC)
    decision = decide_send_mode(
        now=last + dt.timedelta(hours=2), last_inbound_at=last, template_sid=""
    )
    assert decision.mode == "freeform"


def test_exactly_24h_counts_as_outside() -> None:
    """Strict rule (project convention): exactly at the limit is beyond it."""
    from app.scheduler.morning_brief import decide_send_mode

    last = dt.datetime(2026, 10, 15, 5, 0, tzinfo=dt.UTC)
    decision = decide_send_mode(
        now=last + dt.timedelta(hours=24), last_inbound_at=last, template_sid="HXxxx"
    )
    assert decision.mode == "template"


def test_just_inside_24h_is_freeform() -> None:
    from app.scheduler.morning_brief import decide_send_mode

    last = dt.datetime(2026, 10, 15, 5, 0, tzinfo=dt.UTC)
    decision = decide_send_mode(
        now=last + dt.timedelta(hours=24) - dt.timedelta(seconds=1),
        last_inbound_at=last,
        template_sid="HXxxx",
    )
    assert decision.mode == "freeform"


def test_no_inbound_message_ever_is_outside_the_window() -> None:
    """An athlete who never wrote is by definition outside the window."""
    from app.scheduler.morning_brief import decide_send_mode

    decision = decide_send_mode(
        now=NOW, last_inbound_at=None, template_sid="HXxxx"
    )
    assert decision.mode == "template"


def test_outside_window_without_template_skips_with_reason() -> None:
    """The sandbox's real behaviour today: no template configured -> skip."""
    from app.scheduler.morning_brief import decide_send_mode

    last = dt.datetime(2026, 10, 14, 5, 0, tzinfo=dt.UTC)
    decision = decide_send_mode(
        now=NOW, last_inbound_at=last, template_sid=""
    )
    assert decision.mode == "skip"
    assert decision.reason  # explicit, never silent
    assert "template" in decision.reason.lower()


def test_outside_window_with_template_uses_template() -> None:
    from app.scheduler.morning_brief import decide_send_mode

    last = dt.datetime(2026, 10, 14, 5, 0, tzinfo=dt.UTC)
    decision = decide_send_mode(
        now=NOW, last_inbound_at=last, template_sid="HXtemplate0000000000000000000000"
    )
    assert decision.mode == "template"


# ---------------------------------------------------------------------------
# Sends against the dedicated test database + fake Twilio
# ---------------------------------------------------------------------------


async def test_inside_window_sends_freeform_and_audits(
    session_factory: async_sessionmaker[Any],
) -> None:
    from app.scheduler.morning_brief import run_morning_brief

    await seed_brief_rows(session_factory)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=2))
    twilio = FakeTwilioClient()
    summary = await run_morning_brief(
        settings=make_settings(template_sid=""),
        session_factory=session_factory,
        twilio_client=twilio,
        now=NOW,
    )
    # Free-form send through Twilio: body carries the brief, no content_sid.
    assert len(twilio.messages.calls) == 1
    call = twilio.messages.calls[0]
    assert call["to"] == FROM
    assert isinstance(call["body"], str) and call["body"].strip()
    assert "content_sid" not in call
    assert len(call["body"]) <= 1000
    # Summary reports the mode.
    assert summary["mode"] == "freeform"
    assert summary["sent"] is True
    # Outbound audit row exists with the pipeline's payload discipline.
    rows = await outbound_rows(session_factory)
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload["to"] == FROM
    assert payload["text"] == call["body"]
    assert payload["part"] == 1
    assert payload["total"] == 1
    assert payload["mode"] == "freeform"


async def test_outside_window_with_template_sends_template_and_audits(
    session_factory: async_sessionmaker[Any],
) -> None:
    from app.scheduler.morning_brief import run_morning_brief

    await seed_brief_rows(session_factory)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=25))
    twilio = FakeTwilioClient()
    sid = "HXtemplate0000000000000000000000"
    summary = await run_morning_brief(
        settings=make_settings(template_sid=sid),
        session_factory=session_factory,
        twilio_client=twilio,
        now=NOW,
    )
    assert len(twilio.messages.calls) == 1
    call = twilio.messages.calls[0]
    # Template send: content_sid + content_variables, NO free-form body.
    assert call["content_sid"] == sid
    variables = json.loads(call["content_variables"])
    assert variables, "the brief text rides the template variables"
    assert "body" not in call
    assert summary["mode"] == "template"
    assert summary["sent"] is True
    rows = await outbound_rows(session_factory)
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload["mode"] == "template"
    assert payload["content_sid"] == sid
    # The composed brief text is auditable (what we intended to deliver).
    assert payload["text"]


async def test_outside_window_without_template_skips_and_audits(
    session_factory: async_sessionmaker[Any],
) -> None:
    from app.scheduler.morning_brief import run_morning_brief

    await seed_brief_rows(session_factory)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=25))
    twilio = FakeTwilioClient()
    summary = await run_morning_brief(
        settings=make_settings(template_sid=""),
        session_factory=session_factory,
        twilio_client=twilio,
        now=NOW,
    )
    # Never attempt a send that cannot work.
    assert twilio.messages.calls == []
    assert summary["mode"] == "skip"
    assert summary["sent"] is False
    assert summary["reason"]
    assert "template" in summary["reason"].lower()
    # The deliberate skip is visible in the audit trail too.
    rows = await outbound_rows(session_factory)
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload["mode"] == "skip"
    assert payload["skip_reason"] == summary["reason"]


async def test_no_inbound_ever_is_outside_window_template_branch(
    session_factory: async_sessionmaker[Any],
) -> None:
    from app.scheduler.morning_brief import run_morning_brief

    await seed_brief_rows(session_factory)
    twilio = FakeTwilioClient()
    summary = await run_morning_brief(
        settings=make_settings(template_sid=""),
        session_factory=session_factory,
        twilio_client=twilio,
        now=NOW,
    )
    assert twilio.messages.calls == []
    assert summary["mode"] == "skip"


async def test_audit_row_survives_a_failed_send(
    session_factory: async_sessionmaker[Any],
) -> None:
    """Audit discipline: the row is committed BEFORE the Twilio call —
    a send that explodes still leaves the audit trail behind."""
    from app.scheduler.morning_brief import run_morning_brief

    await seed_brief_rows(session_factory)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=2))

    class ExplodingMessages(FakeTwilioMessages):
        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("twilio down")

    twilio = FakeTwilioClient()
    twilio.messages = ExplodingMessages()
    with pytest.raises(RuntimeError):
        await run_morning_brief(
            settings=make_settings(template_sid=""),
            session_factory=session_factory,
            twilio_client=twilio,
            now=NOW,
        )
    rows = await outbound_rows(session_factory)
    assert len(rows) == 1
    assert (rows[0].payload or {})["mode"] == "freeform"
