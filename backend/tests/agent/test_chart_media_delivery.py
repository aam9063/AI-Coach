"""The chart image attached to a WhatsApp reply must be THIS turn's chart,
signed at the send boundary.

Measured in production on 2026-10-21: the athlete asked for a chart and received
a text message with an already-expired media URL, or nothing at all. Two defects
shared one seam:

1. ``_tool_calls(agent.messages)`` walked the WHOLE message list, and with
   conversation memory enabled (WA-8) that list is restored from the session
   store before the turn runs — so a previous turn's ``plot_metric`` result was
   read as if it belonged to the current one, and its already-expired token was
   re-sent. The turn boundary is known: ``_build_agent`` restores the history
   during construction, so ``len(agent.messages)`` right after it is exactly the
   number of messages this turn did NOT produce.
2. The send loop tested ``part == 1`` while iterating chunks, but ``part`` was a
   leaked variable from the earlier audit loop and held ``total_chunks`` — so a
   reply split into two or more WhatsApp messages never carried the image.

Pinned here: the boundary narrows the tool calls; the URL is re-signed with a
fresh clock at the send boundary (immune to history and to any future caching);
and a turn that produced no chart attaches no media even when the conversation
history holds one.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.agent.fake_model import FakeModel
from app.api.charts import (
    build_chart_media_url,
    sign_chart_token,
    verify_chart_token,
)

SENDER = "whatsapp:+10000000001"
COACH = "whatsapp:+10000000000"
SECRET = "test-chart-secret"


class _RecordingTwilio:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _Message:
            sid = "SMoutbound"

        return _Message()


def _settings() -> Any:
    from app.core.settings import Settings
    from tests.dbsupport import test_database_url

    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=test_database_url(),
        llm_provider="fake",
        openai_api_key="",
        twilio_account_sid="ACtest",
        twilio_auth_token="test-token",
        twilio_whatsapp_from=COACH,
        twilio_whatsapp_allowlist=SENDER.removeprefix("whatsapp:"),
        chart_media_secret=SECRET,
        chart_public_base_url="https://example.test",
    )


def _stub_plot_metric(chart_ids: list[int]) -> Any:
    """A ``plot_metric`` stand-in whose output carries an ALREADY EXPIRED url.

    The real tool renders and stores a PNG, which no delivery test needs; what
    matters here is the seam: a chart id plus a stale URL in the tool result.
    Each call consumes the next chart id, exactly as the real tool stores a new
    chart row per plot.
    """
    from strands import tool

    state = {"next": 0}

    @tool
    def plot_metric(kind: str, date_range: str, sport: str = "") -> dict[str, Any]:
        """Plot one metric chart for the athlete (test stand-in)."""
        chart_id = chart_ids[min(state["next"], len(chart_ids) - 1)]
        state["next"] += 1
        stale = build_chart_media_url(
            chart_id,
            _settings(),
            datetime.now(UTC) - timedelta(hours=1),
        )
        return {
            "status": "ok",
            "kind": kind,
            "chart_id": chart_id,
            "media_url": stale,
        }

    return plot_metric


def _token_of(url: str) -> str:
    return url.split("token=", 1)[1]


# --- Unit: re-signing at the send boundary --------------------------------


def test_chart_media_url_re_signs_the_url_with_a_fresh_clock() -> None:
    from app.agent.pipeline import _chart_media_url

    settings = _settings()
    stale_now = datetime(2026, 10, 21, 14, 43, tzinfo=UTC)
    send_now = datetime(2026, 10, 21, 14, 54, tzinfo=UTC)
    stale_url = build_chart_media_url(10, settings, stale_now)
    assert stale_url is not None
    calls = [
        {
            "name": "plot_metric",
            "output": json.dumps(
                {"status": "ok", "chart_id": 10, "media_url": stale_url}
            ),
        }
    ]

    url = _chart_media_url(calls, settings, send_now)

    assert url is not None, "a chart this turn produced must be attached"
    assert url != stale_url, "the expired history url must never be re-sent"
    assert _token_of(url) == sign_chart_token(
        10,
        send_now + timedelta(seconds=settings.chart_media_ttl_seconds),
        SECRET,
    )


def test_chart_media_url_is_none_without_a_chart() -> None:
    from app.agent.pipeline import _chart_media_url

    settings = _settings()
    now = datetime.now(UTC)
    assert _chart_media_url([], settings, now) is None
    assert (
        _chart_media_url(
            [{"name": "plot_metric", "output": '{"status": "error"}'}], settings, now
        )
        is None
    )
    assert (
        _chart_media_url(
            [{"name": "plot_metric", "output": "not json"}], settings, now
        )
        is None
    )
    # An ok result without a chart id cannot be re-signed: never invent one.
    assert (
        _chart_media_url(
            [{"name": "plot_metric", "output": '{"status": "ok"}'}], settings, now
        )
        is None
    )


# --- Unit: the turn boundary ---------------------------------------------


def test_tool_calls_can_be_narrowed_to_the_current_turn() -> None:
    from app.agent.pipeline import _tool_calls

    history = [
        {
            "role": "assistant",
            "content": [
                {
                    "toolUse": {
                        "toolUseId": "old-use",
                        "name": "plot_metric",
                        "input": {"kind": "hrv"},
                    }
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "old-use",
                        "status": "success",
                        "content": [{"json": {"status": "ok", "chart_id": 9}}],
                    }
                }
            ],
        },
    ]

    assert [call["name"] for call in _tool_calls(history)] == ["plot_metric"]
    assert _tool_calls(history, start=len(history)) == []


# --- Integration: the messages actually sent -----------------------------


@pytest.mark.anyio
async def test_the_image_rides_the_first_chunk_of_a_split_reply(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.agent import pipeline
    from app.agent.pipeline import handle_inbound_message
    from app.db.models import MessageLogRow
    from app.db.session import make_session_factory

    monkeypatch.setattr(
        pipeline, "tool_list", lambda: [_stub_plot_metric([10])]
    )
    # Over the 1,000-character budget: the reply is two real WhatsApp messages.
    long_reply = "Gráfico listo. " + "El eje X son días. " * 80
    model = FakeModel(
        [
            {"name": "plot_metric", "input": {"kind": "performance_manager", "date_range": "30d"}},
            long_reply,
        ]
    )
    twilio = _RecordingTwilio()

    await asyncio.to_thread(
        handle_inbound_message,
        {
            "MessageSid": "SMchartsplit",
            "From": SENDER,
            "To": COACH,
            "Body": "mándame la gráfica",
        },
        settings=_settings(),
        model=model,
        twilio_client=twilio,
    )

    assert len(twilio.calls) >= 2, f"expected a split reply, got {len(twilio.calls)}"
    media = [call.get("media_url") for call in twilio.calls]
    assert media[0] is not None, "the first message must carry the chart"
    assert all(entry is None for entry in media[1:]), (
        f"only the first message carries the chart, got {media}"
    )
    assert verify_chart_token(_token_of(media[0][0]), SECRET) == 10

    factory = make_session_factory(db_engine)
    async with factory() as session:
        rows = (
            await session.execute(
                select(MessageLogRow).where(MessageLogRow.direction == "outbound")
            )
        ).scalars().all()
    by_part = {
        int((row.payload or {})["part"]): (row.payload or {}).get("media_url")
        for row in rows
        if row.payload is not None
    }
    assert by_part[1] == media[0][0], (
        "the audit row must record what was ACTUALLY attached"
    )
    assert all(value is None for part, value in by_part.items() if part != 1)


@pytest.mark.anyio
async def test_a_turn_without_a_chart_attaches_no_media(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history of the SAME conversation holds a plot_metric result; the next
    turn plots nothing, so nothing may be attached (the old code re-sent the
    historical — and expired — url)."""
    from app.agent import pipeline
    from app.agent.pipeline import handle_inbound_message
    from app.db.models import MessageLogRow
    from app.db.session import make_session_factory

    monkeypatch.setattr(pipeline, "tool_list", lambda: [_stub_plot_metric([10, 11])])
    settings = _settings()
    first = _RecordingTwilio()
    await asyncio.to_thread(
        handle_inbound_message,
        {
            "MessageSid": "SMchartfirst",
            "From": SENDER,
            "To": COACH,
            "Body": "gráfica de carga",
        },
        settings=settings,
        model=FakeModel(
            [
                {"name": "plot_metric", "input": {"kind": "hrv", "date_range": "30d"}},
                "Ahí tienes.",
            ]
        ),
        twilio_client=first,
    )
    assert first.calls[0].get("media_url") is not None, "turn 1 plots a chart"

    second = _RecordingTwilio()
    await asyncio.to_thread(
        handle_inbound_message,
        {
            "MessageSid": "SMchartsecond",
            "From": SENDER,
            "To": COACH,
            "Body": "¿y cómo voy de forma?",
        },
        settings=settings,
        model=FakeModel(["Vas bien: TSB -12.2"]),
        twilio_client=second,
    )

    assert second.calls, "nothing was sent"
    assert all(call.get("media_url") is None for call in second.calls), (
        f"turn 2 plots nothing and must attach nothing, got {second.calls}"
    )
    factory = make_session_factory(db_engine)
    async with factory() as session:
        rows = (
            await session.execute(
                select(MessageLogRow).where(
                    MessageLogRow.direction == "outbound",
                    MessageLogRow.body == "Vas bien: TSB -12.2",
                )
            )
        ).scalars().all()
    assert rows and all(
        (row.payload or {}).get("media_url") is None for row in rows
    )
