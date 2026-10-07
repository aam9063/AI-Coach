"""WhatsApp inbound-message Celery task (§9.1 fast-ack hand-off).

The webhook (``app.api.webhooks_whatsapp``) only authenticates the request
and hands the message off here: all real work — the agent loop, DB access,
the Twilio REST reply — happens in the worker, never in the request path
(§9.1: respond fast, Twilio retries slow responses).

The processing pipeline itself lands with WA-3 (end-to-end against a fake
LLM adapter). ``handle_inbound_message`` is the module-level seam tests
patch (same pattern as ``_backfill`` in :mod:`app.scheduler.tasks`), so the
hand-off is observable in eager-mode tests without a database, an LLM or a
broker. Until WA-3 lands it logs at WARNING so an unpatched run is
observable rather than silent; it never logs message bodies or secrets.
"""

from __future__ import annotations

import logging
from typing import Any

from app.scheduler.celery_app import celery_app

logger = logging.getLogger(__name__)


def handle_inbound_message(message: dict[str, Any]) -> None:
    """Process one inbound WhatsApp message (pipeline arrives with WA-3).

    ``message`` is the Twilio form payload the webhook validated (keys like
    ``MessageSid``, ``From``, ``To``, ``Body``).
    """
    logger.warning(
        "WhatsApp message %s received but processing is not implemented yet (WA-3)",
        message.get("MessageSid", "<no MessageSid>"),
    )


@celery_app.task(name="app.scheduler.whatsapp_tasks.process_whatsapp_message")  # type: ignore[untyped-decorator]
def process_whatsapp_message(message: dict[str, Any]) -> None:
    """Celery task: process one validated inbound WhatsApp message."""
    handle_inbound_message(message)
