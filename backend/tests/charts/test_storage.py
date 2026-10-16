"""CH-9 RED tests: chart PNG storage (§9.1, §12.7).

The storage service (``app.charts.storage``) is PURE DB LOGIC — no FastAPI
imports — so the media route (CH-9) and the ``plot_metric`` tool (CH-10)
can both build on it. Contract pinned here:

- ``save_chart`` persists the rendered PNG bytes plus ``kind``, the
  ``params`` input echo (nullable), ``engine_version`` (§6 provenance)
  and ``created_at``, and returns the new chart id;
- ``load_chart_png`` returns BYTE-IDENTICAL bytes for a stored id —
  what was rendered is what gets served;
- loading an unknown chart id returns ``None`` (reported as missing,
  never an empty/fabricated payload);
- ``params=None`` (no input echo) is a valid stored state.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.charts.storage import load_chart_png, save_chart
from app.db.models import ChartRow

pytestmark = pytest.mark.anyio

# Minimal fake PNG payload: storage must be byte-opaque (no re-encoding).
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-rendered-chart-bytes"


async def test_save_then_load_returns_identical_bytes(
    db_session: AsyncSession,
) -> None:
    chart_id = await save_chart(
        db_session,
        kind="performance_manager",
        png=PNG_BYTES,
        engine_version="0.1.0",
        params={"from": "2026-01-01", "to": "2026-03-01"},
    )

    assert chart_id > 0
    assert await load_chart_png(db_session, chart_id) == PNG_BYTES


async def test_save_persists_kind_params_engine_version_created_at(
    db_session: AsyncSession,
) -> None:
    chart_id = await save_chart(
        db_session,
        kind="weekly_tss",
        png=PNG_BYTES,
        engine_version="0.1.0",
        params=None,
    )

    row = await db_session.get(ChartRow, chart_id)
    assert row is not None
    assert row.kind == "weekly_tss"
    assert row.png == PNG_BYTES
    assert row.params is None
    assert row.engine_version == "0.1.0"
    assert row.created_at is not None
    assert row.created_at.tzinfo is not None


async def test_load_unknown_id_returns_none(db_session: AsyncSession) -> None:
    assert await load_chart_png(db_session, 999_999) is None
