"""WhatsApp inbound-message Celery task (§9.1 fast-ack hand-off).

The webhook (``app.api.webhooks_whatsapp``) only authenticates the request
and hands the message off here: all real work — the agent loop, DB access,
the Twilio REST reply — happens in the worker, never in the request path
(§9.1: respond fast, Twilio retries slow responses).

**What is the SDK's and what is ours** (WA-3): the Celery task wrapper is
ours; the agent's tool-calling loop inside the pipeline is the Strands
Agents SDK's; ours are the persistence (``message_log`` with the
``MessageSid`` idempotency anchor), the budget/guardrails (WA-5), the
explicit tool list and the safety prompt (§3).

``handle_inbound_message`` is the module-level seam tests patch (same
pattern as ``_backfill`` in :mod:`app.scheduler.tasks`) and the injectable
entry point (every dependency — settings, session factory, model, Twilio
client — can be injected; defaults derive from configuration). It runs
the full pipeline :func:`app.agent.pipeline.process_inbound_message`
with ``asyncio.run`` (the codebase is async-first; Celery tasks are sync).
"""

from __future__ import annotations

from app.agent.pipeline import handle_inbound_message
from app.scheduler.celery_app import celery_app


@celery_app.task(name="app.scheduler.whatsapp_tasks.process_whatsapp_message")  # type: ignore[untyped-decorator]
def process_whatsapp_message(message: dict[str, str]) -> None:
    """Celery task: process one validated inbound WhatsApp message."""
    handle_inbound_message(message)
