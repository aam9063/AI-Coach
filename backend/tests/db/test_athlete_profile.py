"""ZON-10 DB tests: ``athlete_profile`` persistence and the confirmation flow.

Covers (ODD ``engine-zones.md`` ZON-10, brief §6/§7.3):

- profile round-trip with per-metric provenance (the engine's source keys);
- idempotent profile upsert (same ``athlete_id`` never duplicates);
- acceptance of a ``ThresholdChangeProposal`` updates the profile with
  provenance and appends an append-only history row carrying the prior
  value, the new value, the evidence and the confirming actor;
- declining records a history entry and leaves the current value untouched;
- any input that is not an explicit ``proposal_not_applied`` proposal is
  refused (a ``NoThresholdChange``, a wrong-status proposal);
- re-recording a stale proposal is refused, never double-applied;
- ``engine_version`` is stamped on every persisted row.

DB-backed tests run against the dedicated test database only
(``tests/conftest.py`` / ``tests/dbsupport.py``); they skip cleanly when the
compose Postgres is unreachable.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import AthleteProfileRow, AthleteThresholdHistoryRow
from app.engine.zones import (
    ThresholdChangeProposal,
    propose_threshold_change,
)
from app.services.athlete_profile import (
    record_threshold_acceptance,
    record_threshold_decline,
)

pytestmark = pytest.mark.anyio

_RECORDED_AT = datetime(2026, 10, 8, 7, 30, tzinfo=UTC)
_ENGINE_VERSION = "0.1.0"


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


async def _count(db_session: AsyncSession, model: type[Any]) -> int:
    count: int = (
        await db_session.execute(select(func.count()).select_from(model))
    ).scalar_one()
    return count


def _ftp_proposal() -> ThresholdChangeProposal:
    """A real proposal from the pure engine: 190 W FTP vs prior 180 W."""
    outcome = propose_threshold_change(
        metric="ftp_watts",
        current_value=180.0,
        new_value=190.0,
        evidence="best 20-min effort 200 W on 2026-10-07",
    )
    assert isinstance(outcome, ThresholdChangeProposal)
    return outcome


class TestAthleteProfileRepository:
    async def test_profile_round_trip_with_provenance(self, db_session):  # type: ignore[no-untyped-def]
        row = await repository.upsert_athlete_profile(
            db_session,
            athlete_id=1,
            intervals_athlete_id="i555003",
            ftp_watts=180.0,
            ftp_source="manual",
            css_mps=0.833333,
            css_source="css_from_time_trials",
            engine_version=_ENGINE_VERSION,
            updated_at=_RECORDED_AT,
        )
        assert row.id is not None

        stored = (
            await db_session.execute(
                _fresh(
                    select(AthleteProfileRow).where(
                        AthleteProfileRow.athlete_id == 1
                    )
                )
            )
        ).scalar_one()
        assert stored.intervals_athlete_id == "i555003"
        assert stored.ftp_watts == 180.0
        assert stored.ftp_source == "manual"  # the engine's source key
        assert stored.css_mps == pytest.approx(0.833333)
        assert stored.css_source == "css_from_time_trials"
        assert stored.cp_watts is None and stored.cp_source is None
        assert stored.cs_mps is None and stored.cs_source is None
        assert stored.engine_version == _ENGINE_VERSION
        assert stored.updated_at == _RECORDED_AT

    async def test_profile_upsert_is_idempotent_per_athlete(self, db_session):  # type: ignore[no-untyped-def]
        first = await repository.upsert_athlete_profile(
            db_session,
            athlete_id=1,
            ftp_watts=180.0,
            ftp_source="manual",
            engine_version=_ENGINE_VERSION,
            updated_at=_RECORDED_AT,
        )
        second = await repository.upsert_athlete_profile(
            db_session,
            athlete_id=1,
            ftp_watts=190.0,
            ftp_source="cp_derived",
            engine_version=_ENGINE_VERSION,
            updated_at=_RECORDED_AT,
        )
        assert second.id == first.id  # same row updated, never duplicated
        assert await _count(db_session, AthleteProfileRow) == 1
        stored = (
            await db_session.execute(
                _fresh(select(AthleteProfileRow).where(AthleteProfileRow.id == first.id))
            )
        ).scalar_one()
        assert stored.ftp_watts == 190.0
        assert stored.ftp_source == "cp_derived"


class TestThresholdAcceptance:
    async def test_acceptance_updates_profile_and_appends_history(self, db_session):  # type: ignore[no-untyped-def]
        await repository.upsert_athlete_profile(
            db_session,
            athlete_id=1,
            ftp_watts=180.0,
            ftp_source="manual",
            engine_version=_ENGINE_VERSION,
            updated_at=_RECORDED_AT,
        )
        proposal = _ftp_proposal()

        profile, history = await record_threshold_acceptance(
            db_session,
            proposal=proposal,
            athlete_id=1,
            source="cp_derived",
            confirmed_by="owner_whatsapp",
            engine_version=_ENGINE_VERSION,
            recorded_at=_RECORDED_AT,
        )

        # Profile updated with provenance.
        stored_profile = (
            await db_session.execute(
                _fresh(select(AthleteProfileRow).where(AthleteProfileRow.id == profile.id))
            )
        ).scalar_one()
        assert stored_profile.ftp_watts == pytest.approx(190.0)
        assert stored_profile.ftp_source == "cp_derived"
        assert stored_profile.engine_version == _ENGINE_VERSION
        assert stored_profile.updated_at == _RECORDED_AT

        # Exactly one append-only history row with the full justification.
        assert await _count(db_session, AthleteThresholdHistoryRow) == 1
        stored_history = (
            await db_session.execute(_fresh(select(AthleteThresholdHistoryRow)))
        ).scalar_one()
        assert stored_history.id == history.id
        assert stored_history.athlete_id == 1
        assert stored_history.metric == "ftp_watts"
        assert stored_history.decision == "accepted"
        assert stored_history.prior_value == pytest.approx(180.0)
        assert stored_history.new_value == pytest.approx(190.0)
        assert stored_history.proposed_value == pytest.approx(190.0)
        assert "best 20-min effort 200 W on 2026-10-07" in stored_history.evidence
        assert stored_history.confirmed_by == "owner_whatsapp"
        assert stored_history.source == "cp_derived"
        assert stored_history.engine_version == _ENGINE_VERSION
        assert stored_history.recorded_at == _RECORDED_AT

    async def test_acceptance_creates_profile_when_missing(self, db_session):  # type: ignore[no-untyped-def]
        proposal = _ftp_proposal()
        profile, history = await record_threshold_acceptance(
            db_session,
            proposal=proposal,
            athlete_id=1,
            source="manual",
            confirmed_by="owner_whatsapp",
            engine_version=_ENGINE_VERSION,
            recorded_at=_RECORDED_AT,
        )
        assert profile.ftp_watts == pytest.approx(190.0)
        assert profile.ftp_source == "manual"
        assert history.prior_value == pytest.approx(180.0)
        assert await _count(db_session, AthleteProfileRow) == 1

    async def test_re_recording_a_stale_proposal_is_refused(self, db_session):  # type: ignore[no-untyped-def]
        """The append-only history must not double-apply one proposal.

        After the first acceptance the profile's current value IS the
        proposal's proposed value, so the same proposal is stale: the flow
        refuses it instead of appending a misleading second history row.
        """
        proposal = _ftp_proposal()
        await record_threshold_acceptance(
            db_session,
            proposal=proposal,
            athlete_id=1,
            source="cp_derived",
            confirmed_by="owner_whatsapp",
            engine_version=_ENGINE_VERSION,
            recorded_at=_RECORDED_AT,
        )
        with pytest.raises(ValueError, match=r"stale|prior"):
            await record_threshold_acceptance(
                db_session,
                proposal=proposal,
                athlete_id=1,
                source="cp_derived",
                confirmed_by="owner_whatsapp",
                engine_version=_ENGINE_VERSION,
                recorded_at=_RECORDED_AT,
            )
        assert await _count(db_session, AthleteThresholdHistoryRow) == 1
        stored = (
            await db_session.execute(_fresh(select(AthleteProfileRow)))
        ).scalar_one()
        assert stored.ftp_watts == pytest.approx(190.0)

    async def test_acceptance_refuses_non_proposal_input(self, db_session):  # type: ignore[no-untyped-def]
        from app.engine.zones import NoThresholdChange

        no_change = propose_threshold_change(
            metric="ftp_watts", current_value=180.0, new_value=180.0
        )
        assert isinstance(no_change, NoThresholdChange)
        with pytest.raises(ValueError, match=r"non-proposal|NoThresholdChange"):
            await record_threshold_acceptance(
                db_session,
                proposal=no_change,  # type: ignore[arg-type]
                athlete_id=1,
                source="manual",
                confirmed_by="owner_whatsapp",
                engine_version=_ENGINE_VERSION,
                recorded_at=_RECORDED_AT,
            )
        with pytest.raises(ValueError, match="non-proposal"):
            await record_threshold_acceptance(
                db_session,
                proposal="190 W",  # type: ignore[arg-type]
                athlete_id=1,
                source="manual",
                confirmed_by="owner_whatsapp",
                engine_version=_ENGINE_VERSION,
                recorded_at=_RECORDED_AT,
            )
        assert await _count(db_session, AthleteThresholdHistoryRow) == 0

    async def test_acceptance_refuses_wrong_status(self, db_session):  # type: ignore[no-untyped-def]
        import dataclasses

        proposal = _ftp_proposal()
        # Runtime-crafted proposal claiming an applied status: the single
        # "proposal_not_applied" literal is the type-level marker (ZON-9);
        # the flow must still refuse it defensively.
        applied = dataclasses.replace(proposal, status="applied")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="proposal_not_applied"):
            await record_threshold_acceptance(
                db_session,
                proposal=applied,
                athlete_id=1,
                source="manual",
                confirmed_by="owner_whatsapp",
                engine_version=_ENGINE_VERSION,
                recorded_at=_RECORDED_AT,
            )
        assert await _count(db_session, AthleteThresholdHistoryRow) == 0

    async def test_acceptance_refuses_unknown_source(self, db_session):  # type: ignore[no-untyped-def]
        proposal = _ftp_proposal()
        with pytest.raises(ValueError, match="source"):
            await record_threshold_acceptance(
                db_session,
                proposal=proposal,
                athlete_id=1,
                source="guessed",
                confirmed_by="owner_whatsapp",
                engine_version=_ENGINE_VERSION,
                recorded_at=_RECORDED_AT,
            )
        assert await _count(db_session, AthleteThresholdHistoryRow) == 0


class TestThresholdDecline:
    async def test_decline_records_history_and_leaves_value_untouched(self, db_session):  # type: ignore[no-untyped-def]
        await repository.upsert_athlete_profile(
            db_session,
            athlete_id=1,
            ftp_watts=180.0,
            ftp_source="manual",
            engine_version=_ENGINE_VERSION,
            updated_at=_RECORDED_AT,
        )
        proposal = _ftp_proposal()

        history = await record_threshold_decline(
            db_session,
            proposal=proposal,
            athlete_id=1,
            confirmed_by="owner_whatsapp",
            engine_version=_ENGINE_VERSION,
            recorded_at=_RECORDED_AT,
        )

        stored_profile = (
            await db_session.execute(_fresh(select(AthleteProfileRow)))
        ).scalar_one()
        assert stored_profile.ftp_watts == pytest.approx(180.0)  # untouched
        assert stored_profile.ftp_source == "manual"

        stored_history = (
            await db_session.execute(_fresh(select(AthleteThresholdHistoryRow)))
        ).scalar_one()
        assert stored_history.id == history.id
        assert stored_history.decision == "declined"
        assert stored_history.prior_value == pytest.approx(180.0)
        assert stored_history.proposed_value == pytest.approx(190.0)
        assert stored_history.new_value is None  # nothing applied
        assert "best 20-min effort 200 W" in stored_history.evidence
        assert stored_history.confirmed_by == "owner_whatsapp"
        assert stored_history.engine_version == _ENGINE_VERSION
        assert stored_history.recorded_at == _RECORDED_AT

    async def test_decline_refuses_non_proposal_input(self, db_session):  # type: ignore[no-untyped-def]
        from app.engine.zones import NoThresholdChange

        no_change = propose_threshold_change(
            metric="ftp_watts", current_value=180.0, new_value=185.0, margin=0.5
        )
        assert isinstance(no_change, NoThresholdChange)
        with pytest.raises(ValueError, match=r"non-proposal|NoThresholdChange"):
            await record_threshold_decline(
                db_session,
                proposal=no_change,  # type: ignore[arg-type]
                athlete_id=1,
                confirmed_by="owner_whatsapp",
                engine_version=_ENGINE_VERSION,
                recorded_at=_RECORDED_AT,
            )
        assert await _count(db_session, AthleteThresholdHistoryRow) == 0


class TestEngineVersionOnEveryRow:
    async def test_engine_version_stamped_on_profile_and_history(self, db_session):  # type: ignore[no-untyped-def]
        await repository.upsert_athlete_profile(
            db_session,
            athlete_id=1,
            ftp_watts=180.0,
            ftp_source="manual",
            engine_version="test-1.2.3",
            updated_at=_RECORDED_AT,
        )
        proposal = _ftp_proposal()
        await record_threshold_acceptance(
            db_session,
            proposal=proposal,
            athlete_id=1,
            source="cp_derived",
            confirmed_by="owner_whatsapp",
            engine_version="test-1.2.3",
            recorded_at=_RECORDED_AT,
        )
        profile = (
            await db_session.execute(_fresh(select(AthleteProfileRow)))
        ).scalar_one()
        history = (
            await db_session.execute(_fresh(select(AthleteThresholdHistoryRow)))
        ).scalar_one()
        assert profile.engine_version == "test-1.2.3"
        assert history.engine_version == "test-1.2.3"
