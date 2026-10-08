"""Deterministic fake Strands ``Model`` — the agent's test double (WA-4).

**What is the SDK's and what is ours** (WA-4/WA-3 docstring contract):

- The agent loop — message assembly, tool execution, tool-result
  round-trips, stop handling, metrics accumulation — is the **Strands
  Agents SDK's**. This class only plays the model: it yields the
  Bedrock-style stream chunks the SDK's ``process_stream`` consumes
  (``messageStart`` / ``contentBlockStart`` / ``contentBlockDelta`` /
  ``contentBlockStop`` / ``messageStop`` / ``metadata``), so the REAL
  loop runs in tests with zero network and zero credentials.
- Ours is the script: each turn is either a plain string (a final text
  answer) or a dict ``{"name": ..., "input": {...}}`` (one tool-use
  turn). Turns are consumed strictly in order; the fake records every
  invocation it received (``invocations``) so tests can assert what the
  loop actually sent.

The fake is intentionally simple: fixed usage metadata (10 in / 5 out /
15 total, 3 ms latency) and no reasoning about content. It is a test
seam shipped inside ``app`` because WA-4 makes it selectable by
configuration (``settings.llm_provider == "fake"``) — the provider
switch must be testable without a real provider.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from pydantic import BaseModel
from strands.models import Model
from strands.types.content import Messages
from strands.types.tools import ToolSpec

# Fixed, deterministic usage/metrics every invocation reports.
USAGE: dict[str, int] = {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
LATENCY_MS = 3


class FakeModel(Model):
    """Scripted ``strands.models.Model`` driving the real agent loop.

    Args:
        turns: One entry per model invocation, consumed in order: a
            ``str`` yields a text answer ending the loop (``end_turn``);
            a ``dict`` with ``name``/``input`` yields one tool-use turn
            (``tool_use`` stop reason) whose input is the dict.
    """

    def __init__(self, turns: list[str | dict[str, Any]]) -> None:
        self._turns: list[str | dict[str, Any]] = list(turns)
        self._config: dict[str, Any] = {}
        # What the loop actually sent us, one entry per invocation:
        # {"messages": [...], "tool_specs": [...], "system_prompt": ...}.
        self.invocations: list[dict[str, Any]] = []
        self.system_prompts: list[str | None] = []

    # --- Model protocol ----------------------------------------------------
    def update_config(self, **model_config: Any) -> None:
        """Update the model configuration (SDK protocol)."""
        self._config.update(model_config)

    def get_config(self) -> Any:
        """Return the model configuration (SDK protocol)."""
        return self._config

    async def structured_output(
        self,
        output_model: type[BaseModel],
        prompt: Messages,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Structured output is not part of the WhatsApp pipeline (WA-3);
        present only to complete the Model protocol."""
        yield {}

    async def stream(  # type: ignore[override]
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield the scripted turn as Bedrock-style stream chunks.

        The SDK's ``process_stream`` assembles these into the assistant
        message, executes requested tools and drives the loop — all of
        which is the SDK's, not ours.
        """
        try:
            turn = self._turns.pop(0)
        except IndexError:
            raise RuntimeError(
                "FakeModel ran out of scripted turns: the agent made more "
                "model invocations than the script provides"
            ) from None

        self.invocations.append(
            {
                "messages": messages,
                "tool_specs": tool_specs,
                "system_prompt": system_prompt,
            }
        )
        self.system_prompts.append(system_prompt)

        yield {"messageStart": {"role": "assistant"}}
        if isinstance(turn, dict):
            tool_input_json = json.dumps(turn.get("input", {}))
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "fake-tool-use-1", "name": turn["name"]}}
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": tool_input_json}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": turn}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": dict(USAGE),
                "metrics": {"latencyMs": LATENCY_MS},
            }
        }

    # Convenience for tests/inspection.
    @property
    def remaining_turns(self) -> int:
        """How many scripted turns have not been consumed."""
        return len(self._turns)
