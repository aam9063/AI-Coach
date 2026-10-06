"""DB-backed tests for the ``upsert_daily_load`` repository (LOAD-10, first half).

Upserts must be idempotent per ``(athlete_id, date, sport)``: calling the
same upsert twice updates the existing row rather than inserting a
duplicate, following the existing repository pattern (ING-3).
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.models import DailyLoadRow

pytestmark = pytest.mark.anyio

_COMPUTED_AT = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)


def _fresh(select_stmt: Select[Any]) -> Select[Any]:
    """Bypass the session identity map so assertions see persisted values."""
    return select_stmt.execution_options(populate_existing=True)


async def _count(db_session: AsyncSession, model: type[Any]) -> int:
    count: int = (
        await db_session.execute(select(func.count()).select_from(model))
    ).scalar_one()
    return count


class TestUpsertDailyLoad:
    async def test_create_then_update_without_duplicate(self, db_session):  # type: ignore[no-untyped-def]
        created = await repository.upsert_daily_load(
            db_session,
            athlete_id=1,
            date=date(2026, 7, 1),
            sport="ride",
            tss=60.5,
            ctl=1.4,
            atl=8.6,
            tsb=-7.2,
            methods={"hr": 1},
            engine_version="0.1.0",
            computed_at=_COMPUTED_AT,
        )
        assert created.id is not None

        updated = await repository.upsert_daily_load(
            db_session,
            athlete_id=1,
            date=date(2026, 7, 1),
            sport="ride",
            tss=61.0,
            ctl=1.5,
            atl=8.7,
            tsb=-7.3,
            methods={"hr": 2},
            engine_version="0.1.0",
            computed_at=_COMPUTED_AT,
        )

        assert updated.id == created.id  # same row updated, not re-inserted
        assert await _count(db_session, DailyLoadRow) == 1
        stored = (
            await db_session.execute(
                _fresh(select(DailyLoadRow).where(DailyLoadRow.id == created.id))
            )
        ).scalar_one()
        assert stored.tss == 61.0
        assert stored.ctl == 1.5
        assert stored.atl == 8.7
        assert stored.tsb == -7.3
        assert stored.methods == {"hr": 2}
        assert stored.engine_version == "0.1.0"

    async def test_different_sport_rows_are_distinct(self, db_session):  # type: ignore[no-untyped-def]
        await repository.upsert_daily_load(
            db_session,
            athlete_id=1,
            date=date(2026, 7, 1),
            sport="ride",
            tss=60.0,
            ctl=1.0,
            atl=8.0,
            tsb=-7.0,
            engine_version="0.1.0",
            computed_at=_COMPUTED_AT,
        )
        await repository.upsert_daily_load(
            db_session,
            athlete_id=1,
            date=date(2026, 7, 1),
            sport="combined",
            tss=60.0,
            ctl=1.0,
            atl=8.0,
            tsb=-7.0,
            engine_version="0.1.0",
            computed_at=_COMPUTED_AT,
        )
        assert await _count(db_session, DailyLoadRow) == 2
