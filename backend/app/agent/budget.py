"""The WhatsApp agent's budget guardrails (WA-5, brief §9.2).

**What is the SDK's and what is ours** (see the ODD task, "Agent runtime:
Strands Agents SDK"): the tool-calling LOOP — message assembly, tool
execution, tool-result round-trips, stop handling, per-invocation metrics —
is the Strands Agents SDK's. Ours are the guardrails around it:

- :func:`limits_from_settings` builds the per-invocation
  ``Limits(turns=…, output_tokens=…, total_tokens=…)`` from settings; the
  pipeline passes it on EVERY invocation (the constructor takes no limits —
  the trap the ODD section warns about).
- :class:`ConversationBudget` accumulates a conversation's token usage
  across turns from ``result.metrics.accumulated_usage`` and reports when
  the budget is exhausted, so the pipeline can stop with an explicit
  message instead of letting a conversation run unbounded.
- :func:`conversation_usage_tokens` seeds that accumulator from the usage
  persisted in ``message_log`` by previous turns of the same conversation.
  A conversation is the WhatsApp free-form window (§9.1): the 24 h after
  the athlete's last message. The seed query reads the OUTBOUND rows whose
  payload names the sender (``payload["to"]``), summed over that window —
  the persistence side is written by the pipeline on every turn.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from strands.types.agent import Limits

from app.core.settings import Settings
from app.db.models import MessageLogRow

__all__ = [
    "BUDGET_EXHAUSTED_REPLY",
    "GUARDRAIL_STOP_REPLY",
    "ConversationBudget",
    "conversation_usage_tokens",
    "limits_from_settings",
]

# The WhatsApp free-form window (§9.1): a conversation is the 24 h after
# the athlete's last message. Usage older than this window no longer counts
# against the budget.
CONVERSATION_WINDOW = timedelta(hours=24)

BUDGET_EXHAUSTED_REPLY = (
    "He llegado al límite de consumo de esta conversación (presupuesto de "
    "tokens), así que no puedo seguir analizando datos ahora mismo. "
    "El consumo se libera a medida que la conversación envejece (ventana de "
    "24 h); vuelve a intentarlo más tarde.\n\n"
    "I reached this conversation's token budget, so I can't keep analysing "
    "data right now. Usage frees up as the conversation ages out of its "
    "24 h window; please try again later."
)

# Reply when the SDK loop stopped on a per-invocation limit without any
# final text (e.g. ``limit_turns``): the athlete is told why, never left
# with silence or an empty message.
GUARDRAIL_STOP_REPLY = (
    "No he podido completar el análisis dentro de los límites de este turno "
    "(máximo de pasos/tokens). Inténtalo de nuevo con una pregunta más "
    "concreta.\n\n"
    "I couldn't complete the analysis within this turn's limits (max "
    "steps/tokens). Please try again with a more specific question."
)


class ConversationBudget:
    """Accumulated per-conversation token budget (WA-5).

    ``used_tokens`` starts seeded with the conversation's persisted usage
    (prior turns of the same 24 h window) and grows with every
    :meth:`add_usage` of ``result.metrics.accumulated_usage``. The budget is
    exhausted when the accumulated ``totalTokens`` reach ``limit_tokens`` —
    the pipeline then stops BEFORE invoking the model and replies with
    :data:`BUDGET_EXHAUSTED_REPLY`.
    """

    def __init__(self, limit_tokens: int, used_tokens: int = 0) -> None:
        if limit_tokens <= 0:
            raise ValueError(f"limit_tokens must be positive, got {limit_tokens!r}")
        self.limit_tokens = limit_tokens
        self.used_tokens = used_tokens

    @property
    def exhausted(self) -> bool:
        """Whether the accumulated usage has reached the budget."""
        return self.used_tokens >= self.limit_tokens

    @property
    def remaining(self) -> int:
        """Tokens still available before the budget trips."""
        return max(0, self.limit_tokens - self.used_tokens)

    def add_usage(self, usage: dict[str, Any]) -> int:
        """Accumulate one invocation's ``metrics.accumulated_usage``.

        Returns the ``totalTokens`` added (0 when the usage has no
        ``totalTokens`` — never guessed).
        """
        total = usage.get("totalTokens")
        if isinstance(total, int):
            self.used_tokens += total
            return total
        return 0


def limits_from_settings(settings: Settings) -> Limits:
    """Build the per-invocation ``Limits`` from settings (§14).

    Passed to ``agent.invoke_async(prompt, limits=…)`` on EVERY invocation;
    the SDK checks the caps at turn boundaries and stops with
    ``limit_turns`` / ``limit_output_tokens`` / ``limit_total_tokens``.
    """
    return Limits(
        turns=settings.agent_max_turns,
        output_tokens=settings.agent_max_output_tokens,
        total_tokens=settings.agent_max_total_tokens,
    )


async def conversation_usage_tokens(session: AsyncSession, *, sender: str) -> int:
    """Sum the persisted token usage of one conversation (WA-5 seed).

    Reads the outbound ``message_log`` rows written within the free-form
    window whose payload names this sender (``payload["to"]``) and sums
    their ``payload["usage"]["totalTokens"]``. Rows without usable usage
    payloads contribute nothing — never guessed.
    """
    cutoff = datetime.now(UTC) - CONVERSATION_WINDOW
    rows = (
        (
            await session.execute(
                select(MessageLogRow).where(
                    MessageLogRow.direction == "outbound",
                    MessageLogRow.created_at >= cutoff,
                    MessageLogRow.payload["to"].astext == sender,
                )
            )
        )
        .scalars()
        .all()
    )
    total = 0
    for row in rows:
        payload = row.payload or {}
        usage = payload.get("usage")
        if isinstance(usage, dict):
            tokens = usage.get("totalTokens")
            if isinstance(tokens, int):
                total += tokens
    return total
