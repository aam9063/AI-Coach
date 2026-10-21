"""WhatsApp inbound-message pipeline (WA-3, brief §9.1, §9.2).

Processes ONE validated inbound WhatsApp message end-to-end inside the
Celery worker:

1. **Idempotency anchor.** The inbound row is persisted FIRST, keyed by
   the Twilio ``MessageSid`` under the unique constraint
   ``uq_message_log_message_sid``. A Twilio retry of the same message
   collides with the anchor, is logged and skipped — the agent never
   re-runs and the athlete never receives a second reply. The anchor is
   the database, so it survives worker restarts and concurrent retries.
2. **On-demand /sync command (SCH-4, §11).** A body that IS the command
   (``/sync`` after whitespace-strip + case-fold; see
   :func:`matches_sync_command`) never reaches the model: it runs the SAME
   idempotent sync job the beat entry runs (the injectable ``sync_runner``
   seam, default :func:`app.scheduler.tasks.run_sync_async`) and answers
   with a SHORT confirmation through the normal send path (outbound audit
   row BEFORE the Twilio send). The check sits before the budget
   guardrail, so the LLM budget neither blocks nor pays for a command that
   invokes no model; the ``MessageSid`` anchor above bounds a Twilio retry
   of the command at one sync run. A message that merely CONTAINS the word
   goes down the normal agent path.
3. **Agent invocation.** A Strands ``Agent`` is built with the ONE model
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
   tool output it explains (§3). The reply is split at the WhatsApp
   per-message CHARACTER budget (WA-8, §9.2 —
   ``settings.agent_whatsapp_message_char_limit``, default ~1,000
   characters; characters, not UTF-8 bytes, because the constraint is
   stated in characters and Twilio's own WhatsApp cap is likewise
   enforced in characters) and EACH chunk is a real message: one
   outbound audit row per send, with ``part``/``total`` in the payload,
   sent in order — never truncated, never dropping the tail. The
   outbound payload also records
   the turn's USAGE (``inputTokens``/``outputTokens``/``totalTokens``
   from ``result.metrics.accumulated_usage``) and LATENCY (ms from
   ``result.metrics.accumulated_metrics``) — the per-turn cost audit —
   plus the sender (``payload["to"]``), the conversation key the budget
   accumulates over.
5. **Reply via Twilio REST.** Each reply chunk is sent through the
   Twilio REST client with reversed addressing (``From`` <-> ``To``).
   Rows are persisted BEFORE the send: if sending fails, the retry-safe
   anchor guarantees the failure is at most a missed reply, never a
   duplicated one.
6. **Tracing (WA-11, §13).** Every turn is traced through the Strands
   SDK's own OpenTelemetry surface (the Agent's own ``Tracer`` spans:
   ``invoke_agent``, ``chat``, ``execute_tool``) exported to Langfuse over
   OTLP — :mod:`app.agent.tracing`, wired once per worker process by
   :func:`handle_inbound_message` and ONLY when the Langfuse settings are
   fully configured (otherwise a clean no-op, exactly as pre-WA-11). The
   turn's agent span carries the repo prompt version
   (``ai.coach.prompt_version``), the model id for cost derivation, the
   turn latency and the SDK-reported token usage; tool calls arrive as
   the SDK's own ``execute_tool`` spans. Tracing is never a failure path:
   exporter failures are swallowed by the OTel processors and wiring
   failures disable tracing for the process instead of breaking a turn.

Dependencies are all injectable (settings, session factory, model,
Twilio client); the defaults derive each from configuration so the
production path needs no test scaffolding and the tests need no network
and no credentials (fake model + fake Twilio client).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
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
from app.agent.context import build_athlete_context
from app.agent.medical_backstop import (
    MEDICAL_PROMPT_REINFORCEMENT,
    detect_medical_signal,
    ensure_medical_disclosure,
)
from app.agent.memory import RollingSummaryConversationManager
from app.agent.model_factory import create_model
from app.agent.prompt import SYSTEM_PROMPT
from app.agent.session_store import DbSessionRepository
from app.agent.tools import tool_list
from app.agent.tracing import (
    configure_langfuse_tracing,
    turn_latency_callback,
    turn_trace_attributes,
)
from app.core.settings import Settings, get_settings
from app.db.models import AgentConversationSummaryRow, MessageLogRow
from app.db.session import create_db_engine, make_session_factory
from app.evidence.injection import build_evidence_injection

logger = logging.getLogger(__name__)

# engine_version-style provenance for the agent pipeline itself (§6):
# the version of prompt + tool set that produced a turn.
AGENT_VERSION = "0.1.0"

# WhatsApp per-message budget (§9.2): ~1,000 characters. The split happens
# at the CHARACTER budget (settings ``agent_whatsapp_message_char_limit``),
# not at WhatsApp's actual UTF-8 byte limit — the ODD constraint is stated
# in characters, Twilio's own WhatsApp message cap (1,600 chars) is
# likewise enforced in characters, and byte counting would needlessly
# fragment accented Spanish text (multi-byte) while never protecting
# anything the character count misses. See ``split_reply``.
WHATSAPP_MESSAGE_CHAR_LIMIT = 1000

# --- On-demand /sync command (SCH-4, brief §11) -------------------------
#
# An inbound message whose body IS the command triggers the SAME idempotent
# job the beat entry runs (``app.scheduler.tasks.run_sync_async``, the
# function ``sync_intervals`` wraps) and answers with a SHORT confirmation
# through the normal send path (outbound audit row BEFORE the Twilio
# send). The command invokes NO model — the check sits before the budget
# guardrail and the agent build, so the LLM budget neither blocks nor pays
# for it, and the ``MessageSid`` anchor above it bounds a Twilio retry of
# the same command message at one sync run.

# The command, exact after normalization (see ``matches_sync_command``).
SYNC_COMMAND = "/sync"


def matches_sync_command(body: str) -> bool:
    """Whether an inbound body IS the ``/sync`` command (not merely contains it).

    Matching rule (documented SCH-4 decision): the body, with SURROUNDING
    WHITESPACE stripped (WhatsApp delivers what the keyboard sent, often
    with a trailing space or newline) and CASE-FOLDED (mobile autocapitalize
    turns ``/sync`` into ``/Sync``), must EQUAL ``/sync`` exactly. A message
    that merely CONTAINS the word — ``/sync ahora``, ``haz /sync``,
    ``sync`` — is NOT the command and goes down the normal agent path:
    only an unambiguous bare command triggers a data job silently.
    """
    return body.strip().lower() == SYNC_COMMAND


def sync_confirmation(summary: dict[str, Any]) -> str:
    """SHORT bilingual confirmation from the sync job's summary.

    Same Spanish-first convention as the other system-generated replies
    (``app.agent.budget``). Honest about partial failure: failed windows
    are named, never hidden.
    """
    if summary.get("ok"):
        return (
            f"✅ Sync de Intervals.icu completado: "
            f"{summary.get('activities_synced', 0)} actividades, "
            f"{summary.get('streams_synced', 0)} streams, "
            f"{summary.get('wellness_synced', 0)} registros de bienestar.\n"
            f"✅ Intervals.icu sync finished: {summary.get('activities_synced', 0)} "
            f"activities, {summary.get('streams_synced', 0)} streams, "
            f"{summary.get('wellness_synced', 0)} wellness records."
        )
    failed = len(summary.get("failed_windows", []) or [])
    return (
        f"⚠️ El sync terminó con errores en {failed} ventana(s); se reintentará "
        "automáticamente.\n"
        f"⚠️ The sync finished with errors in {failed} window(s); it will retry "
        "automatically."
    )


SYNC_FAILURE_REPLY = (
    "⚠️ El sync falló; inténtalo de nuevo con /sync.\n"
    "⚠️ The sync failed; please try /sync again."
)

# The sync job the command runs: an awaitable returning the job's JSON
# summary (the same dict the ``sync_intervals`` task returns).
SyncRunner = Callable[[], Awaitable[dict[str, Any]]]


def _default_sync_runner() -> SyncRunner:
    """Production default: the SAME job the beat entry runs, in-process.

    Lazy import (the scheduler imports nothing from the agent on this path,
    and the WhatsApp task module already imports this one): awaiting
    ``run_sync_async`` runs the job on the caller's event loop — the
    message-processing worker — instead of a nested ``asyncio.run``.
    """
    from app.scheduler.tasks import SYNC_WINDOW_DAYS, run_sync_async

    async def _run() -> dict[str, Any]:
        return await run_sync_async(days=SYNC_WINDOW_DAYS)

    return _run


async def _handle_sync_command(
    message: dict[str, str],
    *,
    trace_id: str,
    session_factory: SessionFactory,
    twilio_client: Any,
    runner: SyncRunner,
) -> None:
    """Run the /sync job and answer with a short confirmation (SCH-4).

    The outbound audit row is persisted BEFORE the Twilio send (the same
    order as the agent path), shares the command message's ``trace_id`` and
    records the command, the job summary and — on an unexpected failure —
    an explicit ``sync_failed`` flag. An unexpected exception in the job
    never leaves the athlete with silence: an explicit failure message is
    sent instead.
    """
    sender = str(message.get("From", "") or "")
    sync_failed = False
    summary: dict[str, Any] | None = None
    try:
        summary = await runner()
        reply = sync_confirmation(summary)
    except Exception:
        logger.exception("/sync job failed (trace %s)", trace_id)
        sync_failed = True
        reply = SYNC_FAILURE_REPLY
    payload: dict[str, Any] = {
        "to": sender,
        "text": reply,
        "command": SYNC_COMMAND,
        "sync_summary": summary,
    }
    if sync_failed:
        payload["sync_failed"] = True
    async with session_factory() as session:
        session.add(
            MessageLogRow(
                direction="outbound",
                body=reply,
                payload=payload,
                trace_id=trace_id,
                agent_version=AGENT_VERSION,
            )
        )
        await session.commit()
    twilio_client.messages.create(to=message["From"], from_=message["To"], body=reply)
    logger.info(
        "/sync job answered for message %s (trace %s)",
        message.get("MessageSid", ""),
        trace_id,
    )


def split_reply(text: str, limit: int = WHATSAPP_MESSAGE_CHAR_LIMIT) -> list[str]:
    """Split a reply into consecutive messages within the character budget
    (WA-8, §9.2: max ~1,000 characters per message, split if longer).

    NEVER truncates and never drops the tail: the chunks, concatenated,
    reproduce the original text exactly (a chosen separator rides on the
    end of the chunk it closes). Breaks prefer the LAST paragraph
    boundary (``\n\n``) inside the window, then a line break, then a
    space; only separator-free text (one giant word) is hard-cut at the
    limit. A reply within the limit is returned as a single chunk.

    The limit is a CHARACTER budget (see ``WHATSAPP_MESSAGE_CHAR_LIMIT``
    for why characters, not UTF-8 bytes).

    Args:
        text: The full reply text.
        limit: Maximum characters per message; must be positive.

    Returns:
        One or more chunks, each within ``limit``, in send order.
    """
    if limit <= 0:
        raise ValueError(f"message char limit must be positive, got {limit!r}")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            index = window.rfind(separator)
            if index >= 1:
                cut = index + len(separator)
                break
        if cut < 1:
            cut = limit  # hard cut: no usable separator in the window
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks

SessionFactory = async_sessionmaker[AsyncSession]


async def process_inbound_message(
    message: dict[str, str],
    *,
    settings: Settings,
    session_factory: SessionFactory,
    model: Any,
    twilio_client: Any,
    session_repository: SessionRepository,
    sync_runner: SyncRunner | None = None,
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
        sync_runner: The awaitable sync job the ``/sync`` command runs
            (SCH-4). Defaults to the SAME job the beat entry runs
            (:func:`app.scheduler.tasks.run_sync_async` with the shared
            ``SYNC_WINDOW_DAYS`` window); tests inject a fake — never the
            real client, never the network.
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

    # 1b. On-demand /sync command (SCH-4, §11): the body IS the command ->
    # run the SAME idempotent job the beat entry runs and answer with a
    # short confirmation. BEFORE the budget guardrail: the command invokes
    # NO model, so the per-conversation LLM token budget neither blocks it
    # nor pays for it. The MessageSid anchor above already bounds a Twilio
    # retry of the same command message at one sync run.
    if matches_sync_command(body):
        await _handle_sync_command(
            message,
            trace_id=trace_id,
            session_factory=session_factory,
            twilio_client=twilio_client,
            runner=sync_runner or _default_sync_runner(),
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
                    payload={
                        "to": sender,
                        "text": BUDGET_EXHAUSTED_REPLY,
                        "budget_exhausted": True,
                    },
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
    # Athlete context (WA-8): a COMPACT block read from the database and
    # the owner configuration AT TURN TIME is appended to the system
    # prompt — identity, configured thresholds with their provenance,
    # units, and every missing threshold named AS MISSING (never
    # defaulted; the same honesty rule the tools follow, §9.3).
    #
    # Conversation memory (WA-8) rides the SDK's own session seam: the
    # session id is the SENDER (one session per conversation — isolation
    # by construction, and a message processed by a different Celery
    # process reloads the earlier turns), persistence goes through the
    # SDK's RepositorySessionManager on OUR SessionRepository, and the
    # rolling-summary policy is a ConversationManager subclass.
    agent_tools.set_session_factory_provider(lambda: session_factory)
    # Medical-disclosure backstop, layer 1 (WA-9, §3): on a medical signal
    # the instruction to include the disclosure is EXPLICIT for this turn —
    # a mandatory sentence appended to the system prompt (the deterministic
    # outcome layer below guarantees the reply itself).
    medical_signal = detect_medical_signal(body)
    system_prompt = SYSTEM_PROMPT + "\n\n" + await build_athlete_context(
        session_factory, settings
    )
    if medical_signal:
        system_prompt += "\n\n" + MEDICAL_PROMPT_REINFORCEMENT
    # Evidence-injection backstop (live finding 2026-10-14, third strike):
    # prompt rules cannot make the model CALL the tool, so the store's
    # material for the owner's CURRENT message is put in front of it — the
    # SAME retrieval + embedding adapters search_evidence uses, injected
    # into THIS turn's system prompt (visible on the DELIVERED prompt,
    # per-turn, never carried forward). Fail-open: nothing qualifies or
    # anything raises -> empty string -> the turn runs exactly as today.
    evidence_block = await build_evidence_injection(session_factory, settings, body)
    if evidence_block:
        system_prompt += "\n\n" + evidence_block
    memory = RollingSummaryConversationManager(
        window_size=settings.agent_memory_window_messages,
        trigger_messages=settings.agent_summary_trigger_messages,
    )
    session_manager = RepositorySessionManager(
        session_id=sender,
        session_repository=session_repository,
    )
    # The latency stamp rides the SDK's callback seam (the handler runs
    # before the SDK ends the agent span — see app.agent.tracing). The
    # holder is resolved during the invocation, after the agent exists.
    agent_holder: list[Any] = []
    agent = _build_agent(
        model,
        session_manager=session_manager,
        conversation_manager=memory,
        system_prompt=system_prompt,
        trace_attributes=turn_trace_attributes(settings),
        callback_handler=turn_latency_callback(agent_holder),
    )
    agent_holder.append(agent)
    result = await agent.invoke_async(body, limits=limits_from_settings(settings))
    reply = _reply_text(result)
    # Medical-disclosure backstop, layer 2 (WA-9, §3): the outcome is
    # guaranteed deterministically. If the model forgot the disclosure on a
    # medical turn, it is APPENDED as one more natural sentence — never
    # replacing the model's content — and the intervention is recorded in
    # the outbound audit payload below instead of staying silent.
    disclosure_applied = False
    if medical_signal and reply.strip():
        reply, disclosure_applied = ensure_medical_disclosure(reply)
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
    # reply is split at the WhatsApp per-message character budget (WA-8,
    # §9.2): each chunk is a REAL message, so each gets its own outbound
    # row (one audit row per send, ``part``/``total`` provenance in the
    # payload) and its own Twilio send, in order. The outbound payload
    # records this turn's usage and latency (WA-5) plus the sender — the
    # conversation key the budget accumulates over.
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
    chunks = split_reply(reply, limit=settings.agent_whatsapp_message_char_limit)
    total_chunks = len(chunks)
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
        for part, chunk in enumerate(chunks, start=1):
            payload: dict[str, Any] = {
                "to": sender,
                # The sent text, per message: the audit trail must answer
                # "what did we actually send?" by itself (live debugging
                # 2026-10-14 had to read the SDK's session store instead,
                # once racing the turn and showing a stale reply). For a
                # split reply each chunk carries its own text next to its
                # part/total, so the whole reply is reconstructible in
                # order from the payload alone. Audit data only — never
                # read back into the model or the conversation memory.
                "text": chunk,
                "usage": usage,
                "latency_ms": latency_ms,
                "part": part,
                "total": total_chunks,
            }
            if disclosure_applied:
                # Observable, not silent: the audit names the backstop.
                payload["medical_disclosure_backstop"] = True
            session.add(
                MessageLogRow(
                    direction="outbound",
                    body=chunk,
                    payload=payload,
                    trace_id=trace_id,
                    agent_version=AGENT_VERSION,
                )
            )
        await session.commit()

    # 5. Send every chunk through the Twilio REST client (reversed
    # addressing: the inbound From is the outbound To), in order — each
    # chunk is a real WhatsApp message within the per-message budget.
    for chunk in chunks:
        twilio_client.messages.create(to=message["From"], from_=message["To"], body=chunk)
    logger.info(
        "WhatsApp reply sent for message %s (trace %s): %d message(s)",
        message_sid,
        trace_id,
        len(chunks),
    )


def _build_agent(
    model: Any,
    *,
    session_manager: SessionManager | None = None,
    conversation_manager: Any | None = None,
    system_prompt: str | None = None,
    trace_attributes: dict[str, Any] | None = None,
    callback_handler: Any | None = None,
) -> Any:
    """Build the Strands agent with OUR system prompt (optionally extended
    with the per-turn athlete context, WA-8), explicit tools and the
    conversation-memory seam (a session manager over our repository + the
    rolling-summary conversation manager).

    ``trace_attributes`` are the SDK's constructor seam for custom
    attributes on the agent's ``invoke_agent`` span (strands/agent/agent.py
    ``trace_attributes`` -> ``custom_trace_attributes``); the pipeline passes
    the prompt version and model id (WA-11). With tracing unconfigured the
    spans are non-recording and the attributes are no-ops — no cost."""
    from strands import Agent

    return Agent(
        model=model,
        system_prompt=(
            system_prompt if system_prompt is not None else SYSTEM_PROMPT
        ),
        tools=tool_list(),
        session_manager=session_manager,
        conversation_manager=conversation_manager,
        trace_attributes=trace_attributes,
        callback_handler=callback_handler,
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
    sync_runner: SyncRunner | None = None,
) -> None:
    """Synchronous seam the Celery task calls; runs the async pipeline.

    Every dependency can be injected for tests; the defaults derive each
    from configuration (``get_settings``, the async DB engine, the model
    factory, the Twilio REST client, and — for the conversation memory —
    :class:`app.agent.session_store.DbSessionRepository` on the configured
    database). This module-level function is the seam tests patch to
    observe the hand-off (same pattern as ``_backfill`` in
    :mod:`app.scheduler.tasks`).

    The defaults are built PER TURN and the engine is disposed inside the SAME
    event loop. They must NOT be cached across turns: every task here runs its
    own ``asyncio.run``, and asyncpg connections belong to the loop that created
    them, so a pool cached from a previous task answers the next one with
    "cannot rollback; the transaction is in error state". Measured in production
    on 2026-10-21: the first turn succeeded, the second died that way, and the
    athlete's message was lost. One pool per message is the cheap price of a turn
    that works.
    """
    resolved_settings = settings if settings is not None else get_settings()
    # WA-11: wire the Langfuse OTLP exporter once per worker process BEFORE
    # the first turn. Unconfigured settings take the clean no-op path; a
    # wiring failure only disables tracing, never the reply.
    configure_langfuse_tracing(resolved_settings)

    engine = None
    if session_factory is not None:
        resolved_sessions = session_factory
    else:
        engine = create_db_engine(resolved_settings.database_url)
        resolved_sessions = make_session_factory(engine)

    async def _run_turn() -> None:
        try:
            await process_inbound_message(
                message,
                settings=resolved_settings,
                session_factory=resolved_sessions,
                model=(
                    model if model is not None else create_model(resolved_settings)
                ),
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
                # SCH-4: the /sync default job is derived here (the outermost
                # seam), like every other dependency default.
                sync_runner=(
                    sync_runner if sync_runner is not None else _default_sync_runner()
                ),
            )
        finally:
            # In the SAME loop on purpose: these connections are bound to it and
            # cannot be closed from another one.
            if engine is not None:
                await engine.dispose()

    asyncio.run(_run_turn())


__all__ = [
    "AGENT_VERSION",
    "SYNC_COMMAND",
    "WHATSAPP_MESSAGE_CHAR_LIMIT",
    "handle_inbound_message",
    "make_twilio_client",
    "matches_sync_command",
    "process_inbound_message",
    "split_reply",
    "sync_confirmation",
    "tool_list",
]
