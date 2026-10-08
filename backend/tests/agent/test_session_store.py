"""Unit tests for the WA-8 conversation-memory seam.

Two pieces are pinned here directly, independent of the pipeline:

- :class:`app.agent.session_store.DbSessionRepository` — OUR
  ``strands.session.SessionRepository`` implementation on the project's
  database: SDK session objects (``Session`` / ``SessionAgent`` /
  ``SessionMessage``) round-trip through JSONB rows, messages keep their
  sequential identity per (session, agent), and pagination by offset
  works (the summarized-prefix skip the SDK relies on).
- :class:`app.agent.memory.RollingSummaryConversationManager` — the
  rolling-summary POLICY: last N turns verbatim, older turns folded into
  a summary generated once per threshold crossing, state round-trips
  through ``get_state``/``restore_from_session`` (the shape the SDK's
  session persistence relies on).

DB-backed tests use the dedicated test database (``tests/dbsupport.py``);
the manager tests use the fake model — zero network.
"""

from __future__ import annotations

from typing import Any

import pytest
from strands.session.session_repository import SessionRepository
from strands.types.content import Message
from strands.types.session import Session, SessionAgent, SessionMessage, SessionType

from app.agent.fake_model import FakeModel
from tests.dbsupport import test_database_url as _test_database_url

pytestmark = pytest.mark.anyio


def _test_repository() -> Any:
    """Repository on the DEDICATED test database (never the dev DB)."""
    from app.agent.session_store import DbSessionRepository

    return DbSessionRepository(_test_database_url())


# --- DbSessionRepository (DB-backed) ----------------------------------------


class TestDbSessionRepositoryRoundTrip:
    async def test_session_agent_and_messages_round_trip_through_jsonb(
        self, db_engine: Any
    ) -> None:
        repo: SessionRepository = _test_repository()

        session = Session(session_id="whatsapp:+34600000001", session_type=SessionType.AGENT)
        assert repo.create_session(session) is not None
        # Reading it back returns an equivalent Session (fresh manager
        # construction in a new process must find it).
        loaded = repo.read_session("whatsapp:+34600000001")
        assert loaded is not None
        assert loaded.session_id == "whatsapp:+34600000001"

        agent = SessionAgent.from_dict(
            {
                "agent_id": "default",
                "state": {"sender": "whatsapp:+34600000001"},
                "conversation_manager_state": {"removed_message_count": 2},
            }
        )
        repo.create_agent("whatsapp:+34600000001", agent)
        loaded_agent = repo.read_agent("whatsapp:+34600000001", "default")
        assert loaded_agent is not None
        assert loaded_agent.state == {"sender": "whatsapp:+34600000001"}
        assert loaded_agent.conversation_manager_state == {"removed_message_count": 2}

        message = Message({"role": "user", "content": [{"text": "hola"}]})
        repo.create_message(
            "whatsapp:+34600000001", "default", SessionMessage.from_message(message, 0)
        )
        repo.create_message(
            "whatsapp:+34600000001", "default", SessionMessage.from_message(message, 1)
        )
        listed = repo.list_messages("whatsapp:+34600000001", "default")
        assert [m.message_id for m in listed] == [0, 1]
        assert listed[0].to_message()["content"][0]["text"] == "hola"

        # Pagination by offset: the summarized prefix is skipped.
        tail = repo.list_messages("whatsapp:+34600000001", "default", offset=1)
        assert [m.message_id for m in tail] == [1]

        # Single message read/update (the SDK's redaction path).
        one = repo.read_message("whatsapp:+34600000001", "default", 0)
        assert one is not None
        one.redact_message = Message({"role": "user", "content": [{"text": "redacted"}]})
        repo.update_message("whatsapp:+34600000001", "default", one)
        reloaded = repo.read_message("whatsapp:+34600000001", "default", 0)
        assert reloaded is not None
        assert reloaded.to_message()["content"][0]["text"] == "redacted"

    async def test_update_agent_persists_conversation_manager_state(
        self, db_engine: Any
    ) -> None:
        repo: SessionRepository = _test_repository()
        repo.create_session(
            Session(session_id="whatsapp:+34600000002", session_type=SessionType.AGENT)
        )
        agent = SessionAgent.from_dict(
            {"agent_id": "default", "state": {}, "conversation_manager_state": {}}
        )
        repo.create_agent("whatsapp:+34600000002", agent)
        agent.conversation_manager_state = {"removed_message_count": 5}
        repo.update_agent("whatsapp:+34600000002", agent)
        loaded = repo.read_agent("whatsapp:+34600000002", "default")
        assert loaded is not None
        assert loaded.conversation_manager_state == {"removed_message_count": 5}

# --- RollingSummaryConversationManager (fake model, no DB) ------------------


class _StubAgent:
    """Just enough agent for the manager's apply_management."""

    def __init__(self, model: FakeModel, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.model = model


def _user_msg(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def _assistant_msg(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"text": text}]}


class TestRollingSummaryConversationManager:
    def _manager(self, *, window_size: int, trigger_messages: int) -> Any:
        from app.agent.memory import RollingSummaryConversationManager

        return RollingSummaryConversationManager(
            window_size=window_size, trigger_messages=trigger_messages
        )

    async def test_no_summary_below_the_threshold(self) -> None:
        manager = self._manager(window_size=4, trigger_messages=3)
        model = FakeModel([])
        agent = _StubAgent(model, [_user_msg("u1"), _assistant_msg("a1")])
        manager.apply_management(agent)
        assert manager.summary_text is None
        assert len(model.invocations) == 0
        assert len(agent.messages) == 2

    async def test_summary_folds_turns_beyond_the_window(self) -> None:
        model = FakeModel(["RESUMEN de todo lo anterior."])
        manager = self._manager(window_size=2, trigger_messages=2)
        agent = _StubAgent(
            model,
            [
                _user_msg("u1"), _assistant_msg("a1"),
                _user_msg("u2"), _assistant_msg("a2"),
                _user_msg("u3"), _assistant_msg("a3"),
            ],
        )
        manager.apply_management(agent)

        # The summary was generated exactly once, over the folded turns.
        assert len(model.invocations) == 1
        assert manager.summary_text == "RESUMEN de todo lo anterior."
        assert manager.generated_summaries[0]["usage"]["totalTokens"] == 15
        # Last window_size messages stay verbatim; the rest collapsed.
        assert len(agent.messages) == 3  # summary + 2 verbatim
        assert agent.messages[0]["content"][0]["text"] == "RESUMEN de todo lo anterior."
        assert agent.messages[1]["content"][0]["text"] == "u3"
        assert agent.messages[2]["content"][0]["text"] == "a3"
        assert manager.removed_message_count == 4

    async def test_state_round_trips_through_get_and_restore(self) -> None:
        model = FakeModel(["RESUMEN."])
        manager = self._manager(window_size=2, trigger_messages=2)
        agent = _StubAgent(
            model,
            [
                _user_msg("u1"), _assistant_msg("a1"),
                _user_msg("u2"), _assistant_msg("a2"),
                _user_msg("u3"), _assistant_msg("a3"),
            ],
        )
        manager.apply_management(agent)

        state = manager.get_state()
        assert state["__name__"] == "RollingSummaryConversationManager"
        assert state["removed_message_count"] == 4
        assert state["summary_message"]["content"][0]["text"] == "RESUMEN."

        # A NEW manager (a new process) restores the state and prepends
        # the summary message — the SDK's session-restore contract.
        restored = self._manager(window_size=2, trigger_messages=2)
        prepend = restored.restore_from_session(state)
        assert prepend is not None
        assert len(prepend) == 1
        assert prepend[0]["content"][0]["text"] == "RESUMEN."
        assert restored.removed_message_count == 4

    async def test_existing_summary_is_reused_until_the_trigger_recrosses(self) -> None:
        model = FakeModel(["RESUMEN 1.", "RESUMEN 2."])
        manager = self._manager(window_size=2, trigger_messages=4)
        messages: list[dict[str, Any]] = []
        # Grow the conversation turn by turn; only the FIRST crossing and
        # the SECOND (4 new aged-out messages later) may generate a summary.
        for i in range(1, 7):
            messages.append(_user_msg(f"u{i}"))
            messages.append(_assistant_msg(f"a{i}"))
            agent = _StubAgent(model, list(messages))
            manager.apply_management(agent)
            if manager.summary_text is not None:
                messages = list(agent.messages)
        summaries = [s["summary"] for s in manager.generated_summaries]
        assert summaries == ["RESUMEN 1.", "RESUMEN 2."]
