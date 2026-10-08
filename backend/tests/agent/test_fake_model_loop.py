"""WA-4 RED tests: the deterministic fake ``Model`` drives the REAL Strands loop.

Contract (ODD task WA-4, "Agent runtime: Strands Agents SDK" section):

- :class:`app.agent.fake_model.FakeModel` subclasses
  ``strands.models.Model`` and feeds scripted turns to the SDK's own
  agent loop — the loop (message construction, tool execution, stop
  handling) is the SDK's, never ours;
- a plain text turn ends the loop with ``stop_reason == "end_turn"`` and
  the scripted text as the assistant message;
- a tool-use turn makes the SDK execute the tool and hand the toolResult
  back to the model for the next turn (the fake records what it was
  asked, so the test asserts the loop really round-tripped);
- per-invocation usage metrics (``result.metrics.accumulated_usage``)
  come from the fake's metadata event, deterministically.

Zero network: the fake only yields dicts; nothing dials out.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from strands import Agent
from strands.models.openai import OpenAIModel

from app.agent.fake_model import FakeModel

pytestmark = pytest.mark.anyio


def text_blocks(message: Mapping[str, Any]) -> list[str]:
    """Extract the text blocks of a Strands message."""
    return [block["text"] for block in message["content"] if "text" in block]


def tool_uses(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract the toolUse blocks of a Strands message."""
    return [block["toolUse"] for block in message["content"] if "toolUse" in block]


class TestFakeModelDrivesRealLoop:
    async def test_text_turn_ends_loop_with_scripted_reply(self) -> None:
        model = FakeModel(["Tu forma es buena."])
        agent = Agent(model=model, system_prompt="stub prompt")
        result = agent("¿cómo estoy de forma?")

        assert result.stop_reason == "end_turn"
        assert text_blocks(result.message) == ["Tu forma es buena."]
        # The loop passed the user message and the system prompt to the model.
        assert agent.messages[0]["role"] == "user"
        assert model.system_prompts == ["stub prompt"]

    async def test_tool_use_turn_round_trips_tool_result_to_model(self) -> None:
        model = FakeModel(
            [
                {"name": "stub_tool", "input": {"date_range": "7d"}},
                "Tu CTL es 45.0.",
            ]
        )
        agent = Agent(model=model, system_prompt="stub", tools=[])
        result = agent("¿cómo estoy?")

        # First turn asked for the tool; the loop executed it (unregistered
        # tool -> SDK error result) and handed the result back to the model,
        # which then produced the final answer.
        assert result.stop_reason == "end_turn"
        assert text_blocks(result.message) == ["Tu CTL es 45.0."]
        user_contents = [m["content"] for m in agent.messages if m["role"] == "user"]
        assert any(
            any("toolResult" in block for block in content) for content in user_contents
        ), "the loop must hand the toolResult back to the model"
        # The fake saw the toolResult message on its second invocation.
        assert len(model.invocations) == 2
        second_invocation = model.invocations[1]
        assert any(
            "toolResult" in block
            for message in second_invocation["messages"]
            for block in message["content"]
        )

    async def test_usage_metrics_come_from_the_scripted_metadata(self) -> None:
        model = FakeModel(["ok"])
        agent = Agent(model=model)
        result = agent("hola")

        usage = result.metrics.accumulated_usage
        assert usage["inputTokens"] == 10
        assert usage["outputTokens"] == 5
        assert usage["totalTokens"] == 15
        assert result.metrics.accumulated_metrics["latencyMs"] == 3

    async def test_tool_input_json_is_decoded_by_the_loop(self) -> None:
        model = FakeModel(
            [{"name": "stub_tool", "input": {"date_range": "30d"}}, "ok"]
        )
        agent = Agent(model=model, tools=[])
        agent("go")

        tool_use = tool_uses(agent.messages[1])[0]
        assert tool_use["name"] == "stub_tool"
        assert tool_use["input"] == {"date_range": "30d"}
        # The fake received the user message on its first invocation.
        first_invocation = model.invocations[0]
        assert first_invocation["messages"][0]["role"] == "user"
        assert text_blocks(first_invocation["messages"][0]) == ["go"]


class TestFakeModelIsARealModelSubclass:
    def test_subclasses_strands_model(self) -> None:
        from strands.models import Model

        assert issubclass(FakeModel, Model)
        assert isinstance(FakeModel(["x"]), Model)

    def test_is_not_the_openai_model(self) -> None:
        assert not isinstance(FakeModel(["x"]), OpenAIModel)

    def test_config_round_trip(self) -> None:
        model = FakeModel(["x"])
        model.update_config(model_id="fake-1")
        assert model.get_config()["model_id"] == "fake-1"


def test_scripted_turns_are_consumed_in_order() -> None:
    model = FakeModel(["primero", "segundo"])

    async def consume() -> list[str]:
        seen: list[str] = []
        async for chunk in model.stream([{"role": "user", "content": [{"text": "hola"}]}]):
            if "contentBlockDelta" in chunk and "text" in chunk["contentBlockDelta"]["delta"]:
                seen.append(chunk["contentBlockDelta"]["delta"]["text"])
        return seen

    import asyncio

    assert asyncio.run(consume()) == ["primero"]
    assert asyncio.run(consume()) == ["segundo"]
    with pytest.raises(RuntimeError):
        asyncio.run(consume())
