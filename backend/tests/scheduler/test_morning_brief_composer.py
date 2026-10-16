"""SCH-5 RED tests: the morning brief composer (§11, §9.2).

The brief is composed from PERSISTED engine data only (the
``readiness_snapshot`` row + the ``daily_load`` ``combined`` row) — never
recomputed, never invented.

Pinned contracts:

- **Multi-signal rule fidelity** (§7.2/§7.4): the brief carries a
  suggested adjustment ONLY when the persisted snapshot says
  ``suggest_reduce_intensity`` (two or more adverse signals agreed in the
  engine). A single adverse signal NEVER produces a reduction suggestion,
  and ACWR alone never does — ACWR is context-only (§7.2, Impellizzeri
  2020) and is not even an input of the composer.
- **≤ ~1,000 characters** (§9.2): the composed brief stays within the
  WhatsApp per-message character budget for realistic (long) data.
- **No source citations, no bibliographic references**: OWNER DECISION
  2026-10-14 — replies must not display them; scheduled messages follow
  the same decision (superseding the ODD plan's older "same citation rule
  as the agent" line).
- **No chart**: Feature 7 does not exist; the composer never fabricates a
  chart placeholder.
- **Nothing to brief** (no readiness snapshot, or no ``daily_load``
  combined row for the snapshot's date): the gather step reports an
  explicit skip reason — the send is skipped and the reason LOGGED, never
  a fabricated "no data" message and never silence.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.settings import Settings
from app.db.models import DailyLoadRow, ReadinessSnapshotRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

# A maximal realistic dataset: long name, all signals assessed with long
# audit-detail strings — the kind of data that could push the brief over
# the ~1,000-character budget if the composer were careless.
LONG_NAME = "Alberto"

SIGNALS_FULL: list[dict[str, Any]] = [
    {
        "key": "hrv_ln_rmssd",
        "status": "assessed",
        "direction": "stable",
        "confidence": 0.9,
        "observed": 7.12,
        "baseline_mean": 7.05,
        "baseline_sd": 0.21,
        "deviation": 0.07,
        "deviation_in_sd": 0.33,
        "flagged": False,
        "band_half_width_sd": 0.5,
        "window_days": 7,
        "baseline_days": 60,
        "n_window_valid": 7,
        "n_baseline_valid": 58,
        "detail": "HRV rolling mean inside its own baseline band.",
    },
    {
        "key": "resting_hr",
        "status": "assessed",
        "direction": "elevated",
        "confidence": 0.8,
        "observed": 53.0,
        "baseline_mean": 51.2,
        "baseline_sd": 1.8,
        "deviation": 1.8,
        "deviation_in_sd": 1.0,
        "flagged": True,
        "band_half_width_sd": 0.5,
        "window_days": 1,
        "baseline_days": 30,
        "n_window_valid": 1,
        "n_baseline_valid": 29,
        "detail": "Resting heart rate above its own baseline band.",
    },
    {
        "key": "sleep_duration",
        "status": "assessed",
        "direction": "stable",
        "confidence": 0.85,
        "observed": 450.0,
        "baseline_mean": 440.0,
        "baseline_sd": 30.0,
        "deviation": 10.0,
        "deviation_in_sd": 0.33,
        "flagged": False,
        "band_half_width_sd": 0.5,
        "window_days": 1,
        "baseline_days": 30,
        "n_window_valid": 1,
        "n_baseline_valid": 28,
        "detail": "Sleep duration inside its own baseline band.",
    },
]

SUGGESTION_TEXT = "Consider reducing intensity today."


def make_brief_data(**overrides: Any) -> Any:
    """Build the composer's input dataclass with realistic defaults.

    Imported lazily inside the factory so the RED phase fails on the
    import only where needed; each test overrides what it pins.
    """
    from app.scheduler.morning_brief import BriefData

    defaults: dict[str, Any] = {
        "date": dt.date(2026, 10, 15),
        "tss": 67.4,
        "ctl": 45.3,
        "atl": 51.8,
        "tsb": -6.5,
        "signals": tuple(SIGNALS_FULL),
        "suggest_reduce_intensity": False,
        "suggestion": None,
        "reasons": (),
    }
    defaults.update(overrides)
    return BriefData(**defaults)


def compose(**overrides: Any) -> str:
    from app.scheduler.morning_brief import compose_morning_brief

    return compose_morning_brief(make_brief_data(**overrides), athlete_name=LONG_NAME)


# ---------------------------------------------------------------------------
# The athlete's numbers are present
# ---------------------------------------------------------------------------


def test_brief_contains_the_athletes_numbers() -> None:
    """CTL/ATL/TSB/TSS and the readiness signals appear in the brief."""
    brief = compose()
    assert "45.3" in brief  # CTL
    assert "51.8" in brief  # ATL
    assert "-6.5" in brief  # TSB
    assert "67" in brief  # yesterday's TSS
    assert "7.12" in brief  # HRV ln rMSSD observed
    assert "53" in brief  # resting HR observed (bpm)
    assert "7.5" in brief  # sleep 450 min = 7.5 h


def test_brief_addresses_the_athlete_by_name() -> None:
    brief = compose()
    assert LONG_NAME in brief


def test_brief_without_name_still_composes() -> None:
    """An empty athlete_name is a documented settings default."""
    from app.scheduler.morning_brief import compose_morning_brief

    brief = compose_morning_brief(make_brief_data(), athlete_name="")
    assert brief.strip()


# ---------------------------------------------------------------------------
# The multi-signal rule is pinned hard (§7.2/§7.4)
# ---------------------------------------------------------------------------


def test_suggestion_present_only_when_the_engine_rule_fired() -> None:
    """Two+ adverse signals: the snapshot's suggestion appears VERBATIM."""
    brief = compose(
        suggest_reduce_intensity=True,
        suggestion=SUGGESTION_TEXT,
        reasons=("Resting heart rate above its own baseline band.",),
    )
    assert SUGGESTION_TEXT in brief


def test_single_adverse_signal_never_suggests_a_reduction() -> None:
    """One adverse signal (RHR elevated alone): NO reduction suggestion.

    The persisted snapshot encodes the engine's verdict
    (``suggest_reduce_intensity=False``); the brief must not editorialise
    a suggestion the engine did not make.
    """
    single_adverse = [dict(SIGNALS_FULL[0]), dict(SIGNALS_FULL[1]), dict(SIGNALS_FULL[2])]
    brief = compose(
        signals=tuple(single_adverse),
        suggest_reduce_intensity=False,
        suggestion=None,
        reasons=(),
    )
    assert "reduc" not in brief.lower()
    assert SUGGESTION_TEXT not in brief


def test_tsband_fatigue_alone_still_respects_the_rule() -> None:
    """TSB very negative + reported fatigue is TWO signals: fires (sanity
    that the pinned rule is the ENGINE's, not a composer veto)."""
    brief = compose(
        suggest_reduce_intensity=True,
        suggestion=SUGGESTION_TEXT,
        reasons=(
            "TSB is -12.0, below the very-negative threshold -10.0.",
            "Subjective fatigue was reported for today.",
        ),
    )
    assert SUGGESTION_TEXT in brief


def test_acwr_alone_never_triggers_a_suggestion() -> None:
    """ACWR is CONTEXT-ONLY (§7.2): the composer has no ACWR input at all,
    so no ACWR value can ever produce a reduction suggestion."""
    from app.scheduler.morning_brief import BriefData

    fields = BriefData.__dataclass_fields__
    assert "acwr" not in fields
    # And a snapshot that did NOT fire (whatever its ACWR context was)
    # yields a brief without any suggestion.
    brief = compose(suggest_reduce_intensity=False, suggestion=None)
    assert "reduc" not in brief.lower()


def test_suggestion_absent_from_persisted_snapshot_means_absent_from_brief() -> None:
    """A fired rule with a None suggestion cannot be invented by the
    composer: the brief shows no suggestion rather than a made-up one."""
    brief = compose(suggest_reduce_intensity=True, suggestion=None)
    assert "reduc" not in brief.lower()


# ---------------------------------------------------------------------------
# Message-shape constraints (§9.2, owner decisions 2026-10-14)
# ---------------------------------------------------------------------------


def test_brief_stays_within_the_whatsapp_character_budget() -> None:
    """≤ ~1,000 characters (§9.2) even with maximal realistic data."""
    brief = compose()
    assert len(brief) <= 1000


def test_brief_has_no_source_citations_or_references() -> None:
    """OWNER DECISION 2026-10-14: no citations, no bibliography in
    scheduled messages (same decision as agent replies)."""
    brief = compose(
        suggest_reduce_intensity=True,
        suggestion=SUGGESTION_TEXT,
        reasons=("TSB is -12.0, below the very-negative threshold.",),
    )
    assert "et al" not in brief.lower()
    assert "doi" not in brief.lower()
    # No "(Author, Year)" citation shape.
    import re

    assert re.search(r"\([A-Z][a-z]+, (19|20)\d\d\)", brief) is None


def test_brief_never_fabricates_a_chart() -> None:
    """Feature 7 does not exist: no chart placeholder, no image line."""
    brief = compose()
    assert "gráf" not in brief.lower()
    assert "chart" not in brief.lower()
    assert "[imagen" not in brief.lower()


# ---------------------------------------------------------------------------
# Gathering from the PERSISTED engine rows (dedicated test database)
# ---------------------------------------------------------------------------


def make_task_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


async def seed_engine_rows(
    session_factory: async_sessionmaker[Any],
    *,
    as_of: dt.date,
    suggest_reduce_intensity: bool = False,
    suggestion: str | None = None,
    acwr_context: float | None = 1.45,  # adversarial: high ACWR in context
) -> None:
    """Persist one readiness snapshot + its same-date combined load row."""
    async with session_factory() as session:
        session.add(
            DailyLoadRow(
                athlete_id=1,
                date=as_of,
                sport="combined",
                tss=67.4,
                ctl=45.3,
                atl=51.8,
                tsb=-6.5,
                methods={"ride": 1},
                engine_version="0.1.0",
                computed_at=dt.datetime(2026, 10, 15, 6, 55, tzinfo=dt.UTC),
            )
        )
        session.add(
            ReadinessSnapshotRow(
                athlete_id=1,
                date=as_of,
                signals=SIGNALS_FULL,
                adverse_signal_keys=(
                    ["resting_hr"] if suggest_reduce_intensity is False else
                    ["resting_hr", "tsb_very_negative"]
                ),
                agreement_count=1 if suggest_reduce_intensity is False else 2,
                suggest_reduce_intensity=suggest_reduce_intensity,
                suggestion=suggestion,
                reasons=[
                    "Resting heart rate above its own baseline band."
                ] if suggest_reduce_intensity is False else [
                    "Resting heart rate above its own baseline band.",
                    "TSB is -12.0, below the very-negative threshold.",
                ],
                tsb=-6.5,
                tsb_very_negative_below=-10.0,
                subjective_fatigue_reported=False,
                acwr=acwr_context,
                engine_version="0.1.0",
                computed_at=dt.datetime(2026, 10, 15, 6, 55, tzinfo=dt.UTC),
            )
        )
        await session.commit()


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[Any], None]:
    """Session factory on the dedicated test database."""
    try:
        engine: AsyncEngine = await create_test_engine()
    except Exception:
        pytest.skip("Postgres unreachable; brief-gather tests require the compose Postgres")
    from app.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_gather_reads_the_persisted_rows(
    session_factory: async_sessionmaker[Any],
) -> None:
    as_of = dt.date(2026, 10, 15)
    await seed_engine_rows(session_factory, as_of=as_of)
    from app.scheduler.morning_brief import gather_brief_data

    outcome = await gather_brief_data(session_factory, athlete_id=1, as_of=as_of)
    assert outcome.skip_reason is None
    data = outcome.data
    assert data is not None
    assert data.ctl == pytest.approx(45.3)
    assert data.atl == pytest.approx(51.8)
    assert data.tsb == pytest.approx(-6.5)
    assert data.tss == pytest.approx(67.4)
    assert data.suggest_reduce_intensity is False
    assert data.suggestion is None
    assert {s["key"] for s in data.signals} == {
        "hrv_ln_rmssd",
        "resting_hr",
        "sleep_duration",
    }


async def test_gather_carries_the_engine_verdict_verbatim(
    session_factory: async_sessionmaker[Any],
) -> None:
    """The brief's adjustment comes from the PERSISTED verdict, and the
    snapshot's ACWR context (adversarially set to 1.45 here) never
    reaches the composer."""
    as_of = dt.date(2026, 10, 15)
    await seed_engine_rows(
        session_factory,
        as_of=as_of,
        suggest_reduce_intensity=True,
        suggestion=SUGGESTION_TEXT,
        acwr_context=1.45,
    )
    from app.scheduler.morning_brief import gather_brief_data

    outcome = await gather_brief_data(session_factory, athlete_id=1, as_of=as_of)
    data = outcome.data
    assert data is not None
    assert data.suggest_reduce_intensity is True
    assert data.suggestion == SUGGESTION_TEXT
    from app.scheduler.morning_brief import compose_morning_brief

    brief = compose_morning_brief(data, athlete_name=LONG_NAME)
    assert SUGGESTION_TEXT in brief


async def test_gather_without_readiness_snapshot_skips_with_a_reason(
    session_factory: async_sessionmaker[Any],
) -> None:
    """No snapshot row for the date: explicit skip reason, nothing sent."""
    as_of = dt.date(2026, 10, 15)
    from app.scheduler.morning_brief import gather_brief_data

    outcome = await gather_brief_data(session_factory, athlete_id=1, as_of=as_of)
    assert outcome.data is None
    assert outcome.skip_reason is not None
    assert "readiness" in outcome.skip_reason.lower()


async def test_gather_without_combined_load_row_skips_with_a_reason(
    session_factory: async_sessionmaker[Any],
) -> None:
    """Snapshot exists but no ``daily_load`` combined row for its date:
    explicit skip reason (TSB/load numbers would have to be fabricated)."""
    as_of = dt.date(2026, 10, 15)
    async with session_factory() as session:
        session.add(
            ReadinessSnapshotRow(
                athlete_id=1,
                date=as_of,
                signals=SIGNALS_FULL,
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
                computed_at=dt.datetime(2026, 10, 15, 6, 55, tzinfo=dt.UTC),
            )
        )
        await session.commit()
    from app.scheduler.morning_brief import gather_brief_data

    outcome = await gather_brief_data(session_factory, athlete_id=1, as_of=as_of)
    assert outcome.data is None
    assert outcome.skip_reason is not None
    assert "load" in outcome.skip_reason.lower() or "daily_load" in outcome.skip_reason


async def test_task_skips_and_logs_when_there_is_nothing_to_brief(
    session_factory: async_sessionmaker[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The send path honours the no-data skip: no Twilio call, no outbound
    audit row pretending a message was sent, and the reason is LOGGED."""
    from app.scheduler.morning_brief import run_morning_brief

    class NeverTwilio:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            raise AssertionError("no send may be attempted without data")

    twilio = NeverTwilio()
    now = dt.datetime(2026, 10, 15, 7, 30, tzinfo=dt.UTC)
    summary = await run_morning_brief(
        settings=make_task_settings(),
        session_factory=session_factory,
        twilio_client=twilio,
        now=now,
    )
    assert summary["mode"] == "skip"
    assert summary["sent"] is False
    assert summary["reason"] is not None
    assert any("brief" in r.message.lower() for r in caplog.records)


def test_summary_is_json_serializable_shape() -> None:
    """The task summary contract: JSON-serializable dict (Celery)."""

    from app.scheduler.morning_brief import skip_summary

    dumped = json.dumps(skip_summary("no readiness snapshot for 2026-10-15"))
    assert json.loads(dumped)["mode"] == "skip"
