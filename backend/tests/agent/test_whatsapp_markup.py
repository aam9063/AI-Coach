"""Replies must use WhatsApp's native bold, never markdown.

Measured in production on 2026-10-21: the model answered a real question with
``1. **CTL (Condición Física)**: 8.2 puntos`` and WhatsApp rendered the double
asterisks literally, so the athlete read markup instead of emphasis. The prompt
already asks for WhatsApp formatting and the model ignored it, which is the
recurring lesson of this project: a deterministic normalisation at the send
boundary beats another prompt rule that may or may not be obeyed.

The pipeline-level test goes through the real sync seam with a scripted model, so
it fails if the normalisation is dropped from the path that actually sends.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent.fake_model import FakeModel
from app.agent.pipeline import handle_inbound_message, to_whatsapp_text

SENDER = "whatsapp:+10000000001"
COACH = "whatsapp:+10000000000"


class _RecordingTwilio:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = self
        self.bodies: list[str] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.bodies.append(str(kwargs.get("body", "")))

        class _Message:
            sid = "SMoutbound"

        return _Message()


def test_markdown_bold_becomes_whatsapp_bold() -> None:
    assert to_whatsapp_text("**CTL**: 8.2 puntos") == "*CTL*: 8.2 puntos"


def test_conversion_is_idempotent_and_leaves_native_bold_alone() -> None:
    native = "Tu *CTL* es 8.2 y tu TSB es -12.2"
    assert to_whatsapp_text(native) == native
    once = to_whatsapp_text("**a** y **b**")
    assert once == "*a* y *b*"
    assert to_whatsapp_text(once) == once


def test_plain_text_and_single_asterisks_pass_through() -> None:
    assert to_whatsapp_text("sin formato") == "sin formato"
    assert to_whatsapp_text("2 * 3 = 6") == "2 * 3 = 6"
    assert to_whatsapp_text("") == ""


@pytest.mark.anyio
async def test_the_sent_message_carries_no_markdown_bold(db_engine: Any) -> None:
    from app.core.settings import Settings
    from tests.dbsupport import test_database_url as dedicated_test_database_url

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=dedicated_test_database_url(),
        llm_provider="fake",
        openai_api_key="",
        twilio_account_sid="ACtest",
        twilio_auth_token="test-token",
        twilio_whatsapp_from=COACH,
        twilio_whatsapp_allowlist=SENDER.removeprefix("whatsapp:"),
    )
    twilio = _RecordingTwilio()

    await asyncio.to_thread(
        handle_inbound_message,
        {
            "MessageSid": "SMmarkdown",
            "From": SENDER,
            "To": COACH,
            "Body": "¿cómo estoy de forma?",
        },
        settings=settings,
        model=FakeModel(["1. **CTL**: 8.2 puntos\n2. **ATL**: 17.9 puntos"]),
        twilio_client=twilio,
    )

    assert twilio.bodies, "nothing was sent"
    sent = "".join(twilio.bodies)
    assert "**" not in sent, f"markdown bold reached the athlete: {sent!r}"
    assert "*CTL*" in sent and "*ATL*" in sent


def test_a_markdown_link_collapses_to_its_label() -> None:
    """Measured in production 2026-10-21: a chart arrived as
    "[Rendimiento del Último Mes](https://...)" -- WhatsApp renders neither syntax,
    and the image is now attached by the pipeline, so the URL is redundant."""
    text = "Aquí tienes: [Rendimiento del Último Mes](https://example.test/charts/10?token=abc)"
    assert to_whatsapp_text(text) == "Aquí tienes: Rendimiento del Último Mes"


def test_a_bare_url_survives() -> None:
    """A URL the athlete asked for must not be stripped: only markdown links are."""
    text = "Está en https://example.test/charts/10?token=abc"
    assert to_whatsapp_text(text) == text


# The chart-media seam (which chart this turn produced, and how its URL is
# signed at the send boundary) is pinned in tests/agent/test_chart_media_delivery.py:
# the URL is RE-SIGNED there, never copied from the tool output.
