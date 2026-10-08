"""WA-4 RED tests: the provider-agnostic model factory.

Contract (ODD task WA-4; PROJECT_BRIEF §6 "LLM provider behind a thin
adapter interface, switchable by config"):

- ``app.agent.model_factory.create_model(settings)`` returns ONE model
  object chosen purely by configuration (``settings.llm_provider``):
  ``"openai"`` -> the real ``strands`` ``OpenAIModel`` built from the
  settings (never called at construction time — building the client is a
  local operation, no request is made); ``"fake"`` -> the deterministic
  :class:`app.agent.fake_model.FakeModel` every test uses;
- an unknown provider fails loudly (``ValueError``), never silently
  falls back to a real provider;
- the factory never imports provider SDK clients for the ``fake`` path
  beyond what :mod:`strands` itself needs, so tests run without
  credentials.
"""

from __future__ import annotations

from typing import Any

import pytest
from strands.models.openai import OpenAIModel

from app.agent.fake_model import FakeModel
from app.agent.model_factory import create_model
from app.core.settings import Settings


def make_settings(**overrides: Any) -> Settings:
    """Settings isolated from any real .env file (no credentials read)."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestProviderSelection:
    def test_openai_provider_builds_openai_model_from_settings(self) -> None:
        settings = make_settings(
            llm_provider="openai",
            llm_model_id="gpt-4o-mini",
            openai_api_key="sk-test-not-real",
        )
        model = create_model(settings)

        assert isinstance(model, OpenAIModel)
        # Built FROM the settings, without any network call: construction
        # only wires a local client object.
        assert model.get_config()["model_id"] == "gpt-4o-mini"

    def test_fake_provider_builds_the_deterministic_fake(self) -> None:
        settings = make_settings(llm_provider="fake")

        model = create_model(settings)

        assert isinstance(model, FakeModel)
        assert not isinstance(model, OpenAIModel)

    def test_unknown_provider_fails_loudly(self) -> None:
        settings = make_settings(llm_provider="anthropic")

        with pytest.raises(ValueError, match="llm_provider"):
            create_model(settings)

    def test_factory_needs_no_credentials_for_the_fake_path(self) -> None:
        settings = make_settings(llm_provider="fake", openai_api_key="")

        assert isinstance(create_model(settings), FakeModel)
