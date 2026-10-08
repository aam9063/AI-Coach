"""Provider-agnostic LLM model factory (WA-4, PROJECT_BRIEF §6).

§6: "the LLM provider sits behind a thin adapter interface, OpenAI or
Anthropic, switchable by configuration" — with the Strands Agents SDK the
adapter is Strands' own ``strands.models.Model`` interface: one model
object is handed to the agent, and this factory chooses WHICH object from
``settings.llm_provider``:

- ``"openai"``: the real ``strands.models.openai.OpenAIModel`` built from
  ``settings.openai_api_key`` / ``settings.llm_model_id``. Constructing it
  wires a local client object only — no request is made at construction
  time; the first provider call happens when the agent runs in production.
- ``"fake"``: the deterministic :class:`app.agent.fake_model.FakeModel`,
  which drives the real Strands loop with zero network and zero
  credentials — the provider every automated test uses.
- anything else fails loudly (``ValueError``): a typo in the provider
  configuration must never silently fall back to a real provider.

**Division of responsibility**: the model OBJECT (and the conversation
loop it feeds) is the SDK's; ours is the selection policy and the
configuration plumbing around it.
"""

from __future__ import annotations

from strands.models import Model
from strands.models.openai import OpenAIModel

from app.agent.fake_model import FakeModel
from app.core.settings import Settings

PROVIDER_OPENAI = "openai"
PROVIDER_FAKE = "fake"


def create_model(settings: Settings) -> Model:
    """Build the ONE model object the agent is handed, per configuration.

    Raises:
        ValueError: ``settings.llm_provider`` names no known provider.
    """
    if settings.llm_provider == PROVIDER_OPENAI:
        return OpenAIModel(
            model_id=settings.llm_model_id,
            client_args={"api_key": settings.openai_api_key},
        )
    if settings.llm_provider == PROVIDER_FAKE:
        return FakeModel([])
    raise ValueError(
        f"unknown llm_provider {settings.llm_provider!r}: "
        f"expected {PROVIDER_OPENAI!r} or {PROVIDER_FAKE!r}"
    )
