"""WhatsApp inbound-message pipeline (WA-3, brief §9.1, §9.2).

Processes ONE validated inbound WhatsApp message end-to-end inside the
Celery worker:

1. **Idempotency anchor.** The inbound row is persisted FIRST, keyed by
   the Twilio ``MessageSid`` under the unique constraint
   ``uq_message_log_message_sid``. A Twilio retry of the same message
   collides with the anchor, is logged and skipped — the agent never
   re-runs and the athlete never receives a second reply. The anchor is
   the database, so it survives worker restarts and concurrent retries.
2. **Agent invocation.** A Strands ``Agent`` is built with the ONE model
   object from the provider-agnostic factory (:mod:`app.agent.model_factory`),
   the system-prompt stub (:mod:`app.agent.prompt`) and the EXPLICIT tool
   list (:mod:`app.agent.tools` — never directory-loaded, never vended
   tools). **The tool-calling loop itself is the SDK's**; ours are the
   guardrails around it (WA-5, :mod:`app.agent.budget`):
   ``Limits(turns=…, output_tokens=…, total_tokens=…)`` from settings is
   passed on EVERY invocation, and the per-conversation token budget (the
   WhatsApp free-form 24 h window, §9.1) accumulated from
   ``result.metrics.accumulated_usage`` stops the turn BEFORE the model is
   invoked when the conversation's persisted usage has exhausted it — the
   athlete gets an explicit budget message instead of an unbounded run. A
   loop stop on a per-invocation limit (``limit_*`` stop reason) without a
   final answer also yields an explicit guardrail message.
3. **Audit persistence.** Every tool call the loop executed (name,
   input, output, status) and the outbound reply are written to
   ``message_log`` sharing the turn's ``trace_id`` and ``agent_version``
   (§6 provenance), so any number in a reply is auditable back to the
   tool output it explains (§3). The outbound row's payload also records
   the turn's USAGE (``inputTokens``/``outputTokens``/``totalTokens``
   from ``result.metrics.accumulated_usage``) and LATENCY (ms from
   ``result.metrics.accumulated_metrics``) — the per-turn cost audit —
   plus the sender (``payload["to"]``), the conversation key the budget
   accumulates over.
4. **Reply via Twilio REST.** The agent's text is sent through the
   Twilio REST client with reversed addressing (``From`` <-> ``To``).
   Rows are persisted BEFORE the send: if sending fails, the retry-safe
   anchor guarantees the failure is at most a missed reply, never a
   duplicated one. (Langfuse tracing per turn arrives with WA-11 and
   reuses the trace-id semantics.)

Dependencies are all injectable (settings, session factory, model,
Twilio client); the defaults derive each from configuration so the
production path needs no test scaffolding and the tests need no network
and no credentials (fake model + fake Twilio client).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from strands.session import RepositorySessionManager, SessionManager, SessionRepository

from app.agent import tools as agent_tools
from app.agent.budget import (
    BUDGET_EXHAUSTED_REPLY,
    GUARDRAIL_STOP_REPLY,
    ConversationBudget,
    conversation_usage_tokens,
    limits_from_settings,
)
from app.agent.memory import RollingSummaryConversationManager
from app.agent.model_factory import create_model
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.session_store import DbSessionRepository
from app.agent.tools import tool_list
from app.core.settings import Settings, get_settings
from app.db.models import AgentConversationSummaryRow, MessageLogRow
from app.db.session import create_db_engine, make_session_factory

logger = logging.getLogger(__name__)

# engine_version-style provenance for the agent pipeline itself (§6):
# the version of prompt + tool set that produced a turn.
AGENT_VERSION = "0.1.0"

SessionFactory = async_sessionmaker[AsyncSession]


async def process_inbound_message(
    message: dict[str, str],
    *,
    settings: Settings,
    session_factory: SessionFactory,
    model: Any,
    twilio_client: Any,
    session_repository: SessionRepository,
) -> None:
    """Process one validated inbound message (async body of the task).

    Args:
        message: The Twilio form payload the webhook validated (keys
            ``MessageSid``, ``From``, ``To``, ``Body``).
        settings: Application settings (provider selection, provenance).
        session_factory: Async session factory on the application database.
        model: The ONE Strands model object for the agent (factory-built).
        twilio_client: The Twilio REST client used to send the reply.
        session_repository: REQUIRED: the SDK ``SessionRepository``
            backing the conversation memory (WA-8). Deliberately not
            defaulted here: a settings-derived default would silently
            point DB-touching tests at the configured (development)
            database. The production default is built once, in
            :func:`handle_inbound_message`, from settings; tests inject a
            repository on the dedicated test database.
    """
    message_sid = str(message.get("MessageSid", "") or "")
    body = str(message.get("Body", "") or "")
    trace_id = uuid4().hex

    # 1. Idempotency anchor: persist the inbound row FIRST. A Twilio
    # retry of the same MessageSid hits the unique constraint and is a
    # no-op — never a second agent run, never a second reply. The payload
    # names the sender/receiver: the sender is the conversation key the
    # per-conversation budget accumulates over (WA-5).
    async with session_factory() as session:
        session.add(
            MessageLogRow(
                direction="inbound",
                message_sid=message_sid,
                body=body,
                payload={"from": message.get("From", ""), "to": message.get("To", "")},
                trace_id=trace_id,
                agent_version=AGENT_VERSION,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            logger.warning(
                "WhatsApp message %s already processed (Twilio retry); skipping", message_sid
            )
            return

    # 2. Per-conversation budget guardrail (WA-5): seed the accumulator
    # from the usage persisted by this conversation's earlier turns (the
    # 24 h free-form window, §9.1). An exhausted conversation stops here —
    # an explicit message, zero model invocations, zero new spend.
    sender = str(message.get("From", "") or "")
    async with session_factory() as session:
        prior_usage = await conversation_usage_tokens(session, sender=sender)
    budget = ConversationBudget(
        limit_tokens=settings.agent_conversation_token_budget, used_tokens=prior_usage
    )
    if budget.exhausted:
        async with session_factory() as session:
            session.add(
                MessageLogRow(
                    direction="outbound",
                    body=BUDGET_EXHAUSTED_REPLY,
                    payload={"to": sender, "budget_exhausted": True},
                    trace_id=trace_id,
                    agent_version=AGENT_VERSION,
                )
            )
            await session.commit()
        twilio_client.messages.create(
            to=message["From"], from_=message["To"], body=BUDGET_EXHAUSTED_REPLY
        )
        logger.warning(
            "WhatsApp conversation with %s exhausted its token budget "
            "(%d/%d tokens in the 24 h window); replying with the explicit "
            "budget message (trace %s)",
            sender,
            budget.used_tokens,
            budget.limit_tokens,
            trace_id,
        )
        return

    # 3. Run the agent: the loop is the SDK's; the model, system prompt,
    # explicit tool list and the PER-INVOCATION LIMITS are ours. The tools
    # share this message's DB session factory via the module seam. Invoked
    # through the SDK's native async entry point so everything stays on the
    # caller's event loop.
    #
    # Conversation memory (WA-8) rides the SDK's own session seam: the
    # session id is the SENDER (one session per conversation — isolation
    # by construction, and a message processed by a different Celery
    # process reloads the earlier turns), persistence goes through the
    # SDK's RepositorySessionManager on OUR SessionRepository, and the
    # rolling-summary policy is a ConversationManager subclass.
    agent_tools.set_session_factory_provider(lambda: session_factory)
    memory = RollingSummaryConversationManager(
        window_size=settings.agent_memory_window_messages,
        trigger_messages=settings.agent_summary_trigger_messages,
    )
    session_manager = RepositorySessionManager(
        session_id=sender,
        session_repository=session_repository,
    )
    agent = _build_agent(model, session_manager=session_manager, conversation_manager=memory)
    result = await agent.invoke_async(body, limits=limits_from_settings(settings))
    reply = _reply_text(result)
    if not reply.strip():
        # The loop stopped on a limit (limit_turns/limit_output_tokens/
        # limit_total_tokens) or otherwise produced no final text: the
        # athlete is told why, never left with silence.
        logger.warning(
            "Agent loop produced no text for trace %s (stop_reason=%s); "
            "sending the explicit guardrail message",
            trace_id,
            result.stop_reason,
        )
        reply = GUARDRAIL_STOP_REPLY
    tool_calls = _tool_calls(agent.messages)

    # 4. Persist the audit trail BEFORE sending (see module docstring):
    # the idempotency anchor bounds any failure at one missed reply. The
    # outbound payload records this turn's usage and latency (WA-5) plus
    # the sender — the conversation key the budget accumulates over.
    usage = dict(result.metrics.accumulated_usage)
    latency_ms = result.metrics.accumulated_metrics.get("latencyMs")
    budget.add_usage(usage)
    # The rolling summary's generation IS a model turn (WA-8): its usage
    # counts against the same conversation budget and is persisted with the
    # same provenance as any turn (agent_conversation_summary), where the
    # next turn's budget seed reads it back.
    generated_summaries = list(memory.generated_summaries)
    for generated in generated_summaries:
        budget.add_usage(generated["usage"])
    async with session_factory() as session:
        for call in tool_calls:
            session.add(
                MessageLogRow(
                    direction="tool_call",
                    tool_name=str(call["name"]),
                    payload={
                        "input": call["input"],
                        "output": call["output"],
                        "status": call["status"],
                    },
                    trace_id=trace_id,
                    agent_version=AGENT_VERSION,
                )
            )
        for generated in generated_summaries:
            session.add(
                AgentConversationSummaryRow(
                    session_id=sender,
                    summary=str(generated["summary"]),
                    covered_message_count=int(generated["covered_messages"]),
                    usage=generated["usage"],
                    trace_id=trace_id,
                    agent_version=AGENT_VERSION,
                )
            )
        session.add(
            MessageLogRow(
                direction="outbound",
                body=reply,
                payload={
                    "to": sender,
                    "usage": usage,
                    "latency_ms": latency_ms,
                },
                trace_id=trace_id,
                agent_version=AGENT_VERSION,
            )
        )
        await session.commit()

    # 5. Send the reply through the Twilio REST client (reversed
    # addressing: the inbound From is the outbound To).
    twilio_client.messages.create(to=message["From"], from_=message["To"], body=reply)
    logger.info("WhatsApp reply sent for message %s (trace %s)", message_sid, trace_id)


def _build_agent(
    model: Any,
    *,
    session_manager: SessionManager | None = None,
    conversation_manager: Any | None = None,
) -> Any:
    """Build the Strands agent with OUR system prompt, explicit tools and
    the conversation-memory seam (WA-8: a session manager over our
    repository + the rolling-summary conversation manager)."""
    from strands import Agent

    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=tool_list(),
        session_manager=session_manager,
        conversation_manager=conversation_manager,
    )


def _reply_text(result: Any) -> str:
    """Concatenate the assistant's final text blocks."""
    content = result.message.get("content", [])
    return "".join(block["text"] for block in content if "text" in block)


def _tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract executed tool calls (name, input, output, status) from the
    loop's message history — SDK-generated messages, our audit projection.
    """
    results_by_id: dict[str, dict[str, Any]] = {}
    for message in messages:
        for block in message.get("content", []):
            if "toolResult" in block:
                tool_result = block["toolResult"]
                results_by_id[tool_result["toolUseId"]] = tool_result

    calls: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []):
            if "toolUse" not in block:
                continue
            tool_use = block["toolUse"]
            tool_result = results_by_id.get(tool_use["toolUseId"], {})
            output: Any = None
            for content in tool_result.get("content", []):
                if "json" in content:
                    output = content["json"]
                elif "text" in content and output is None:
                    output = content["text"]
            calls.append(
                {
                    "name": tool_use["name"],
                    "input": tool_use.get("input"),
                    "output": output,
                    "status": tool_result.get("status"),
                }
            )
    return calls


def make_twilio_client(settings: Settings) -> Any:
    """Twilio REST client for the reply path (construction is local —
    no network until a message is actually created)."""
    from twilio.rest import Client

    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def handle_inbound_message(
    message: dict[str, str],
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    model: Any | None = None,
    twilio_client: Any | None = None,
    session_repository: SessionRepository | None = None,
) -> None:
    """Synchronous seam the Celery task calls; runs the async pipeline.

    Every dependency can be injected for tests; the defaults derive each
    from configuration (``get_settings``, the async DB engine, the model
    factory, the Twilio REST client, and — for the conversation memory —
    :class:`app.agent.session_store.DbSessionRepository` on the configured
    database). This module-level function is the seam tests patch to
    observe the hand-off (same pattern as ``_backfill`` in
    :mod:`app.scheduler.tasks`).
    """
    resolved_settings = settings if settings is not None else get_settings()
    resolved_sessions = session_factory if session_factory is not None else make_session_factory(
        create_db_engine(resolved_settings.database_url)
    )
    asyncio.run(
        process_inbound_message(
            message,
            settings=resolved_settings,
            session_factory=resolved_sessions,
            model=model if model is not None else create_model(resolved_settings),
            twilio_client=(
                twilio_client
                if twilio_client is not None
                else make_twilio_client(resolved_settings)
            ),
            session_repository=(
                session_repository
                if session_repository is not None
                else DbSessionRepository(resolved_settings.database_url)
            ),
        )
    )


__all__ = [
    "AGENT_VERSION",
    "handle_inbound_message",
    "make_twilio_client",
    "process_inbound_message",
    "tool_list",
]
