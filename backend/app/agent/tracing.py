"""Per-turn tracing of the WhatsApp agent to Langfuse over the Strands SDK's
own OpenTelemetry surface (WA-11, PROJECT_BRIEF §13) — deliberately NOT a
hand-rolled tracer.

**The SDK seams (read from the installed strands-agents source, not guessed):**

- ``strands.telemetry.config.StrandsTelemetry`` installs the global OTel
  ``TracerProvider``; ``setup_otlp_exporter(**kwargs)`` (telemetry/config.py,
  ``setup_otlp_exporter``) attaches ``BatchSpanProcessor(OTLPSpanExporter)``
  to it. This module only decides WHETHER to call it and with WHICH endpoint
  and credentials — the span creation, the loop instrumentation and the
  export pipeline are entirely the SDK's.
- The ``Agent`` builds its spans with its own ``Tracer``
  (``strands/agent/agent.py``: ``self.tracer = get_tracer()``,
  ``_start_agent_trace_span`` / ``_end_agent_trace_span``): one
  ``invoke_agent <name>`` span per turn (carrying ``gen_ai.usage.*`` token
  totals from ``result.metrics.accumulated_usage`` and, when the endpoint is
  detected as Langfuse, ``langfuse.observation.type``), one ``chat`` span
  per model call (carrying ``gen_ai.server.request.duration`` — the model
  latency), and one ``execute_tool <name>`` span per tool call (carrying
  ``gen_ai.tool.name`` / ``gen_ai.tool.call.id`` plus the tool INPUT and
  OUTPUT as span events under the SDK's default conventions). This module
  adds nothing to that — it only supplies the constructor-level
  ``trace_attributes`` the SDK merges into the agent span.

**What one turn's trace carries (§13):**

- **prompt version** — ``PROMPT_VERSION`` from :mod:`app.agent.prompt`
  (the prompt lives in the repository; bump the constant whenever
  ``SYSTEM_PROMPT`` changes behaviour) stamped as
  ``PROMPT_VERSION_ATTRIBUTE`` on every agent span;
- **tool calls with inputs/outputs** — the SDK's ``execute_tool`` spans;
- **tokens** — ``gen_ai.usage.*`` on the agent span (SDK-reported);
- **cost** — the SDK emits no cost; Langfuse derives it from the model id
  plus the token usage, so the configured model id is stamped as
  ``MODEL_ID_ATTRIBUTE`` on every agent span (the SDK only emits
  ``gen_ai.request.model`` when the model object exposes a ``model_id``
  config, which not every provider object does);
- **latency** — ``gen_ai.server.request.duration`` per model call (SDK) and
  the turn's accumulated latency from ``result.metrics.accumulated_metrics``
  stamped as ``TURN_LATENCY_ATTRIBUTE`` on the agent span
  (:func:`turn_latency_callback` — the callback handler runs BEFORE the SDK
  ends the agent span, agent.py ``callback_handler(result=result)`` then
  ``_end_agent_trace_span``).

**Configuration contract (never silently wrong):**

- Nothing set (the pre-WA-11 state): clean no-op — no provider is
  installed, no environment variable is touched; the SDK's spans are
  non-recording and cost nothing. The turn behaves exactly as before.
- Keys present but ``LANGFUSE_HOST`` empty: tracing is NOT fully
  configured. ONE warning naming ``LANGFUSE_HOST`` is logged and the no-op
  path is taken. DELIBERATE CHOICE: no default endpoint is ever guessed —
  a guessed endpoint that is not the owner's would send traces nowhere
  with zero signal, the exact silent failure mode this configuration must
  not repeat.
- All three (public key, secret key, host) set: the OTLP/HTTP exporter is
  built for ``{host}/api/public/otel/v1/traces`` with HTTP Basic auth
  (public:secret). The OTLP environment surface
  (``OTEL_EXPORTER_OTLP_ENDPOINT``, ``LANGFUSE_BASE_URL``) is written so
  the SDK's own Langfuse detection (``strands.telemetry.tracer``
  ``Tracer.is_langfuse``) engages — it gates Langfuse-specific span
  attributes.

**Tracing is never a failure path:** exporter failures never reach the turn
(the OTel span processors catch exporter exceptions —
``opentelemetry/sdk/trace/export`` ``SimpleSpanProcessor.on_end`` wraps
``export`` in try/except; ``BatchSpanProcessor`` isolates export in its
worker thread), and a failure while WIRING is caught here, logged, and
answered with tracing OFF — the turn completes and the owner still gets a
reply.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider

from app.agent.prompt import PROMPT_VERSION
from app.core.settings import Settings

logger = logging.getLogger(__name__)

# Custom trace attributes stamped on every agent span (the SDK merges
# constructor ``trace_attributes`` into ``invoke_agent`` spans). The
# ``ai.coach.`` namespace keeps ours disjoint from the SDK's ``gen_ai.*``
# semantic conventions.
PROMPT_VERSION_ATTRIBUTE = "ai.coach.prompt_version"
MODEL_ID_ATTRIBUTE = "ai.coach.model_id"
TURN_LATENCY_ATTRIBUTE = "ai.coach.turn.latency_ms"

# Langfuse's OTLP/HTTP trace endpoint path (Langfuse docs: the OTel
# endpoint is {host}/api/public/otel/v1/traces).
OTLP_TRACES_PATH = "/api/public/otel/v1/traces"

# The decision is once per process: attaching a second exporter would
# duplicate every span, and re-deciding per turn would re-log the warning.
_decision: bool | None = None


def langfuse_otlp_endpoint(host: str) -> str:
    """The OTLP/HTTP trace endpoint for a Langfuse host."""
    return host.rstrip("/") + OTLP_TRACES_PATH


def langfuse_basic_auth(public_key: str, secret_key: str) -> str:
    """HTTP Basic auth header value for the Langfuse OTLP endpoint."""
    encoded = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return f"Basic {encoded}"


def check_langfuse_configuration(settings: Settings) -> str | None:
    """Return ``None`` when tracing is fully configured, otherwise the reason
    it is not (a human-readable string naming the missing variable).

    Fully configured means ALL THREE of ``langfuse_public_key``,
    ``langfuse_secret_key`` and ``langfuse_host`` are non-empty. Partial
    configurations are treated as NOT configured — never a guessed endpoint.
    """
    missing: list[str] = []
    if not settings.langfuse_public_key:
        missing.append("LANGFUSE_PUBLIC_KEY")
    if not settings.langfuse_secret_key:
        missing.append("LANGFUSE_SECRET_KEY")
    if not settings.langfuse_host:
        missing.append("LANGFUSE_HOST")
    if not missing:
        return None
    return (
        "Langfuse tracing is not fully configured: missing "
        + ", ".join(missing)
        + " (tracing stays off; no default endpoint is guessed)"
    )


def configure_langfuse_tracing(settings: Settings, *, telemetry: Any = None) -> bool:
    """Wire the SDK's OTLP exporter to Langfuse when — and only when — the
    Langfuse settings are fully configured; return whether tracing is on.

    Decided ONCE per process (the decision is cached): calling it again is
    a no-op that returns the same decision, so per-turn callers cannot
    attach duplicate exporters. Unconfigured or partially configured is a
    clean no-op (ONE warning when PARTIALLY configured, naming the missing
    variable — silent when nothing at all is set, exactly the pre-WA-11
    behaviour). Any failure while wiring is caught, logged, and answered
    with tracing OFF: tracing must never become a failure path of the turn.

    Args:
        settings: Application settings (``langfuse_*`` fields).
        telemetry: Optional pre-built ``StrandsTelemetry`` (tests inject one
            bound to a non-global provider; production uses the default,
            which installs the global provider).
    """
    global _decision
    if _decision is not None:
        return _decision
    _decision = _wire_langfuse(settings, telemetry)
    return _decision


def _wire_langfuse(settings: Settings, telemetry: Any) -> bool:
    reason = check_langfuse_configuration(settings)
    if reason is not None:
        # Partially configured (some Langfuse variable set) is a visible
        # misconfiguration: ONE warning, naming the missing variable.
        # Completely unconfigured is the quiet status quo — no noise.
        touched = bool(
            settings.langfuse_public_key
            or settings.langfuse_secret_key
            or settings.langfuse_host
        )
        if touched:
            logger.warning("%s", reason)
        return False
    try:
        endpoint = langfuse_otlp_endpoint(settings.langfuse_host)
        headers = {
            "Authorization": langfuse_basic_auth(
                settings.langfuse_public_key, settings.langfuse_secret_key
            )
        }
        # The OTLP env surface: the SDK's Tracer reads it for its Langfuse
        # detection (is_langfuse -> langfuse.observation.type and span
        # attribute conventions). Writing it is part of the documented
        # Langfuse+Strands integration.
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
        os.environ["LANGFUSE_BASE_URL"] = settings.langfuse_host

        if telemetry is not None:
            # Injected (tests): attach to the injected telemetry only.
            telemetry.setup_otlp_exporter(endpoint=endpoint, headers=headers)
            logger.info("Langfuse OTLP tracing wired to %s", endpoint)
            return True

        current = trace_api.get_tracer_provider()
        if isinstance(current, SDKTracerProvider):
            # Something else already installed a global provider:
            # set_tracer_provider would be silently IGNORED by the OTel API,
            # so attach the exporter to the provider spans will actually
            # flow through (visible, never silent).
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            logger.warning(
                "An OpenTelemetry TracerProvider is already installed; "
                "attaching the Langfuse OTLP exporter (%s) to it", endpoint,
            )
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
            current.add_span_processor(BatchSpanProcessor(exporter))
            return True

        from strands.telemetry import StrandsTelemetry

        StrandsTelemetry().setup_otlp_exporter(endpoint=endpoint, headers=headers)
        logger.info("Langfuse OTLP tracing wired to %s", endpoint)
        return True
    except Exception:
        # Tracing must never become a failure path: wiring problems (bad
        # package state, constructor errors) leave the turn untouched.
        logger.exception(
            "Langfuse tracing wiring failed; continuing with tracing OFF — "
            "the turn must complete regardless"
        )
        return False


def reset_configuration_for_tests() -> None:
    """TEST-ONLY: forget the once-per-process wiring decision so a test can
    observe :func:`configure_langfuse_tracing` in isolation."""
    global _decision
    _decision = None


def turn_trace_attributes(settings: Settings) -> dict[str, str | int]:
    """The custom trace attributes for one turn's agent span: the prompt
    version (repo constant — bump it with the prompt, :mod:`app.agent.prompt`)
    and the configured model id (what Langfuse needs to derive cost from
    the SDK-reported token usage)."""
    return {
        PROMPT_VERSION_ATTRIBUTE: PROMPT_VERSION,
        MODEL_ID_ATTRIBUTE: settings.llm_model_id,
    }


def turn_latency_callback(agent_holder: list[Any]) -> Any:
    """A Strands callback handler that stamps the turn's latency onto the
    agent span while it is still recording.

    The SDK invokes ``callback_handler(result=result)`` BEFORE it ends the
    agent span (agent.py: the result callback precedes
    ``_end_agent_trace_span``), so this is the one seam where turn-level
    data measured by the loop can still reach the span. The latency is the
    SDK's own accumulated metric (``result.metrics.accumulated_metrics``),
    the same number the pipeline persists in the ``message_log`` audit.

    Args:
        agent_holder: One-element list that will contain the Agent once the
            pipeline has built it (the callback runs later, during the
            invocation, so the forward reference is always resolved).
    """

    def handler(**kwargs: Any) -> None:
        result = kwargs.get("result")
        if result is None or not agent_holder:
            return
        span = getattr(agent_holder[0], "trace_span", None)
        if span is None or not span.is_recording():
            return
        try:
            latency = result.metrics.accumulated_metrics.get("latencyMs")
            if latency is not None:
                span.set_attribute(TURN_LATENCY_ATTRIBUTE, latency)
        except Exception:
            # Never a failure path: an unstamped attribute beats a broken turn.
            logger.warning("could not stamp the turn latency onto the agent span", exc_info=True)

    return handler


__all__ = [
    "MODEL_ID_ATTRIBUTE",
    "PROMPT_VERSION_ATTRIBUTE",
    "TURN_LATENCY_ATTRIBUTE",
    "check_langfuse_configuration",
    "configure_langfuse_tracing",
    "langfuse_basic_auth",
    "langfuse_otlp_endpoint",
    "reset_configuration_for_tests",
    "turn_latency_callback",
    "turn_trace_attributes",
]
