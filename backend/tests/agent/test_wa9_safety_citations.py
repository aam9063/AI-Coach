"""WA-9 RED tests: the citation rule and medical safety (§3, §8).

Two layers, matching how WA-7/WA-8 already pin prompt behaviour:

- **Content pinned through the real loop**: the assertions run against the
  system prompt the ``FakeModel`` ACTUALLY receives (the ``delivered_prompt``
  helper from :mod:`tests.agent.test_prompt`), not just the module constant.
- **Fake-model end-to-end scenarios** through the real pipeline: the owner
  reports pain and the reply must (a) contain no diagnosis, (b) advise
  reducing or stopping load, (c) refer to a professional. A LONG safety
  reply must still split (WA-8) without losing the referral.

Pinned contract:

Medical safety (§3):
1. On pain/injury/illness signals: NEVER diagnose, NEVER prescribe
   treatment, suggest REDUCING or STOPPING load, and EXPLICITLY recommend
   a doctor or physiotherapist — stating it is not a medical professional.
2. An engine number is not medical clearance: a comfortable readiness
   score does not clear an injury.

Citation honesty (§3, §8):
3. Every scientific claim cites a source as ``(Autor, Año)`` (DOI
   available on request); with no supporting source the agent says so
   explicitly. In the CURRENT state the evidence store (Feature 9) does
   not exist and there is no ``search_evidence`` tool, so the honest form
   is pinned: the agent must NOT invent an ``(Autor, Año)``, must not
   cite anything it was not given by a tool, and the prompt names the
   ``search_evidence`` seam that Feature 9 will plug in.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.agent.fake_model import FakeModel
from app.agent.pipeline import process_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.agent.test_prompt import delivered_prompt
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMwa9ss00000000000000000000001"
FROM = "whatsapp:+34600000001"
TO = "whatsapp:+34600000000"

PAIN_MESSAGE = (
    "Llevo una semana con dolor en el tendón de Aquiles cuando corro. "
    "¿Puedo entrenar hoy?"
)


class TestMedicalSafetyRuleInPrompt:
    async def test_pain_signals_trigger_reduce_or_stop_and_referral(self) -> None:
        prompt = await delivered_prompt()

        # The signals the rule keys on (§3): pain, injury, illness.
        assert "dolor" in prompt.lower()
        assert "lesión" in prompt.lower()
        assert "enfermedad" in prompt.lower()
        # Not a medical professional — stated as such (§3).
        assert "profesional médico" in prompt.lower()
        # Reduce OR stop the load (§3: "reduces load suggestions").
        assert "reducir" in prompt.lower()
        assert "detener" in prompt.lower()
        # Explicit professional referral: doctor or physiotherapist.
        assert "médico" in prompt.lower()
        assert "fisioterapeuta" in prompt.lower()

    async def test_never_diagnose_and_never_prescribe_treatment(self) -> None:
        prompt = await delivered_prompt()

        # Never diagnose (diagnosticar/diagnóstico/diagnostiques — the
        # shared stem is "diagnosti")...
        assert "diagnosti" in prompt.lower()
        # ...and never prescribe treatment ( stretches, corrective
        # exercises, recovery timelines — none of it).
        assert "prescrib" in prompt.lower()

    async def test_engine_number_is_not_medical_clearance(self) -> None:
        prompt = await delivered_prompt()

        # A comfortable readiness score does not clear an injury: the
        # prompt must say a good engine number is NOT a medical clearance
        # and that safety outranks the metric when pain is present.
        assert "readiness" in prompt.lower()
        assert "autorización médica" in prompt.lower()


class TestCitationHonestyRuleInPrompt:
    async def test_citation_format_is_pinned(self) -> None:
        prompt = await delivered_prompt()

        # §8: the exact citation format, DOI available on request.
        assert "(autor, año)" in prompt.lower()
        assert "doi" in prompt.lower()

    async def test_never_invent_a_citation_and_say_when_there_is_no_source(
        self,
    ) -> None:
        prompt = await delivered_prompt()

        # The HONEST form while the evidence store does not exist: the
        # prompt names the missing seam (search_evidence) and forbids
        # invented citations — no fabricated (Autor, Año), nothing cited
        # that no tool handed over.
        assert "search_evidence" in prompt
        assert "no inventes" in prompt.lower()
        # With no supporting source, the agent says so explicitly.
        assert "sin fuente" in prompt.lower() or "no tengo una fuente" in prompt.lower()


class TestMedicalSafetyEndToEnd:
    @pytest.fixture
    async def session_factory(
        self,
    ) -> AsyncGenerator[async_sessionmaker[Any], None]:
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
    def session_repository(self) -> Any:
        """Repository on the DEDICATED test database (never the dev DB)."""
        from app.agent.session_store import DbSessionRepository
        from tests.dbsupport import test_database_url

        return DbSessionRepository(test_database_url())

    @staticmethod
    def _make_settings() -> Settings:
        return Settings(
            _env_file=None,  # type: ignore[call-arg]
            llm_provider="fake",
        )

    class FakeTwilioMessages:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        def create(self, **kwargs: str) -> Any:
            self.calls.append(kwargs)
            return {"sid": "SMout"}

    class FakeTwilioClient:
        def __init__(self) -> None:
            self.messages = TestMedicalSafetyEndToEnd.FakeTwilioMessages()

    async def test_pain_report_gets_reduce_load_referral_and_no_diagnosis(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """The owner reports Achilles pain: the reply must advise reducing
        or stopping load, refer to a professional, say it is not a medical
        professional — and contain NO diagnosis."""
        safety_reply = (
            "Lamento lo del tendón. No puedo decirte qué es ni qué "
            "tratamiento seguir: no soy un profesional médico. "
            "Con dolor al correr, mi sugerencia es reducir o detener la "
            "carga — nada de series ni rodajes largos mientras duela — y "
            "que lo valore un médico o fisioterapeuta. "
            "Tu readiness de hoy puede ser bueno y aun así eso no es una "
            "autorización médica: con dolor, la seguridad manda."
        )
        model = FakeModel([safety_reply])
        twilio = self.FakeTwilioClient()

        await process_inbound_message(
            {"MessageSid": SID, "From": FROM, "To": TO, "Body": PAIN_MESSAGE},
            settings=self._make_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        # The safety rule reached the model ON THIS TURN (the full system
        # prompt the loop handed over, athlete context included).
        delivered = str(model.invocations[0]["system_prompt"])
        assert "profesional médico" in delivered.lower()
        assert "diagnosti" in delivered.lower()
        assert "fisioterapeuta" in delivered.lower()

        # (a) no diagnosis, (b) reduce/stop load, (c) professional referral.
        reply = twilio.messages.calls[0]["body"]
        assert "diagnóstico" not in reply.lower()
        assert "tendinopatía" not in reply.lower()
        assert "no soy un profesional médico" in reply.lower()
        assert "reducir o detener la carga" in reply.lower()
        assert "médico o fisioterapeuta" in reply.lower()

    async def test_long_safety_reply_splits_without_losing_the_referral(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """A LONG safety reply (over the ~1,000-char WhatsApp budget) is
        still split by WA-8's splitter into ordered messages — and the
        referral arrives: no chunk may drop or truncate the advice."""
        filler = (
            "Mientras el dolor siga ahí, prioriza el descanso relativo: "
            "caminar y nadar suave suelen tolerarse mejor que correr, y "
            "mantener el resto de hábitos (sueño, nutrición) ayuda a la "
            "carga general del cuerpo. "
        )
        referral = (
            "Recuerda: no soy un profesional médico y esto no es un "
            "diagnóstico ni un tratamiento. Con dolor en el tendón, "
            "reduce o detener la carga — no fuerces el rodaje de hoy — y "
            "consulta a un médico o fisioterapeuta para que lo valore "
            "antes de volver a correr."
        )
        long_reply = (filler * 6) + referral
        assert len(long_reply) > 1000
        model = FakeModel([long_reply])
        twilio = self.FakeTwilioClient()

        await process_inbound_message(
            {"MessageSid": SID, "From": FROM, "To": TO, "Body": PAIN_MESSAGE},
            settings=self._make_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )

        bodies = [call["body"] for call in twilio.messages.calls]
        # Split into budget-sized messages, nothing truncated, nothing dropped.
        assert len(bodies) >= 2
        assert all(len(body) <= 1000 for body in bodies)
        assert "".join(bodies) == long_reply
        # The referral survives the split: reduce/stop load + professional.
        joined = "".join(bodies).lower()
        assert "reduce o detener la carga" in joined
        assert "médico o fisioterapeuta" in joined
        assert "no soy un profesional médico" in joined
        # ONE outbound audit row per send, in order (WA-8 semantics hold
        # for the safety path too).
        async with session_factory() as session:
            rows = (await session.execute(
                select(MessageLogRow).where(MessageLogRow.direction == "outbound")
            )).scalars().all()
        rows = sorted(rows, key=lambda r: r.id)
        assert [row.body for row in rows] == bodies
        assert all(row.payload["total"] == len(bodies) for row in rows)
