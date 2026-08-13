"""Tests for the test-database machinery itself.

These assert that the fixtures in conftest.py actually do what they claim.
Worth having: every other test in this project trusts them, so if isolation
silently broke, failures would show up as baffling cross-contamination in
unrelated suites rather than here.
"""

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.conftest import rollback_isolated_session

PROBE_TABLE = "rollback_isolation_probe"


async def test_migrations_have_been_applied(session: AsyncSession) -> None:
    """The session-scoped fixture ran `alembic upgrade head` on a fresh DB.

    Head is read from the migration scripts rather than hardcoded. A literal
    revision ID would need editing in every phase that adds a migration, and
    the day someone updated it carelessly the test would stop meaning
    anything. This version also catches a second head appearing, which is the
    failure mode the sequential-branch rule exists to prevent.
    """
    expected_head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()

    result = await session.execute(text("SELECT version_num FROM alembic_version"))
    versions = result.scalars().all()

    assert versions == [expected_head], "test database is not migrated to head"


async def test_rollback_isolation_erases_committed_writes(engine: AsyncEngine) -> None:
    """A commit inside one isolated scope must be invisible to the next one.

    This drives `rollback_isolated_session` twice within a single test rather
    than splitting the two halves across two test functions. The earlier
    version of this test was a deliberately ordered pair — one test wrote, the
    next asserted the write was gone — which meant the second test passed
    vacuously when run on its own (`pytest -k`, a re-run of one failure, or any
    future parallel plugin that reorders). A test that can pass for the wrong
    reason is worse than no test, especially this one: it is the only thing
    standing between us and silent cross-test contamination everywhere else.

    The probe is a table rather than a row because there are no models yet.
    DDL is transactional in Postgres, so a rolled-back CREATE TABLE leaves
    nothing behind, which makes the table's existence the evidence.
    """
    async with rollback_isolated_session(engine) as first:
        await first.execute(text(f"CREATE TABLE {PROBE_TABLE} (id integer PRIMARY KEY)"))
        await first.execute(text(f"INSERT INTO {PROBE_TABLE} (id) VALUES (1)"))
        # A real commit from application code. Under rollback isolation this
        # releases a SAVEPOINT rather than committing the outer transaction.
        await first.commit()

        written = await first.execute(text(f"SELECT count(*) FROM {PROBE_TABLE}"))
        assert written.scalar_one() == 1, "the write should be visible within its own scope"

    async with rollback_isolated_session(engine) as second:
        survived = await second.execute(
            text("SELECT to_regclass(:name)"), {"name": f"public.{PROBE_TABLE}"}
        )

        assert survived.scalar_one() is None, (
            "rollback isolation is broken: a committed write survived its scope"
        )


async def test_session_fixture_is_rollback_isolated(session: AsyncSession) -> None:
    """The fixture every other test depends on uses that same mechanism.

    Guards the wiring rather than the mechanism: if `session` were ever
    re-pointed at a plain committing sessionmaker, the test above would still
    pass while the whole suite quietly lost its isolation.
    """
    await session.execute(text("SELECT 1"))
    assert session.in_transaction(), "the session fixture is not inside a transaction"

    result = await session.execute(
        text("SELECT to_regclass(:name)"), {"name": f"public.{PROBE_TABLE}"}
    )
    assert result.scalar_one() is None, "probe table leaked from another test"
