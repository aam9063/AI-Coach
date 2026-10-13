"""WA-9 backstop RED tests: the deterministic medical-disclosure guarantee.

The live evidence (owner turn, 2026-10): the reply correctly reduced load,
avoided the intense session, referred to a doctor/physiotherapist and
diagnosed nothing — but OMITTED the explicit "no soy un profesional médico"
disclosure that prompt rule 5 requires. A prompt pin reaches the model; it
does not make the model obey it. So the guarantee is implemented where it
cannot be forgotten, in two layers:

1. **Prompt reinforcement (this turn)**: when the inbound message carries a
   medical signal, the turn's system prompt carries an explicit, mandatory
   sentence to include the disclosure (asserted through what the FakeModel
   actually receives).
2. **Deterministic outcome**: after the model replies, a medical turn whose
   reply lacks the disclosure gets it APPENDED as a natural sentence
   (never replacing content), and the intervention is OBSERVABLE in the
   audit (``medical_disclosure_backstop`` in the outbound payload) instead
   of silent.

Contracts pinned here:

- The detector (:func:`app.agent.medical_backstop.detect_medical_signal`)
  is conservative, Spanish + English, accent- and inflection-tolerant,
  with its trade-off documented (a false positive costs one extra
  sentence; a false negative is today's behaviour).
- A NON-medical turn is left untouched: no injected sentence, no audit
  flag, no prompt reinforcement.
- A medical turn whose reply already contains the disclosure does NOT get
  it twice.
- Splitting (WA-8) still holds on the medical path: one audit row per
  send, ``part``/``total``, nothing truncated, nothing dropped — and a
  long medical reply loses neither the referral nor the appended
  disclosure.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent import tools
from app.agent.fake_model import FakeModel
from app.agent.medical_backstop import (
    MEDICAL_DISCLOSURE,
    MEDICAL_PROMPT_REINFORCEMENT,
    detect_medical_signal,
    ensure_medical_disclosure,
    reply_contains_disclosure,
)
from app.agent.pipeline import process_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import create_test_engine

pytestmark = pytest.mark.anyio

SID = "SMbackstopss00000000000000000001"
FROM = "whatsapp:+34600000011"
TO = "whatsapp:+34600000000"

PAIN_MESSAGE = (
    "Llevo una semana con dolor en el tendón de Aquiles cuando corro. "
    "¿Puedo entrenar hoy?"
)
PLAIN_MESSAGE = "¿Cómo están mi CTL y mi TSB esta semana?"


# ---------------------------------------------------------------------------
# Layer 0: the detector (unit)
# ---------------------------------------------------------------------------


class TestMedicalSignalDetector:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            # --- Spanish: pain ------------------------------------------
            ("Llevo una semana con dolor en el tendón de Aquiles", True),
            ("me duele la rodilla al correr", True),
            ("ayer me dolía la espalda", True),
            ("sigo doliendo del tobillo", True),
            ("tengo dolores después del rodaje", True),
            # --- Spanish: injury ----------------------------------------
            ("me lesioné del isquio", True),
            ("vuelvo de una lesión del menisco", True),
            ("voy lesionado al gimnasio", True),
            # --- Spanish: illness / discomfort --------------------------
            ("estoy enfermo y no entrenaré", True),
            ("con esta enfermedad lo mejor es descansar", True),
            ("tengo una molestia en el hombro", True),
            ("las molestias continúan al pedalear", True),
            ("me mareé en la serie final", True),
            # --- English ------------------------------------------------
            ("I have knee pain after the run", True),
            ("my hamstring is painful today", True),
            ("my lower back hurts when I ride", True),
            ("I keep hurting my shoulder", True),
            ("I am injured and cannot run", True),
            ("the injury came back", True),
            ("I feel sick this week", True),
            ("the illness knocked me out", True),
            ("there is an ache in my ankle", True),
            ("my calves are aching", True),
            ("my throat is sore", True),
            ("the soreness will not go away", True),
            # --- accents and casing are tolerated -----------------------
            ("DOLOR en la rodilla", True),
            ("LESIÓN del menisco", True),
            ("ENFERMO total", True),
            ("Molestias en el hombro", True),
            ("PAINFUL ride today", True),
            ("HURTS to walk", True),
        ],
    )
    def test_medical_messages_are_detected(self, message: str, expected: bool) -> None:
        assert detect_medical_signal(message) is expected

    @pytest.mark.parametrize(
        "message",
        [
            # Everyday training talk with no pain/injury/illness word.
            "¿Cómo están mi CTL y mi TSB esta semana?",
            "Voy a correr con mi hermano el domingo",
            "El entrenamiento de ayer fue duro pero bien",
            "¿Cuál es mi ritmo objetivo para el maratón?",
            "Compré una zapatillas nuevas con los dólares del premio",
            "Visité el dolmen del parque durante la ruta",
            "I will travel during the off-season",
            "My new shoes are brilliant",
            "What is my readiness today?",
            "",
        ],
    )
    def test_non_medical_messages_are_not_detected(self, message: str) -> None:
        assert detect_medical_signal(message) is False

    def test_documented_false_negatives_are_today_s_behaviour(self) -> None:
        """The trade-off in the docstring, pinned: implicit signals without
        any pain/injury/illness word are deliberately NOT detected — that
        is the status quo this backstop improves on, not a regression."""
        assert detect_medical_signal("no me encuentro bien para entrenar") is False
        assert detect_medical_signal("me siento raro al correr") is False


class TestDisclosureHelpers:
    def test_reply_containing_the_disclosure_is_recognised(self) -> None:
        assert reply_contains_disclosure(
            "No soy un profesional médico, así que consulta a un médico."
        )
        # Case- and accent-insensitive on the REPLY side.
        assert reply_contains_disclosure("NO SOY UN PROFESIONAL MEDICO.")

    def test_reply_without_the_disclosure_is_not_recognised(self) -> None:
        assert not reply_contains_disclosure(
            "Reduce la carga y consulta a un médico o fisioterapeuta."
        )

    def test_missing_disclosure_is_appended_never_replaced(self) -> None:
        reply = "Reduce la carga y consulta a un médico o fisioterapeuta."
        ensured, applied = ensure_medical_disclosure(reply)
        assert applied is True
        assert ensured.startswith(reply), "the model's content is never replaced"
        assert ensured.endswith(MEDICAL_DISCLOSURE)
        assert reply_contains_disclosure(ensured)

    def test_already_compliant_reply_is_returned_unchanged(self) -> None:
        reply = (
            "No puedo decirte qué es: no soy un profesional médico. "
            "Reduce la carga y consulta a un médico o fisioterapeuta."
        )
        ensured, applied = ensure_medical_disclosure(reply)
        assert applied is False
        assert ensured == reply
        assert ensured.lower().count("no soy un profesional médico") == 1


# ---------------------------------------------------------------------------
# Layers 1 + 2: end-to-end through the real pipeline (fake model, test DB)
# ---------------------------------------------------------------------------


class TestMedicalBackstopEndToEnd:
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
            self.messages = TestMedicalBackstopEndToEnd.FakeTwilioMessages()

    async def _run_turn(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
        model: FakeModel,
        body: str,
    ) -> Any:
        twilio = self.FakeTwilioClient()
        await process_inbound_message(
            {"MessageSid": SID, "From": FROM, "To": TO, "Body": body},
            settings=self._make_settings(),
            session_factory=session_factory,
            model=model,
            twilio_client=twilio,
            session_repository=session_repository,
        )
        return twilio

    async def _outbound_rows(
        self, session_factory: async_sessionmaker[Any]
    ) -> list[MessageLogRow]:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(MessageLogRow).where(MessageLogRow.direction == "outbound")
                )
            ).scalars().all()
        return sorted(rows, key=lambda r: r.id)

    @staticmethod
    def _payload(row: MessageLogRow) -> dict[str, Any]:
        """Narrow the nullable JSON column for the assertions below."""
        assert row.payload is not None
        return row.payload

    async def test_medical_reply_omitting_the_disclosure_gets_it_appended(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """The live defect, scripted: a medically careful reply that forgot
        the explicit disclosure. What is SENT contains it — appended, with
        the model's content untouched — and the intervention is observable
        in the audit, not silent."""
        defective_reply = (
            "Con dolor en el tendón mi sugerencia es reducir la carga y "
            "saltarte el intervalado de hoy. Te recomiendo que lo valore "
            "un médico o fisioterapeuta antes de volver a correr."
        )
        assert not reply_contains_disclosure(defective_reply)
        model = FakeModel([defective_reply])

        twilio = await self._run_turn(session_factory, session_repository, model, PAIN_MESSAGE)

        # Layer 1: the reinforcement reached the model ON THIS TURN.
        delivered = str(model.invocations[0]["system_prompt"])
        assert MEDICAL_PROMPT_REINFORCEMENT in delivered

        # Layer 2: the SENT message contains the disclosure, appended.
        assert len(twilio.messages.calls) == 1
        body = twilio.messages.calls[0]["body"]
        assert body.startswith(defective_reply), "the model's content is kept"
        assert body.endswith(MEDICAL_DISCLOSURE)
        assert "no soy un profesional médico" in body.lower()
        assert body.lower().count("no soy un profesional médico") == 1

        # The intervention is observable in the audit payload.
        rows = await self._outbound_rows(session_factory)
        assert len(rows) == 1
        assert rows[0].body == body
        assert self._payload(rows[0]).get("medical_disclosure_backstop") is True

    async def test_non_medical_turn_is_left_untouched(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        plain_reply = "Tu CTL de la semana es 52.0 y tu TSB es -8.0; carga controlada."
        model = FakeModel([plain_reply])

        twilio = await self._run_turn(session_factory, session_repository, model, PLAIN_MESSAGE)

        assert twilio.messages.calls[0]["body"] == plain_reply
        # No reinforcement on a non-medical turn either.
        delivered = str(model.invocations[0]["system_prompt"])
        assert MEDICAL_PROMPT_REINFORCEMENT not in delivered
        rows = await self._outbound_rows(session_factory)
        assert "medical_disclosure_backstop" not in self._payload(rows[0])

    async def test_already_compliant_medical_reply_is_not_duplicated(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        compliant_reply = (
            "No puedo diagnosticarte: no soy un profesional médico. Reduce "
            "la carga hoy y consulta a un médico o fisioterapeuta."
        )
        model = FakeModel([compliant_reply])

        twilio = await self._run_turn(session_factory, session_repository, model, PAIN_MESSAGE)

        body = twilio.messages.calls[0]["body"]
        assert body == compliant_reply
        assert body.lower().count("no soy un profesional médico") == 1
        rows = await self._outbound_rows(session_factory)
        assert "medical_disclosure_backstop" not in self._payload(rows[0])

    async def test_long_medical_reply_splits_without_losing_referral_or_disclosure(
        self,
        session_factory: async_sessionmaker[Any],
        session_repository: Any,
    ) -> None:
        """WA-8 still holds on the medical path: the appended disclosure
        rides the split (one audit row per send, part/total) and neither
        the referral nor the disclosure is lost."""
        filler = (
            "Mientras el dolor siga ahí, prioriza el descanso relativo: "
            "caminar y nadar suave suelen tolerarse mejor que correr, y "
            "mantener el resto de hábitos (sueño, nutrición) ayuda a la "
            "carga general del cuerpo. "
        )
        referral = (
            "Reduce o detiene la carga — nada de series mientras duela — y "
            "consulta a un médico o fisioterapeuta para que lo valore."
        )
        long_defective_reply = (filler * 6) + referral
        assert len(long_defective_reply) > 1000
        assert not reply_contains_disclosure(long_defective_reply)
        model = FakeModel([long_defective_reply])

        twilio = await self._run_turn(session_factory, session_repository, model, PAIN_MESSAGE)

        bodies = [call["body"] for call in twilio.messages.calls]
        assert len(bodies) >= 2
        assert all(len(body) <= 1000 for body in bodies)
        # Nothing dropped, nothing replaced: chunks reproduce the model's
        # reply PLUS the appended disclosure, exactly.
        assert "".join(bodies) == long_defective_reply + " " + MEDICAL_DISCLOSURE
        joined = "".join(bodies).lower()
        assert "médico o fisioterapeuta" in joined
        assert "no soy un profesional médico" in joined
        # One audit row per send, part/total provenance intact (WA-8).
        rows = await self._outbound_rows(session_factory)
        assert [row.body for row in rows] == bodies
        assert [self._payload(row)["part"] for row in rows] == list(range(1, len(bodies) + 1))
        assert all(self._payload(row)["total"] == len(bodies) for row in rows)
        assert all(
            self._payload(row).get("medical_disclosure_backstop") is True for row in rows
        )
