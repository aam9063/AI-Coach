"""CH-9 RED tests: authenticated, expiring chart media route (§9.1, §15).

OWNER DECISION (2026-10-16, odd/tasks/charts.md Log L2): chart PNGs are
served through THIS FastAPI route, not object storage presigned URLs —
personal health data stays inside the owner's infrastructure.

Security contract pinned here:

- a VALID, UNEXPIRED token serves the stored PNG bytes with media type
  ``image/png`` and a ``Cache-Control: private, no-store`` header (the
  URL is short-lived, never publicly or long-lived cacheable);
- an EXPIRED token is 403 — the resource exists, access is denied;
- a TAMPERED token (any modified character) is 403;
- a token signed for a DIFFERENT chart id than the path is 403 (the
  token's chart id and the path must agree);
- a valid token for an UNKNOWN chart id is 404;
- an UNCONFIGURED secret fails CLOSED: even a cryptographically valid
  token is 403 (same convention as the Twilio webhook without an auth
  token);
- the route is registered on the real ``create_app()`` factory (a
  regression guard, same pattern as the WhatsApp webhook tests);
- tokens are computed via the SAME signing helpers the route uses
  (``sign_chart_token``) — the tests never re-implement the crypto,
  they verify the exported contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.charts import sign_chart_token
from app.charts.storage import save_chart
from app.core.settings import Settings
from app.main import create_app

pytestmark = pytest.mark.anyio

SECRET = "test-chart-media-secret"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-rendered-chart-bytes"


def make_settings(**overrides: Any) -> Settings:
    """Settings for the chart route tests (media secret configured)."""
    kwargs: dict[str, Any] = {
        "chart_media_secret": SECRET,
        "chart_media_ttl_seconds": 600,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client against the real app factory with test DB + settings."""
    from app.api.charts import get_chart_settings
    from app.db.session import get_db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_chart_settings] = lambda: make_settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        yield http


def _future_token(chart_id: int, seconds: int = 300) -> str:
    return sign_chart_token(
        chart_id, datetime.now(UTC) + timedelta(seconds=seconds), SECRET
    )


def _past_token(chart_id: int) -> str:
    return sign_chart_token(
        chart_id, datetime.now(UTC) - timedelta(seconds=1), SECRET
    )


async def _saved_chart_id(db_session: AsyncSession) -> int:
    return await save_chart(
        db_session, kind="weekly_tss", png=PNG_BYTES, engine_version="0.1.0"
    )


async def test_valid_token_serves_png_bytes(
    db_session: AsyncSession, client: httpx.AsyncClient
) -> None:
    chart_id = await save_chart(
        db_session,
        kind="performance_manager",
        png=PNG_BYTES,
        engine_version="0.1.0",
    )

    response = await client.get(
        f"/charts/{chart_id}", params={"token": _future_token(chart_id)}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG_BYTES
    # §9.1: short-lived URL, never publicly or long-lived cacheable.
    assert response.headers["cache-control"] == "private, no-store"


async def test_head_is_supported_for_twilio_media_validation(
    db_session: AsyncSession, client: httpx.AsyncClient
) -> None:
    """Twilio validates media URLs with GET AND HEAD (error 63019 otherwise).

    A HEAD request must answer 200 ``image/png`` with the true
    ``Content-Length`` and no body — the same access contract as GET,
    so the media fetcher's pre-flight check succeeds.
    """
    chart_id = await save_chart(
        db_session,
        kind="performance_manager",
        png=PNG_BYTES,
        engine_version="0.1.0",
    )

    response = await client.head(
        f"/charts/{chart_id}", params={"token": _future_token(chart_id)}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(PNG_BYTES))
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content == b""


async def test_expired_token_is_rejected_with_403(
    db_session: AsyncSession, client: httpx.AsyncClient
) -> None:
    chart_id = await _saved_chart_id(db_session)

    response = await client.get(
        f"/charts/{chart_id}", params={"token": _past_token(chart_id)}
    )

    assert response.status_code == 403


async def test_tampered_token_is_rejected_with_403(
    db_session: AsyncSession, client: httpx.AsyncClient
) -> None:
    chart_id = await _saved_chart_id(db_session)
    token = _future_token(chart_id)
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")

    response = await client.get(
        f"/charts/{chart_id}", params={"token": tampered}
    )

    assert response.status_code == 403


async def test_token_signed_for_another_chart_is_rejected_with_403(
    db_session: AsyncSession, client: httpx.AsyncClient
) -> None:
    chart_id = await _saved_chart_id(db_session)
    other_id = chart_id + 1  # valid signature, but for a DIFFERENT chart

    response = await client.get(
        f"/charts/{chart_id}", params={"token": _future_token(other_id)}
    )

    assert response.status_code == 403


async def test_valid_token_for_unknown_chart_id_is_404(
    client: httpx.AsyncClient,
) -> None:
    unknown_id = 999_999

    response = await client.get(
        f"/charts/{unknown_id}", params={"token": _future_token(unknown_id)}
    )

    assert response.status_code == 404


async def test_unconfigured_secret_fails_closed(
    db_session: AsyncSession,
) -> None:
    """No configured secret: even a cryptographically valid token is 403."""
    from app.api.charts import get_chart_settings
    from app.db.session import get_db_session

    chart_id = await _saved_chart_id(db_session)
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_chart_settings] = lambda: make_settings(
        chart_media_secret=""
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.get(
            f"/charts/{chart_id}", params={"token": _future_token(chart_id)}
        )

    assert response.status_code == 403


async def test_chart_route_is_registered_on_the_production_app() -> None:
    """Regression guard: GET /charts/{chart_id} exists on the real factory."""
    app = create_app()

    paths = app.openapi().get("paths", {}).get("/charts/{chart_id}", {})
    assert "get" in paths, "chart media router not registered in create_app()"
