"""Authenticated, expiring chart media route (CH-9, §9.1/§12.7).

OWNER DECISION (2026-10-16, odd/tasks/charts.md Log L2): chart PNGs are
served through THIS FastAPI route, not object storage presigned URLs —
personal health data stays inside the owner's infrastructure (§14).

Access model: the ``plot_metric`` tool (CH-10) signs a short-lived HMAC
token carrying the chart id and an expiry timestamp
(:func:`sign_chart_token`, keyed with ``Settings.chart_media_secret``)
and hands out ``GET /charts/{chart_id}?token=...``. Twilio fetches the
URL to attach the image to a WhatsApp message; once the token expires
the same URL denies access (§9.1: short-lived/expiring, never publicly
permanent).

Failing CLOSED: without a configured secret no token can be valid, and
a tampered or expired token is 403 (access DENIED — the resource may
well exist); only a valid token naming an unknown chart id gets 404.

Token crypto uses the stdlib (``hmac``/``hashlib``, SHA-256, constant
time comparison); ``itsdangerous`` is deliberately NOT added — it is
not a project dependency and the stdlib covers this exactly.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.charts.storage import load_chart_png
from app.core.settings import Settings, get_settings
from app.db.session import get_db_session

router = APIRouter()


def sign_chart_token(chart_id: int, expires_at: datetime, secret: str) -> str:
    """Return an HMAC-signed token carrying ``chart_id`` + expiry.

    Format: ``"{chart_id}:{expires_epoch}:{signature}"`` where the
    signature is HMAC-SHA256 over the payload with ``secret``. Callers
    pass an aware ``expires_at``; ``plot_metric`` (CH-10) computes it as
    now + ``Settings.chart_media_ttl_seconds``.
    """
    payload = f"{chart_id}:{int(expires_at.timestamp())}"
    signature = _signature(payload, secret)
    return f"{payload}:{signature}"


def verify_chart_token(token: str, secret: str) -> int | None:
    """Return the token's chart id if valid AND unexpired, else None.

    Invalid means: malformed, wrong secret (constant-time comparison),
    or expired. None of these distinguish to the caller — the route
    answers 403 for every one of them (access denied, never a probe).
    """
    parts = token.split(":")
    if len(parts) != 3:
        return None
    chart_id_raw, expires_raw, signature = parts
    if not chart_id_raw.isdigit() or not expires_raw.isdigit():
        return None
    payload = f"{chart_id_raw}:{expires_raw}"
    if not hmac.compare_digest(signature, _signature(payload, secret)):
        return None
    if int(expires_raw) < int(datetime.now(UTC).timestamp()):
        return None
    return int(chart_id_raw)


def _signature(payload: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def get_chart_settings() -> Settings:
    """Settings dependency seam (overridden in tests), like the webhook's."""
    return get_settings()


@router.get("/charts/{chart_id}")
async def get_chart(
    chart_id: int,
    token: Annotated[str, Query(description="HMAC-signed, expiring access token")],
    settings: Annotated[Settings, Depends(get_chart_settings)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    """Serve a previously rendered chart PNG to a valid, unexpired token."""
    secret = settings.chart_media_secret
    if not secret:
        # Fail closed: an unconfigured secret means no token can be valid.
        raise HTTPException(status_code=403, detail="invalid or expired token")
    token_chart_id = verify_chart_token(token, secret)
    if token_chart_id is None or token_chart_id != chart_id:
        raise HTTPException(status_code=403, detail="invalid or expired token")
    png = await load_chart_png(session, chart_id)
    if png is None:
        raise HTTPException(status_code=404, detail="chart not found")
    # §9.1: the URL is short-lived; the response must not be cached
    # publicly or long-lived by any intermediary.
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store"},
    )
