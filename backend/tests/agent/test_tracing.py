"""WA-11 tests: per-turn Langfuse tracing over the SDK's OpenTelemetry surface.

The tracer under test is :mod:`app.agent.tracing`, which wires the Strands
SDK's own telemetry (``strands.telemetry.StrandsTelemetry`` + the Agent's
``Tracer`` spans) to Langfuse over OTLP/HTTP. These tests pin:

- **Unconfigured = clean no-op**: no exporter, no environment change, the
  global tracer provider untouched, and a turn behaves exactly as before
  (WA-11: "unconfigured must be a clean no-op, exactly as today").
- **Keys without host = NOT configured**: ONE visible warning naming
  ``LANGFUSE_HOST`` and the no-op path — a default endpoint is never
  guessed (the empty-host failure mode sends traces nowhere with no
  signal).
- **Configured = correct wiring**: the OTLP exporter is built for
  ``{host}/api/public/otel/v1/traces`` with Basic auth (pk:sk), exactly
  once per process.
- **A turn's spans carry what §13 asks for**: prompt version, tool call
  name/id with input and output, tokens, and latency — asserted on the
  spans the real SDK loop produces.
- **A broken exporter never breaks a reply**: an exporter that raises
  cannot fail the turn — the athlete still gets the answer.
- **handle_inbound_message (the Celery seam) wires tracing once.**

Everything here is OFFLINE: the fake model drives the real SDK loop, spans
are captured with an in-memory exporter on an injected/installed
TracerProvider, and the OTLP exporter class is replaced by a constructor
spy — no test ever contacts Langfuse.
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import AsyncGenerator, Sequence
from typing import Any, cast

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from strands.telemetry.config import StrandsTelemetry

import app.agent.pipeline as pipeline
from app.agent import tools
from app.agent.fake_model import LATENCY_MS, USAGE, FakeModel
from app.agent.pipeline import process_inbound_message
from app.agent.prompt import PROMPT_VERSION
from app.agent.tracing import (
    MODEL_ID_ATTRIBUTE,
    PROMPT_VERSION_ATTRIBUTE,
    TURN_LATENCY_ATTRIBUTE,
    configure_langfuse_tracing,
)
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMtrace000000000000000000000000001"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"
HOST = "https://cloud.langfuse.example"

# The OTLP/Langfuse environment surface the wiring writes (asserted in the
# wiring test and cleaned up around every test).
TRACE_ENV_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "LANGFUSE_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_trace_env_and_decisions() -> Any:
    """Remove the OTLP env surface and reset the once-per-process wiring
    decision around every test, so tests observe the wiring in isolation."""
    import app.agent.tracing as tracing

    saved = {name: os.environ.get(name) for name in TRACE_ENV_VARS}
    for name in TRACE_ENV_VARS:
        os.environ.pop(name, None)
    tracing.reset_configuration_for_tests()
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    tracing.reset_configuration_for_tests()


def make_message(**overrides: str) -> dict[str, str]:
    return {"MessageSid": SID, "From": FROM, "To": TO, "Body": "¿cómo estoy?",
            **overrides}


def make_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="fake",
        openai_api_key="",
        twilio_account_sid="",
        twilio_auth_token="",
        **overrides,
    )


class FakeTwilioMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create(self, **kwargs: str) -> Any:
        self.calls.append(kwargs)
        return {"sid": "SMout"}


class FakeTwilioClient:
    def __init__(self) -> None:
        self.messages = FakeTwilioMessages()


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[Any], None]:
    try:
        engine: AsyncEngine = await create_test_engine()
    except Exception:
        pytest.skip("Postgres unreachable; pipeline tests require the compose Postgres")
    from app.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tools.set_session_factory_provider(lambda: factory)
    yield factory
    tools.reset_session_factory_provider()
    await engine.dispose()


@pytest.fixture
def session_repository() -> Any:
    """Repository on the DEDICATED test database (never the dev DB)."""
    from app.agent.session_store import DbSessionRepository
    from tests.dbsupport import test_database_url

    return DbSessionRepository(test_database_url())


@pytest.fixture
def memory_spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Install a recording global tracer provider whose spans land in an
    in-memory exporter, and drop the SDK's cached Tracer singleton so the
    next Agent binds a fresh Tracer to THIS provider (restored afterwards).

    The Agent's spans (invoke_agent / chat / execute_tool) are the SDK's
    own (strands/agent/agent.py ``_start_agent_trace_span``), so capturing
    them here exercises exactly what production would export.
    """
    import strands.telemetry.tracer as tracer_module

    exporter = InMemorySpanExporter()
    current = trace_api.get_tracer_provider()
    if isinstance(current, SDKTracerProvider):
        # A previous test in this process already installed a real global
        # provider (set_tracer_provider would be ignored): attach to it.
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider = SDKTracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace_api.set_tracer_provider(provider)
    monkeypatch.setattr(tracer_module, "_tracer_instance", None)
    return exporter


async def run_turn(
    session_factory: async_sessionmaker[Any],
    session_repository: Any,
    model: FakeModel,
    settings: Settings | None = None,
) -> FakeTwilioClient:
    twilio = FakeTwilioClient()
    await process_inbound_message(
        make_message(),
        settings=settings if settings is not None else make_settings(),
        session_factory=session_factory,
        model=model,
        twilio_client=twilio,
        session_repository=session_repository,
    )
    return twilio


async def outbound_bodies(
    factory: async_sessionmaker[Any],
) -> list[str]:
    async with factory() as session:
        rows = (await session.execute(
            select(MessageLogRow).where(MessageLogRow.direction == "outbound")
        )).scalars().all()
    return [row.body for row in sorted(rows, key=lambda r: r.id)]


def attributes_of(span: ReadableSpan) -> dict[str, Any]:
    """A span's attributes as a plain dict (the OTel type is optional)."""
    return dict(span.attributes or {})


def event_attributes(span: ReadableSpan) -> list[dict[str, Any]]:
    """A span's events' attributes as plain dicts."""
    return [dict(event.attributes or {}) for event in span.events]


class TestUnconfiguredIsACleanNoOp:
    async def test_no_exporter_no_env_change_and_the_turn_is_unchanged(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nothing configured (the .env state before WA-11): configure is a
        silent no-op — no OTLP env surface, the global tracer provider is
        untouched — and a full turn behaves exactly as before WA-11."""
        provider_before = trace_api.get_tracer_provider()
        caplog.set_level(logging.WARNING, logger="app.agent.tracing")

        assert configure_langfuse_tracing(make_settings()) is False

        for name in TRACE_ENV_VARS:
            assert name not in os.environ, f"{name} must not be set when tracing is unconfigured"
        assert trace_api.get_tracer_provider() is provider_before
        # Clean no-op means SILENT when nothing at all is configured.
        assert caplog.records == []

        # The turn itself: same reply, same persisted usage/latency audit.
        twilio = await run_turn(
            session_factory, session_repository, FakeModel(["Respuesta directa."])
        )
        assert twilio.messages.calls == [
            {"to": FROM, "from_": TO, "body": "Respuesta directa."}
        ]
        async with session_factory() as session:
            rows = (await session.execute(
                select(MessageLogRow).where(MessageLogRow.direction == "outbound")
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].payload is not None
        assert rows[0].payload["usage"] == dict(USAGE)
        assert rows[0].payload["latency_ms"] == LATENCY_MS


class TestKeysWithoutHostIsNotConfigured:
    async def test_warns_once_naming_langfuse_host_and_takes_the_no_op_path(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keys present but LANGFUSE_HOST empty: tracing is NOT configured —
        ONE warning that names LANGFUSE_HOST, no env change, and the turn
        completes exactly as unconfigured. Deliberate: no default endpoint
        is ever guessed."""
        caplog.set_level(logging.WARNING)
        settings = make_settings(
            langfuse_public_key="pk-lf-test",
            langfuse_secret_key="sk-lf-test",
            langfuse_host="",
        )

        assert configure_langfuse_tracing(settings) is False

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and r.name == "app.agent.tracing"]
        assert len(warnings) == 1, "exactly ONE warning must name the missing variable"
        assert "LANGFUSE_HOST" in warnings[0].getMessage()
        for name in TRACE_ENV_VARS:
            assert name not in os.environ
        assert isinstance(trace_api.get_tracer_provider(), trace_api.ProxyTracerProvider)

        # The no-op path: the turn still runs end-to-end.
        twilio = await run_turn(session_factory, session_repository,
                                FakeModel(["Sigo funcionando sin traza."]), settings)
        assert twilio.messages.calls[0]["body"] == "Sigo funcionando sin traza."


class TestConfiguredWiring:
    def test_builds_the_langfuse_otlp_exporter_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fully configured: an OTLP/HTTP exporter is attached for
        {host}/api/public/otel/v1/traces with Basic auth (pk:sk), the OTLP
        env surface is written (the SDK's Langfuse detection reads it), and
        a second configure call is a no-op (once per process)."""
        from opentelemetry.sdk.trace.export import SpanExportResult

        constructed: list[dict[str, Any]] = []

        class SpyOTLPSpanExporter:
            def __init__(self, **kwargs: Any) -> None:
                constructed.append(kwargs)

            def export(self, spans: Any) -> Any:
                return SpanExportResult.SUCCESS

            def shutdown(self) -> Any:
                return None

            def force_flush(self, timeout_millis: int = 30000) -> bool:
                return True

        monkeypatch.setattr(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            SpyOTLPSpanExporter,
        )
        # Injected provider: StrandsTelemetry attaches its exporter HERE,
        # never to the process-global provider (no cross-test pollution).
        telemetry = StrandsTelemetry(tracer_provider=SDKTracerProvider())
        settings = make_settings(
            langfuse_public_key="pk-lf-test",
            langfuse_secret_key="sk-lf-test",
            langfuse_host=HOST,
        )

        assert configure_langfuse_tracing(settings, telemetry=telemetry) is True
        endpoint = f"{HOST}/api/public/otel/v1/traces"
        expected_auth = "Basic " + base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
        assert constructed == [
            {"endpoint": endpoint, "headers": {"Authorization": expected_auth}}
        ]
        assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == endpoint
        assert os.environ["LANGFUSE_BASE_URL"] == HOST

        # Once per process: a second call must not attach a second exporter.
        assert configure_langfuse_tracing(settings) is True
        assert len(constructed) == 1

        telemetry.tracer_provider.shutdown()


class TestTurnSpans:
    async def test_a_turn_produces_spans_carrying_prompt_version_tools_tokens_latency(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        memory_spans: InMemorySpanExporter,
    ) -> None:
        """One turn with a tool call, through the REAL SDK loop: the agent
        span carries the prompt version (repo constant), the model id, the
        turn latency and the accumulated token usage; the tool span names
        the tool and records its input and output (the SDK records tool
        content as span events under the default conventions)."""
        model = FakeModel([
            {"name": "get_load_status", "input": {"date_range": "7d"}},
            "Respuesta final con traza.",
        ])
        await run_turn(session_factory, session_repository, model)

        memory_spans.force_flush()
        spans = memory_spans.get_finished_spans()
        agent_span = next(s for s in spans if s.name.startswith("invoke_agent"))
        tool_span = next(s for s in spans if s.name == "execute_tool get_load_status")

        attributes = attributes_of(agent_span)
        # Prompt version: explicit, from the repo (app.agent.prompt).
        assert attributes[PROMPT_VERSION_ATTRIBUTE] == PROMPT_VERSION
        # Model id: what Langfuse needs to derive cost from usage.
        assert attributes[MODEL_ID_ATTRIBUTE] == make_settings().llm_model_id
        # Latency: the turn's accumulated latency, stamped from the SDK's
        # own metrics. The scripted turn makes TWO model calls (tool turn +
        # final answer), each reporting FakeModel's deterministic 3 ms, so
        # the SDK accumulates 2 x LATENCY_MS.
        assert attributes[TURN_LATENCY_ATTRIBUTE] == 2 * LATENCY_MS
        # Tokens: the SDK reports the turn's accumulated usage on the agent
        # span — likewise accumulated over both model calls.
        assert attributes["gen_ai.usage.total_tokens"] == 2 * USAGE["totalTokens"]
        assert attributes["gen_ai.usage.input_tokens"] == 2 * USAGE["inputTokens"]
        assert attributes["gen_ai.usage.output_tokens"] == 2 * USAGE["outputTokens"]

        # Tool call: name, id, and the INPUT and OUTPUT of the call.
        tool_attributes = attributes_of(tool_span)
        assert tool_attributes["gen_ai.tool.name"] == "get_load_status"
        assert tool_attributes["gen_ai.tool.call.id"]
        events = event_attributes(tool_span)
        assert any("date_range" in str(event.get("content", "")) for event in events), \
            "the tool INPUT must be recorded on the tool span"
        assert any(str(event.get("id")) == str(tool_attributes["gen_ai.tool.call.id"])
                   for event in events), \
            "the tool OUTPUT must be recorded against the same call id"


class TestBrokenExporterIsHarmless:
    async def test_an_exporter_that_raises_never_breaks_the_reply(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """Tracing is never a failure path: an exporter that raises on every
        export must leave the turn untouched — the reply still goes out and
        the audit rows are still written."""
        from opentelemetry import trace as trace_api

        class ExplodingExporter(SpanExporter):
            def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
                raise RuntimeError("Langfuse unreachable")

            def shutdown(self) -> None:
                return None

        current = trace_api.get_tracer_provider()
        assert isinstance(current, SDKTracerProvider)
        current.add_span_processor(SimpleSpanProcessor(ExplodingExporter()))

        twilio = await run_turn(session_factory, session_repository,
                                FakeModel(["La respuesta llegó igual."]))

        assert twilio.messages.calls == [
            {"to": FROM, "from_": TO, "body": "La respuesta llegó igual."}
        ]
        assert await outbound_bodies(session_factory) == ["La respuesta llegó igual."]


class TestProductionSeamWiresTracing:
    def test_handle_inbound_message_configures_tracing_once(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Celery entry point wires tracing from settings (once per
        process) before running the turn — the production path cannot skip
        it, and tests that inject everything still observe the hand-off."""
        configured: list[Settings] = []

        def fake_configure(resolved: Settings) -> bool:
            configured.append(resolved)
            return True

        monkeypatch.setattr(pipeline, "configure_langfuse_tracing", fake_configure)

        async def fake_process(message: dict[str, str], **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(pipeline, "process_inbound_message", fake_process)
        settings = make_settings()

        pipeline.handle_inbound_message(
            make_message(), settings=settings, session_factory=None,
            model=FakeModel([]), twilio_client=FakeTwilioClient(),
            session_repository=cast(Any, object()),
        )

        assert configured == [settings]
