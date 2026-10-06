"""Alembic migration environment.

Wired to the environment-driven ``Settings.database_url`` (§14) through an
async SQLAlchemy engine (SQLAlchemy 2 async stack, §6). ``target_metadata``
points at the app DB models (app.db.models.Base) so autogenerate works.

The target URL resolves through :func:`resolve_database_url`, which lets a
caller pin it explicitly instead of inheriting the cached settings.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def resolve_database_url() -> str:
    """Resolve the migration target URL.

    Precedence: the explicit ``ALEMBIC_DATABASE_URL`` environment variable,
    then Alembic's own ``sqlalchemy.url`` option (which callers such as the
    migration tests set with ``cfg.set_main_option``), then
    ``Settings.database_url``.

    The explicit overrides exist because ``get_settings()`` is cached: a
    process that already read settings once (an in-process ``alembic
    upgrade``, for instance) would otherwise migrate whatever database that
    cache points at — how a helper once applied a migration to the developer
    database instead of the dedicated test one. Test infrastructure must pin
    the URL and assert it is a test database (tests/dbsupport.py).
    """
    override = os.environ.get("ALEMBIC_DATABASE_URL")
    if override:
        return override
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return get_settings().database_url

# DB models registered for autogenerate (ING-3).
from app.db.models import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in offline mode (emit SQL without a DB connection)."""
    context.configure(
        url=resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations against a synchronous connection from the async engine."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations using an async engine with run_sync (§6 async stack)."""
    engine = create_async_engine(resolve_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
