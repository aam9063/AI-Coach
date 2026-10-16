"""SCH-7 RED tests: the post-activity review (§11).

The trigger is defined HONESTLY: the sync cannot tell a NEW activity from
a re-synced one (the upsert path counts both as ``activities_synced``), so
"new" is not observable from the sync's return. The review is therefore an
IDEMPOTENT PASS bounded to recently started activities: each run finds the
activities started within the look-back window that have no review marker
in the ``message_log`` audit trail and reviews them once. The marker makes
spam structurally impossible — the pass can fire any number of times, an
activity is reviewed EXACTLY once.

Pinned contracts:

- **Reviewed once**: running the trigger twice over the same data
  produces exactly ONE review message.
- **Look-back bound**: an activity older than the look-back window is
  never reviewed (a 30-day first backfill does not become 30 messages).
- **Content**: short analysis from PERSISTED engine outputs (activity
  facts, the day's combined load, the session-durability row) plus the
  request for RPE/sensations that feeds ``log_subjective`` (the athlete's
  reply goes to the agent, which logs it — the review only ASKS, with the
  scales stated). A missing engine output is stated AS missing — never a
  fabricated number.
- **Multi-signal discipline**: an adjustment suggestion appears ONLY when
  the persisted readiness snapshot says ``suggest_reduce_intensity`` with
  a suggestion — the engine's multi-signal verdict, verbatim. ACWR is not
  an input of the composer at all.
- **≤ ~1,000 characters**, no citations, no chart (owner decision
  2026-10-14).
- **Window/template/audit discipline exactly as the morning brief**:
  free-form inside the 24 h window, the configured template outside it,
  a loud skip when no template is configured, and an outbound audit row
  written BEFORE the Twilio call — the row doubles as the review marker
  (``kind`` + ``activity_id`` in the payload), so the audit trail answers
  "was this activity reviewed, and what was sent?" by itself.
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
    ActivityRow,
    DailyLoadRow,
    MessageLogRow,
    ReadinessSnapshotRow,
    SessionDurabilityRow,
)
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

FROM = "whatsapp:+34600000001"  # the athlete/owner (also the recipient)
NOW = dt.datetime(2026, 10, 15, 9, 5, tzinfo=dt.UTC)  # a review pass at :05

ACTIVITY_START = NOW - dt.timedelta(hours=3)
ACTIVITY_DATE = ACTIVITY_START.date()


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


async def seed_activity(
    session_factory: async_sessionmaker[Any],
    *,
    start_time: dt.datetime = ACTIVITY_START,
    activity_id: int | None = None,
) -> ActivityRow:
    """One ingested ride (summary fields only; streams not needed here)."""
    async with session_factory() as session:
        row = ActivityRow(
            source="intervals",
            source_id=f"i{int(start_time.timestamp())}",
            type="Ride",
            name="Rodillo dulce",
            start_time=start_time,
            start_time_local=start_time.isoformat(),
            distance_m=32400.0,
            duration_s=5400,
            rpe=None,
        )
        if activity_id is not None:
            row.id = activity_id
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def seed_engine_outputs(
    session_factory: async_sessionmaker[Any],
    *,
    date: dt.date,
    suggestion: str | None = None,
    suggest_reduce_intensity: bool = False,
) -> None:
    """The day's combined load row + readiness snapshot (+ durability)."""
    async with session_factory() as session:
        session.add(
            DailyLoadRow(
                athlete_id=1,
                date=date,
                sport="combined",
                tss=67.4,
                ctl=45.3,
                atl=51.8,
                tsb=-6.5,
                methods={"ride": 1},
                engine_version="0.1.0",
                computed_at=NOW,
            )
        )
        session.add(
            ReadinessSnapshotRow(
                athlete_id=1,
                date=date,
                signals=[],
                adverse_signal_keys=[],
                agreement_count=2 if suggest_reduce_intensity else 0,
                suggest_reduce_intensity=suggest_reduce_intensity,
                suggestion=suggestion,
                reasons=["tsb_very_negative", "subjective_fatigue"]
                if suggest_reduce_intensity
                else [],
                tsb=-6.5,
                tsb_very_negative_below=-10.0,
                subjective_fatigue_reported=suggest_reduce_intensity,
                acwr=1.9,
                engine_version="0.1.0",
                computed_at=NOW,
            )
        )
        session.add(
            SessionDurabilityRow(
                activity_id=1,
                sport="ride",
                status="ok",
                ef_first_half=1.12,
                ef_second_half=1.05,
                decoupling=0.042,
                decoupling_pct=4.2,
                within_reference_band=True,
                reference_band=0.05,
                intensity_first_half=None,
                intensity_second_half=None,
                intensity_drift=0.0,
                max_intensity_drift=0.0,
                n_samples=3600,
                n_first_half=1800,
                n_second_half=1800,
                detail="steady",
                engine_version="0.1.0",
                computed_at=NOW,
            )
        )
        await session.commit()


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
    """Session factory on the dedicated test database."""
    try:
        engine: AsyncEngine = await create_test_engine()
    except Exception:
        pytest.skip("Postgres unreachable; review tests require the compose Postgres")
    from app.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def run_review(
    session_factory: async_sessionmaker[Any],
    twilio: FakeTwilioClient,
    **settings_kwargs: Any,
) -> dict[str, Any]:
    from app.scheduler.post_activity_review import run_post_activity_review

    return await run_post_activity_review(
        settings=make_settings(**settings_kwargs),
        session_factory=session_factory,
        twilio_client=twilio,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# Pure composer (no DB, no network)
# ---------------------------------------------------------------------------


def _facts(**overrides: Any) -> Any:
    from app.scheduler.post_activity_review import ActivityFacts, DurabilityFacts

    defaults: dict[str, Any] = {
        "activity_id": 1,
        "sport": "Ride",
        "name": "Rodillo dulce",
        "start_time": ACTIVITY_START,
        "duration_s": 5400,
        "distance_m": 32400.0,
        "rpe": None,
        "tss": 67.4,
        "tsb": -6.5,
        "durability": DurabilityFacts(
            status="ok",
            ef_first_half=1.12,
            ef_second_half=1.05,
            decoupling_pct=4.2,
            within_reference_band=True,
        ),
        "engine_suggestion": None,
    }
    defaults.update(overrides)
    return ActivityFacts(**defaults)


def test_composer_is_short_asks_for_rpe_and_has_no_citations() -> None:
    from app.scheduler.post_activity_review import compose_post_activity_review

    text = compose_post_activity_review(_facts(), athlete_name="Alberto")
    assert len(text) <= 1000
    assert "Rodillo dulce" in text
    assert "90 min" in text  # duration_s 5400 -> minutes
    assert "32.4 km" in text
    # The request that feeds log_subjective: RPE + sensations, scales stated.
    assert "RPE" in text
    assert "1-10" in text
    # Owner decision 2026-10-14: no citations, no charts.
    for forbidden in ("Seiler", "Stöggl", "et al.", "doi", "http"):
        assert forbidden not in text


def test_composer_reports_missing_engine_outputs_as_missing() -> None:
    from app.scheduler.post_activity_review import compose_post_activity_review

    text = compose_post_activity_review(_facts(tss=None, tsb=None, durability=None))
    assert len(text) <= 1000
    # Stated AS missing, never fabricated as zero.
    assert "67" not in text
    assert "no" in text.lower()  # an explicit "not computed/available" line


def test_composer_suggestion_only_from_the_multi_signal_verbatim() -> None:
    from app.scheduler.post_activity_review import compose_post_activity_review

    engine_line = "Reduce la intensidad mañana: dos señales adversas."
    text = compose_post_activity_review(_facts(engine_suggestion=engine_line))
    assert engine_line in text  # verbatim, never rephrased
    # A review without an engine verdict invents no advice.
    plain = compose_post_activity_review(_facts(engine_suggestion=None))
    assert "Sugerencia" not in plain


def test_composer_mentions_acwr_never() -> None:
    """ACWR is context-only (§7.2); it cannot enter the review."""
    from app.scheduler.post_activity_review import compose_post_activity_review

    text = compose_post_activity_review(_facts())
    assert "ACWR" not in text


# ---------------------------------------------------------------------------
# Trigger + reviewed-once against the dedicated test database
# ---------------------------------------------------------------------------


async def test_trigger_reviews_a_new_activity_once(
    session_factory: async_sessionmaker[Any],
) -> None:
    """Run the trigger TWICE: one review, ever. The pin SCH-7 asks for."""
    await seed_activity(session_factory)
    await seed_engine_outputs(session_factory, date=ACTIVITY_DATE)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=1))
    twilio = FakeTwilioClient()

    first = await run_review(session_factory, twilio)
    assert first["sent"] == 1
    assert len(twilio.messages.calls) == 1
    call = twilio.messages.calls[0]
    assert call["to"] == FROM
    assert isinstance(call["body"], str) and len(call["body"]) <= 1000

    second = await run_review(session_factory, twilio)
    # The second pass reviews NOTHING: the marker in message_log.
    assert second["reviewed"] == 0
    assert len(twilio.messages.calls) == 1
    rows = await outbound_rows(session_factory)
    reviews = [
        r for r in rows if (r.payload or {}).get("kind") == "post_activity_review"
    ]
    assert len(reviews) == 1
    assert (reviews[0].payload or {})["activity_id"] == 1


async def test_activities_beyond_the_lookback_window_are_never_reviewed(
    session_factory: async_sessionmaker[Any],
) -> None:
    """A 30-day backfill must not become 30 review messages."""
    from app.scheduler.post_activity_review import DEFAULT_LOOKBACK_HOURS

    await seed_activity(
        session_factory,
        start_time=NOW - dt.timedelta(hours=DEFAULT_LOOKBACK_HOURS + 1),
    )
    twilio = FakeTwilioClient()
    summary = await run_review(session_factory, twilio)
    assert summary["reviewed"] == 0
    assert twilio.messages.calls == []
    assert await outbound_rows(session_factory) == []


async def test_review_of_an_activity_without_engine_outputs_is_honest(
    session_factory: async_sessionmaker[Any],
) -> None:
    """A brand-new activity may precede the engine recompute: the review
    still happens (the RPE request is the point) and states what is
    missing instead of fabricating numbers."""
    await seed_activity(session_factory)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=1))
    twilio = FakeTwilioClient()
    summary = await run_review(session_factory, twilio)
    assert summary["reviewed"] == 1
    assert summary["sent"] == 1
    text = str(twilio.messages.calls[0]["body"])
    assert "67.4" not in text and "TSS" not in text
    assert "no" in text.lower()


async def test_review_uses_the_window_template_audit_discipline(
    session_factory: async_sessionmaker[Any],
) -> None:
    """Outside the 24 h window without a template SID: the loud skip of
    the morning brief, audited — and the skip MARKS the activity so the
    pass never re-attempts it (bounded audit trail, never a repeated
    send that cannot work)."""
    await seed_activity(session_factory)
    await seed_engine_outputs(session_factory, date=ACTIVITY_DATE)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=30))
    twilio = FakeTwilioClient()

    summary = await run_review(session_factory, twilio, post_activity_review_template_sid="")
    assert summary["sent"] == 0
    assert summary["skipped"] == 1
    assert twilio.messages.calls == []
    rows = await outbound_rows(session_factory)
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload["mode"] == "skip"
    assert payload["skip_reason"]
    assert payload["kind"] == "post_activity_review"
    # And the marked activity is not retried on the next pass.
    again = await run_review(session_factory, twilio, post_activity_review_template_sid="")
    assert again["reviewed"] == 0
    assert len(await outbound_rows(session_factory)) == 1


async def test_review_audit_row_precedes_the_twilio_send(
    session_factory: async_sessionmaker[Any],
) -> None:
    """Audit discipline: the marker row is committed BEFORE the send — a
    Twilio failure costs at most one missed review, never a duplicate."""
    await seed_activity(session_factory)
    await seed_engine_outputs(session_factory, date=ACTIVITY_DATE)
    await seed_inbound(session_factory, NOW - dt.timedelta(hours=1))

    class ExplodingMessages(FakeTwilioMessages):
        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("twilio down")

    twilio = FakeTwilioClient()
    twilio.messages = ExplodingMessages()
    with pytest.raises(RuntimeError):
        await run_review(session_factory, twilio)
    rows = await outbound_rows(session_factory)
    assert len(rows) == 1
    assert (rows[0].payload or {})["kind"] == "post_activity_review"
    # The marker holds: a retry of the pass does not re-review.
    twilio2 = FakeTwilioClient()
    summary = await run_review(session_factory, twilio2)
    assert summary["reviewed"] == 0
    assert twilio2.messages.calls == []


# ---------------------------------------------------------------------------
# Celery task wiring
# ---------------------------------------------------------------------------


def test_review_task_is_registered_with_its_name() -> None:
    import app.scheduler.post_activity_review_tasks  # noqa: F401
    from app.scheduler.celery_app import celery_app

    name = "app.scheduler.post_activity_review_tasks.post_activity_review"
    assert name in celery_app.tasks
