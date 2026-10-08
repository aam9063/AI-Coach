"""WA-5 RED tests: the agent's guardrails around the SDK loop (§9.2).

The tool-calling LOOP is the Strands Agents SDK's; ours are the guardrails:

- ``limits=Limits(turns=…, output_tokens=…, total_tokens=…)`` built from
  settings and passed on EVERY invocation;
- a per-conversation token budget accumulated from
  ``result.metrics.accumulated_usage`` across the turns of one conversation
  (the WhatsApp free-form window, §9.1: the 24 h after the last user
  message), enforced BEFORE invoking the model — an exhausted conversation
  gets an explicit message instead of an unbounded run.

Pure-unit tests (no DB, no network); the pipeline-level enforcement is
pinned in ``tests/agent/test_pipeline_guardrails.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.budget import (
    BUDGET_EXHAUSTED_REPLY,
    ConversationBudget,
    limits_from_settings,
)
from app.core.settings import Settings


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestConversationBudget:
    def test_accumulates_usage_and_reports_remaining(self) -> None:
        budget = ConversationBudget(limit_tokens=100)
        assert not budget.exhausted
        assert budget.add_usage({"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}) == 15
        assert budget.used_tokens == 15
        assert budget.remaining == 85
        assert not budget.exhausted

    def test_exhausted_when_used_reaches_the_limit(self) -> None:
        budget = ConversationBudget(limit_tokens=30, used_tokens=15)
        budget.add_usage({"totalTokens": 15})
        assert budget.used_tokens == 30
        assert budget.exhausted
        assert budget.remaining == 0

    def test_exhausted_when_seeded_past_the_limit(self) -> None:
        budget = ConversationBudget(limit_tokens=10, used_tokens=99)
        assert budget.exhausted

    def test_add_usage_tolerates_a_missing_total(self) -> None:
        budget = ConversationBudget(limit_tokens=10)
        assert budget.add_usage({}) == 0
        assert not budget.exhausted

    def test_exhausted_budget_message_is_explicit(self) -> None:
        # The athlete must be told WHY the conversation stopped (§9.3 spirit:
        # never a silent stop, never an invented answer).
        text = BUDGET_EXHAUSTED_REPLY.lower()
        assert "budget" in text
        assert BUDGET_EXHAUSTED_REPLY.strip() != ""


class TestLimitsFromSettings:
    def test_builds_the_per_invocation_limits_from_settings(self) -> None:
        settings = make_settings(
            agent_max_turns=4,
            agent_max_output_tokens=1234,
            agent_max_total_tokens=5678,
        )
        limits = limits_from_settings(settings)
        assert limits == {"turns": 4, "output_tokens": 1234, "total_tokens": 5678}

    def test_settings_defaults_are_positive(self) -> None:
        settings = make_settings()
        assert settings.agent_max_turns > 0
        assert settings.agent_max_output_tokens > 0
        assert settings.agent_max_total_tokens > 0
        assert settings.agent_conversation_token_budget > 0


def test_limits_type_is_the_sdk_typed_dict() -> None:
    from strands.types.agent import Limits

    limits: Limits = limits_from_settings(make_settings())
    assert set(limits) <= {"turns", "output_tokens", "total_tokens"}


@pytest.mark.parametrize("field", ["agent_max_turns", "agent_max_output_tokens",
                                   "agent_max_total_tokens", "agent_conversation_token_budget"])
def test_guardrail_settings_fields_exist(field: str) -> None:
    assert hasattr(make_settings(), field)
