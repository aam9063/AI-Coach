"""Deterministic evidence label: a failed search must not silently precede
improvised science claims.

Measured in production on 2026-10-21: a real athlete question
("¿Qué dice la ciencia sobre el entrenamiento en ayunas?") made the agent call
``search_evidence``, which returned ``status: "insufficient_data"`` — nothing
relevant in the curated store. The reply correctly OPENED with "No tengo una
fuente en el almacén que respalde…" and then went on to improvise: "Algunos
estudios sugieren que puede ayudar a mejorar la oxidación de grasas…". That is
exactly what the owner forbids: science claims with no source. Prompt rules
have already failed three times in this project (medical disclosure, evidence
tool call, citations), so this guard is code, not prose: the LABELLED variant
the owner chose — general knowledge is allowed but must be visibly labelled as
not coming from his library — is appended deterministically at the send
boundary, and the intervention is recorded in the outbound audit payload
(``evidence_label_applied``), exactly like ``disclosure_applied``.

The guard only fires when an ACTUAL ``search_evidence`` call failed with
``insufficient_data`` in the same turn: a successful search or no search at all
never gets the label.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent import tools
from app.agent.fake_model import FakeModel
from app.agent.pipeline import handle_inbound_message
from app.core.settings import Settings
from app.db.models import MessageLogRow
from tests.dbsupport import test_database_url as dedicated_test_database_url

SENDER = "whatsapp:+10000000001"
COACH = "whatsapp:+10000000000"

ES_LABEL = (
    "Ojo: lo anterior es conocimiento general, no evidencia de tu "
    "biblioteca: no tengo una fuente que lo respalde."
)
EN_LABEL = (
    "Heads-up: that is general knowledge, not evidence from your library: "
    "I have no source backing it."
)

# The exact production failure, replayed: the model opened with the honest
# no-source sentence and then improvised a study claim anyway.
ES_MARKED_REPLY = (
    "No tengo una fuente en el almacén que respalde esa afirmación.\n\n"
    "Algunos estudios sugieren que puede ayudar a mejorar la oxidación "
    "de grasas, pero no te lo doy como ciencia."
)
ES_UNMARKED_REPLY = (
    "No tengo una fuente en el almacén que respalde esa afirmación, así "
    "que no te la doy como ciencia."
)
EN_MARKED_REPLY = (
    "I have no source for this in the library. Some studies suggest "
    "fasted cardio burns more fat."
)


class _RecordingTwilio:
    """The minimum Twilio surface the pipeline touches, recording sends."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = self
        self.bodies: list[str] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.bodies.append(str(kwargs.get("body", "")))

        class _Message:
            sid = "SMoutbound"

        return _Message()


def _settings() -> Settings:
    # embedding_provider="fake" so the REAL search_evidence runs offline
    # against the dedicated test database (the same seam the evidence
    # citation e2e tests pin).
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=dedicated_test_database_url(),
        llm_provider="fake",
        embedding_provider="fake",
        openai_api_key="",
        twilio_account_sid="ACtest",
        twilio_auth_token="test-token",
        twilio_whatsapp_from=COACH,
        twilio_whatsapp_allowlist=SENDER.removeprefix("whatsapp:"),
    )


@pytest.fixture
async def tool_seams(db_engine: Any) -> Any:
    """Pin the tool seams to the dedicated test database and the fake
    embedding adapter, so the REAL search_evidence answers from the (fresh,
    therefore EMPTY) store — insufficient_data — with zero network."""
    factory: async_sessionmaker[Any] = async_sessionmaker(
        db_engine, expire_on_commit=False
    )
    tools.set_session_factory_provider(lambda: factory)
    tools.set_settings_override(_settings())
    yield factory
    tools.reset_session_factory_provider()
    tools.reset_settings_override()


async def _outbound_payloads(db_engine: Any) -> list[dict[str, Any]]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(MessageLogRow).where(MessageLogRow.direction == "outbound")
                )
            )
            .scalars()
            .all()
        )
    return [row.payload for row in rows]


async def _run_turn(
    db_engine: Any,
    *,
    message_sid: str,
    body: str,
    turns: list[str | dict[str, Any]],
) -> _RecordingTwilio:
    twilio = _RecordingTwilio()
    await asyncio.to_thread(
        handle_inbound_message,
        {"MessageSid": message_sid, "From": SENDER, "To": COACH, "Body": body},
        settings=_settings(),
        model=FakeModel(turns),
        twilio_client=twilio,
    )
    return twilio


@pytest.mark.anyio
async def test_failed_search_plus_science_claim_gets_the_es_label(
    db_engine: Any, tool_seams: Any
) -> None:
    """(a) The production failure, replayed: search_evidence REALLY runs
    against the fresh (empty) test store and returns insufficient_data, the
    scripted reply improvises a study claim, and the SENT body ends with the
    deterministic ES label — audited with ``evidence_label_applied``."""
    twilio = await _run_turn(
        db_engine,
        message_sid="SMevidLabel00000000000000001",
        body="¿qué opinas del entrenamiento en ayunas?",
        turns=[
            {
                "name": "search_evidence",
                "input": {"query": "entrenamiento en ayunas oxidación de grasas"},
            },
            ES_MARKED_REPLY,
        ],
    )

    assert twilio.bodies, "nothing was sent"
    sent = "".join(twilio.bodies)
    assert sent.endswith(ES_LABEL), f"the ES label is missing: {sent!r}"
    # APPEND, never rewrite: the model's own words are still there.
    assert "Algunos estudios sugieren" in sent
    payloads = await _outbound_payloads(db_engine)
    assert payloads, "no outbound audit rows"
    assert all(p.get("evidence_label_applied") is True for p in payloads), (
        f"evidence_label_applied missing: {payloads!r}"
    )


@pytest.mark.anyio
async def test_failed_search_without_science_claim_gets_no_label(
    db_engine: Any, tool_seams: Any
) -> None:
    """(b) Same failed search, but the reply stays inside the honest
    no-source wording: no label sentence and no audit flag."""
    twilio = await _run_turn(
        db_engine,
        message_sid="SMevidLabel00000000000000002",
        body="¿qué opinas del entrenamiento en ayunas?",
        turns=[
            {
                "name": "search_evidence",
                "input": {"query": "entrenamiento en ayunas oxidación de grasas"},
            },
            ES_UNMARKED_REPLY,
        ],
    )

    assert twilio.bodies, "nothing was sent"
    sent = "".join(twilio.bodies)
    assert ES_LABEL not in sent, f"unexpected label: {sent!r}"
    assert sent == ES_UNMARKED_REPLY, f"the reply was rewritten: {sent!r}"
    payloads = await _outbound_payloads(db_engine)
    assert payloads, "no outbound audit rows"
    assert all("evidence_label_applied" not in p for p in payloads), (
        f"unexpected evidence_label_applied flag: {payloads!r}"
    )


@pytest.mark.anyio
async def test_english_science_claim_gets_the_en_label(
    db_engine: Any, tool_seams: Any
) -> None:
    """(c) Same guard, English reply: the EN sentence is appended."""
    twilio = await _run_turn(
        db_engine,
        message_sid="SMevidLabel00000000000000003",
        body="what do you think about fasted training?",
        turns=[
            {
                "name": "search_evidence",
                "input": {"query": "fasted training fat oxidation"},
            },
            EN_MARKED_REPLY,
        ],
    )

    assert twilio.bodies, "nothing was sent"
    sent = "".join(twilio.bodies)
    assert sent.endswith(EN_LABEL), f"the EN label is missing: {sent!r}"
    assert ES_LABEL not in sent
    payloads = await _outbound_payloads(db_engine)
    assert payloads, "no outbound audit rows"
    assert all(p.get("evidence_label_applied") is True for p in payloads), (
        f"evidence_label_applied missing: {payloads!r}"
    )


@pytest.mark.anyio
async def test_science_claim_without_a_failed_search_gets_no_label(
    db_engine: Any, tool_seams: Any
) -> None:
    """(d) The guard fires ONLY on an actual failed search: no
    search_evidence call in the turn, even a science-claiming reply, gets
    neither the label nor the flag."""
    twilio = await _run_turn(
        db_engine,
        message_sid="SMevidLabel00000000000000004",
        body="¿qué opinas del entrenamiento en ayunas?",
        turns=[ES_MARKED_REPLY],
    )

    assert twilio.bodies, "nothing was sent"
    sent = "".join(twilio.bodies)
    assert ES_LABEL not in sent, f"unexpected label without a failed search: {sent!r}"
    payloads = await _outbound_payloads(db_engine)
    assert payloads, "no outbound audit rows"
    assert all("evidence_label_applied" not in p for p in payloads), (
        f"unexpected evidence_label_applied flag: {payloads!r}"
    )
