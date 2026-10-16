"""Chart PNG storage: pure DB persistence of rendered charts (CH-9, §9.1).

Pure DB logic — NO FastAPI imports here: the media route
(:mod:`app.api.charts`) and the ``plot_metric`` tool (CH-10, §9.3) both
build on these functions.

OWNER DECISION (2026-10-16, odd/tasks/charts.md Log L2): chart PNGs are
served through an authenticated, expiring FastAPI route, not object
storage, so the rendered bytes are persisted in the ``chart`` table
(:class:`app.db.models.ChartRow`) — personal health data stays inside the
owner's infrastructure (§14).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChartRow


async def save_chart(
    session: AsyncSession,
    kind: str,
    png: bytes,
    engine_version: str,
    params: dict[str, object] | None = None,
) -> int:
    """Persist one rendered chart and return its id.

    ``params`` is the chart-inputs echo (what the renderer was asked to
    draw) — stored verbatim for provenance, never re-rendered from.
    """
    row = ChartRow(
        kind=kind,
        png=png,
        engine_version=engine_version,
        params=params,
    )
    session.add(row)
    await session.commit()
    return row.id


async def load_chart_png(session: AsyncSession, chart_id: int) -> bytes | None:
    """Return the stored PNG bytes for ``chart_id``, or None if unknown.

    A missing chart is reported AS missing (None) — never an empty or
    fabricated payload.
    """
    row = await session.get(ChartRow, chart_id)
    if row is None:
        return None
    return row.png
