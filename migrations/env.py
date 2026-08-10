"""Alembic environment.

Differences from Rails migrations worth knowing:

  * Rails derives the schema from your migration history and writes
    schema.rb. Alembic instead *diffs* `Base.metadata` (your models) against
    the live database and writes the difference — `alembic revision
    --autogenerate`. The models are the source of truth, the migration is
    generated output, and you are expected to read and edit it before
    committing. Autogenerate does not reliably detect everything (renames,
    CHECK constraints, and the exclusion constraint coming in Phase 2), so
    those get hand-written.

  * Revisions form a linked list by hash (down_revision -> revision), not a
    sorted list of timestamps. That is why merges are possible and why the
    file name is cosmetic.

Commands:
    uv run alembic revision --autogenerate -m "message"
    uv run alembic upgrade head
    uv run alembic downgrade -1
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Importing the models package registers every model class on Base.metadata.
# Without this, autogenerate sees an empty metadata and produces a migration
# that drops every table in the database.
import app.models  # noqa: F401
from app.core.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# What autogenerate compares the database against.
target_metadata = Base.metadata


def get_url() -> str:
    """Resolve the database URL.

    Priority: whatever the caller already set on the config (the test suite
    points this at the test database), then the application settings. The URL
    deliberately never lives in alembic.ini, so there is exactly one place
    credentials come from and nothing secret is committed.
    """
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    return str(get_settings().database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`).

    Useful for handing a DBA a script, or reviewing exactly what a migration
    will do in production before it touches anything.
    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type changes (VARCHAR(50) -> VARCHAR(100)), which
        # autogenerate ignores by default.
        compare_type=True,
        # Detect added/removed server-side defaults.
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against an async engine.

    Alembic's migration API is synchronous, so the async connection is bridged
    with `run_sync`: it hands the underlying sync-style connection to
    `do_run_migrations` while the greenlet machinery awaits the real IO
    underneath. This is the standard way to use a sync library on top of an
    async SQLAlchemy driver.
    """
    section = config.get_section(config.config_ini_section, {})
    # Escape % so ConfigParser interpolation does not mangle URLs containing
    # percent-encoded characters (common in generated passwords).
    section["sqlalchemy.url"] = get_url().replace("%", "%%")

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        # NullPool: migrations are a short-lived one-shot process. Pooling
        # would just leave connections open at exit.
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        # A caller (the test suite) supplied an open connection; reuse it
        # rather than opening a second one to the same database.
        do_run_migrations(connectable)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
