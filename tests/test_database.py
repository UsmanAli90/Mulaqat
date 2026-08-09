"""Tests for the test-database machinery itself.

These assert that the fixtures in conftest.py actually do what they claim.
Worth having: every other test in this project trusts them, so if isolation
silently broke, failures would show up as baffling cross-contamination in
unrelated suites rather than here.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PROBE_TABLE = "rollback_isolation_probe"


async def test_migrations_have_been_applied(session: AsyncSession) -> None:
    """The session-scoped fixture ran `alembic upgrade head` on a fresh DB."""
    result = await session.execute(text("SELECT version_num FROM alembic_version"))
    versions = result.scalars().all()

    assert versions == ["15cc4a0cad0a"], "test database is not migrated to head"


# The next two tests are a pair and depend on running in this order, which
# pytest guarantees within a file. The first writes; the second proves the
# write is gone. DDL is transactional in Postgres, so a rolled-back CREATE
# TABLE leaves nothing behind — which makes the table itself the evidence.


async def test_a_write_is_visible_within_its_own_test(session: AsyncSession) -> None:
    await session.execute(text(f"CREATE TABLE {PROBE_TABLE} (id integer PRIMARY KEY)"))
    await session.execute(text(f"INSERT INTO {PROBE_TABLE} (id) VALUES (1)"))
    # A real commit from application code. Under the rollback fixture this
    # releases a SAVEPOINT rather than committing the outer transaction.
    await session.commit()

    result = await session.execute(text(f"SELECT count(*) FROM {PROBE_TABLE}"))

    assert result.scalar_one() == 1


async def test_b_previous_test_was_rolled_back(session: AsyncSession) -> None:
    """The committed write from the previous test must not survive."""
    result = await session.execute(
        text("SELECT to_regclass(:name)"), {"name": f"public.{PROBE_TABLE}"}
    )

    assert result.scalar_one() is None, (
        "rollback isolation is broken: state leaked from the previous test"
    )
