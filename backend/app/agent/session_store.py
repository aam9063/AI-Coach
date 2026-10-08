"""OUR ``strands.session.SessionRepository`` on the project's database
(WA-8, brief §9.2).

**What is the SDK's and what is ours** (ODD task, "Agent runtime" table —
the spike verified the seam): conversation persistence is the **Strands
Agents SDK's** session machinery. The pipeline builds each turn's agent
with the SDK's ``RepositorySessionManager`` (one session per SENDER, so
memory is per conversation by construction); this module is only the
pluggable storage underneath it — the ``SessionRepository`` interface the
SDK defines, implemented over the project's Postgres with the exact
CRUD surface the SDK calls (``create_session``/``read_session``,
``create_agent``/``read_agent``/``update_agent``, ``create_message``/
``read_message``/``update_message``/``list_messages``).

The SDK objects (``Session``, ``SessionAgent``, ``SessionMessage``)
serialize to plain JSON dicts (``to_dict``/``from_dict`` with byte
encoding built in), so each table stores the SDK's own serialization in a
JSONB ``data`` column — the SDK defines the shape, the database only
stores it (tables in :mod:`app.db.models`, migration ``b8d2e4f6a9c1``).

**Why a dedicated background loop**: the SDK's repository interface is
SYNCHRONOUS and is called on the pipeline's event loop (agent
construction, message-added hooks, end-of-invocation management).
Asyncpg connections are bound to the event loop that created them, so
the repository cannot reuse the pipeline's loop-bound engine; instead
every repository owns ONE dedicated background event loop (daemon
thread) with its own engine, and each synchronous SDK call is bridged
onto it. Correct across loops and across processes (each Celery worker
gets its own repository, loop and engine); the extra thread is
irrelevant for a single-athlete chat.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from strands.session.session_repository import SessionRepository
from strands.types.session import Session, SessionAgent, SessionMessage

from app.db.models import (
    AgentSessionAgentRow,
    AgentSessionMessageRow,
    AgentSessionRow,
    _utcnow,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = ["DbSessionRepository"]


class DbSessionRepository(SessionRepository):
    """``SessionRepository`` backed by the project's Postgres (WA-8).

    Args:
        database_url: Async Postgres DSN. In production the pipeline
            derives it from settings; tests pass the DEDICATED test
            database URL (``tests.dbsupport``) — never the dev database.
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        # One DEDICATED background event loop (with its own engine) for all
        # of this repository's operations. asyncpg connections are loop-
        # bound and the SDK calls this interface synchronously from the
        # pipeline's loop, so operations are bridged onto a private loop
        # instead; one persistent loop keeps connections reusable.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._engine: Any = None
        self._lock = threading.Lock()

    # --- sync→async bridge --------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                threading.Thread(
                    target=self._loop.run_forever, daemon=True, name="agent-session-repo"
                ).start()
                self._engine = create_async_engine(self._database_url, poolclass=NullPool)
            return self._loop

    def _run(
        self, operation: Callable[[async_sessionmaker[AsyncSession]], Coroutine[Any, Any, T]]
    ) -> T:
        """Run one repository operation on the repository's private loop.

        The SDK calls this interface synchronously from the pipeline's
        event loop; asyncpg connections are loop-bound, so every operation
        is submitted to the dedicated background loop (see the module
        docstring for why the connections cannot live on the caller's
        loop).
        """
        loop = self._ensure_loop()
        factory = async_sessionmaker(self._engine, expire_on_commit=False)
        future: Future[T] = asyncio.run_coroutine_threadsafe(operation(factory), loop)
        return future.result(timeout=60)

    # --- Session CRUD --------------------------------------------------------

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        """Create the session row; a concurrent creation is idempotent."""

        async def op(factory: async_sessionmaker[AsyncSession]) -> Session:
            async with factory() as db:
                db.add(
                    AgentSessionRow(
                        session_id=session.session_id,
                        session_type=session.session_type.value,
                        data=session.to_dict(),
                    )
                )
                try:
                    await db.commit()
                except IntegrityError:
                    # Another worker created this sender's session first:
                    # the unique constraint makes creation idempotent.
                    await db.rollback()
                    logger.debug(
                        "session_id=<%s> | session already exists (concurrent "
                        "creation); keeping the existing row",
                        session.session_id,
                    )
            return session

        return self._run(op)

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        async def op(factory: async_sessionmaker[AsyncSession]) -> Session | None:
            async with factory() as db:
                row = (
                    await db.execute(
                        AgentSessionRow.__table__.select().where(
                            AgentSessionRow.session_id == session_id
                        )
                    )
                ).first()
                if row is None:
                    return None
                return Session.from_dict(dict(row._mapping["data"]))

        return self._run(op)

    # --- Agent CRUD -----------------------------------------------------------

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        async def op(factory: async_sessionmaker[AsyncSession]) -> None:
            async with factory() as db:
                db.add(
                    AgentSessionAgentRow(
                        session_id=session_id,
                        agent_id=session_agent.agent_id,
                        data=session_agent.to_dict(),
                    )
                )
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    logger.warning(
                        "session_id=<%s>, agent_id=<%s> | agent row already "
                        "exists (concurrent creation); keeping the existing row",
                        session_id,
                        session_agent.agent_id,
                    )

        self._run(op)

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> SessionAgent | None:
        async def op(factory: async_sessionmaker[AsyncSession]) -> SessionAgent | None:
            async with factory() as db:
                row = (
                    await db.execute(
                        AgentSessionAgentRow.__table__.select().where(
                            AgentSessionAgentRow.session_id == session_id,
                            AgentSessionAgentRow.agent_id == agent_id,
                        )
                    )
                ).first()
                if row is None:
                    return None
                return SessionAgent.from_dict(dict(row._mapping["data"]))

        return self._run(op)

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        async def op(factory: async_sessionmaker[AsyncSession]) -> None:
            async with factory() as db:
                await db.execute(
                    update(AgentSessionAgentRow)
                    .where(
                        AgentSessionAgentRow.session_id == session_id,
                        AgentSessionAgentRow.agent_id == session_agent.agent_id,
                    )
                    .values(data=session_agent.to_dict(), updated_at=_utcnow())
                )
                await db.commit()

        self._run(op)

    # --- Message CRUD -----------------------------------------------------------

    def create_message(
        self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any
    ) -> None:
        async def op(factory: async_sessionmaker[AsyncSession]) -> None:
            async with factory() as db:
                db.add(
                    AgentSessionMessageRow(
                        session_id=session_id,
                        agent_id=agent_id,
                        message_id=session_message.message_id,
                        role=str(session_message.message.get("role", "user")),
                        data=session_message.to_dict(),
                    )
                )
                await db.commit()

        self._run(op)

    def read_message(
        self, session_id: str, agent_id: str, message_id: int, **kwargs: Any
    ) -> SessionMessage | None:
        async def op(factory: async_sessionmaker[AsyncSession]) -> SessionMessage | None:
            async with factory() as db:
                row = (
                    await db.execute(
                        AgentSessionMessageRow.__table__.select().where(
                            AgentSessionMessageRow.session_id == session_id,
                            AgentSessionMessageRow.agent_id == agent_id,
                            AgentSessionMessageRow.message_id == message_id,
                        )
                    )
                ).first()
                if row is None:
                    return None
                return SessionMessage.from_dict(dict(row._mapping["data"]))

        return self._run(op)

    def update_message(
        self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any
    ) -> None:
        async def op(factory: async_sessionmaker[AsyncSession]) -> None:
            async with factory() as db:
                await db.execute(
                    update(AgentSessionMessageRow)
                    .where(
                        AgentSessionMessageRow.session_id == session_id,
                        AgentSessionMessageRow.agent_id == agent_id,
                        AgentSessionMessageRow.message_id == session_message.message_id,
                    )
                    .values(data=session_message.to_dict())
                )
                await db.commit()

        self._run(op)

    def list_messages(
        self,
        session_id: str,
        agent_id: str,
        limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[SessionMessage]:
        """List a conversation's messages in the SDK's sequential order.

        ``offset`` skips the first messages — the SDK passes its
        conversation manager's ``removed_message_count`` here, so the
        summarized prefix is skipped, never deleted.
        """

        async def op(factory: async_sessionmaker[AsyncSession]) -> list[SessionMessage]:
            query = (
                AgentSessionMessageRow.__table__.select()
                .where(
                    AgentSessionMessageRow.session_id == session_id,
                    AgentSessionMessageRow.agent_id == agent_id,
                )
                .order_by(AgentSessionMessageRow.message_id.asc())
                .offset(offset)
            )
            if limit is not None:
                query = query.limit(limit)
            async with factory() as db:
                rows = (await db.execute(query)).all()
                return [SessionMessage.from_dict(dict(row._mapping["data"])) for row in rows]

        return self._run(op)
