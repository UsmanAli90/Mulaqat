"""Test database setup.

There are two isolation modes here, and picking the wrong one is how you get
a test suite that lies to you.

--------------------------------------------------------------------------
Mode 1: `session` — rollback isolation. THE DEFAULT. Use this.
--------------------------------------------------------------------------
Equivalent to Rails' `use_transactional_fixtures`. Each test runs inside an
outer transaction that is rolled back at the end, so the database is pristine
for the next test and nothing has to be deleted.

The mechanism: we open one connection, begin a transaction on it, and bind
the AsyncSession to that *connection*. Anything the test's code commits ends
up committing a nested SAVEPOINT, not the outer transaction — so `commit()`
inside application code behaves normally from the code's point of view, and
still vanishes on rollback. `join_transaction_mode="create_savepoint"` is
what arranges that.

--------------------------------------------------------------------------
Mode 2: `committing_db` — real commits, truncate afterwards. RARE.
--------------------------------------------------------------------------
Only for tests that need two *different* database connections to see each
other's writes. There is exactly one such case in this project: the
double-booking test in Phase 4, which fires concurrent requests and asserts
the Postgres exclusion constraint rejects the loser.

Rollback isolation physically cannot test that. Two connections inside one
uncommitted transaction cannot see each other's rows, so the constraint never
fires and the test passes for the wrong reason — the worst possible outcome
for the check that protects against double-booking.

Tests using this mode must be marked `@pytest.mark.commits`, which documents
why they are different and makes them greppable. They are slower and they
cannot run in parallel with each other.
"""

import asyncio
import os
from collections.abc import AsyncGenerator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app

TEST_DB_NAME = "booking_test"


def _test_database_url() -> str:
    """Derive the test database URL from the configured dev URL.

    Same host, port and credentials, different database name. Deriving rather
    than adding a second env var means there is no way for the two to drift
    out of sync.
    """
    url = str(get_settings().database_url)
    base, _, _ = url.rpartition("/")
    return f"{base}/{TEST_DB_NAME}"


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Settings pointed at the test database."""
    return get_settings().model_copy(
        update={
            "database_url": _test_database_url(),
            "environment": "test",
            "debug": False,
        }
    )


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[str]:
    """Create the test database, migrate it to head, drop it at the end.

    Runs once per test session, not per test. Migrating for every test would
    be correct but unusably slow; the per-test isolation comes from the
    transaction fixtures below instead.

    This fixture is deliberately synchronous. CREATE DATABASE cannot run
    inside a transaction block, and psycopg/asyncpg autocommit handling for
    that is fiddly — shelling out through a plain sync connection to the
    `postgres` maintenance database is simpler and runs exactly once.
    """
    url = _test_database_url()
    admin_url = url.rpartition("/")[0] + "/postgres"

    # Async engine against the maintenance DB, with autocommit so DDL like
    # CREATE DATABASE is legal.

    async def _recreate() -> None:
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                # Drop first so a crashed previous run cannot leave behind a
                # half-migrated database that silently poisons this one.
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
                await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
        finally:
            await engine.dispose()

    asyncio.run(_recreate())

    # Run the real migrations, not Base.metadata.create_all(). This means the
    # test suite exercises the same migration path production will, so a
    # migration that is wrong or missing fails here rather than on deploy.
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(alembic_cfg, "head")

    yield url

    async def _drop() -> None:
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)'))
        finally:
            await engine.dispose()

    # Skip the drop when debugging a failure and you want to inspect state.
    if os.environ.get("KEEP_TEST_DB") != "1":
        asyncio.run(_drop())


@pytest.fixture
async def engine(migrated_database: str) -> AsyncGenerator[AsyncEngine]:
    """Engine bound to the test database.

    NullPool so connections are closed as soon as they are released — pooled
    connections held across tests would keep the database busy and make the
    session-teardown DROP hang.
    """
    test_engine = create_async_engine(migrated_database, poolclass=NullPool)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def connection(engine: AsyncEngine) -> AsyncGenerator[AsyncConnection]:
    """One connection with an open outer transaction, rolled back on teardown."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield conn
        await transaction.rollback()


@pytest.fixture
async def session(connection: AsyncConnection) -> AsyncGenerator[AsyncSession]:
    """Rollback-isolated session. The default for every test.

    Bound to the already-open connection above. `join_transaction_mode=
    "create_savepoint"` makes any commit() the code under test performs open
    and release a SAVEPOINT inside our outer transaction, so application code
    can commit normally and the outer rollback still erases everything.
    """
    factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    async with factory() as test_session:
        yield test_session


@pytest.fixture
async def client(test_settings: Settings, session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """HTTP client with the app's session dependency replaced by the test session.

    `dependency_overrides` is FastAPI's seam for this: swap the provider for a
    dependency without the application code knowing. Because the app is built
    by a factory, the override applies to this instance only and cannot leak
    into another test.

    ASGITransport calls the app in-process — no socket, no running server.
    """
    app = create_app(settings=test_settings)

    async def override_get_session() -> AsyncGenerator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Committing mode. Only for @pytest.mark.commits tests.
# ---------------------------------------------------------------------------


@pytest.fixture
async def committing_sessionmaker(
    engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Session factory whose commits are real, with a TRUNCATE cleanup.

    Hands out independent sessions on independent connections, which is the
    whole point: concurrent requests must be able to see, and collide with,
    each other's committed rows.

    Cleanup truncates every table rather than rolling back, because by
    definition the data was committed. RESTART IDENTITY resets sequences so
    IDs do not drift between tests; CASCADE handles foreign keys.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory

    table_names = list(Base.metadata.tables)
    if table_names:
        quoted = ", ".join(f'"{name}"' for name in table_names)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
