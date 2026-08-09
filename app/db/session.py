"""Async engine, session factory, and the FastAPI session dependency."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def create_engine() -> AsyncEngine:
    """Build the async engine.

    The engine owns the connection pool and is created once per process. It is
    the closest thing to ActiveRecord's connection pool, except it is an
    explicit object you pass around rather than global state.
    """
    settings = get_settings()
    return create_async_engine(
        str(settings.database_url),
        # Log every statement in local development. Do not enable in
        # production: it prints bound parameters, which will include invitee
        # emails and payment references.
        echo=settings.debug and settings.environment == "local",
        # Verify a pooled connection is still alive before handing it out.
        # Without this, a connection killed by a Postgres restart or an
        # idle-timeout proxy surfaces as a confusing error on a random request.
        pool_pre_ping=True,
    )


engine: AsyncEngine = create_engine()

# async_sessionmaker is a factory, not a session. Each request gets a fresh
# AsyncSession from it; sessions are never shared across requests because a
# session holds a single connection and its own identity map.
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    # Keep attributes loaded after commit. With the default (True), touching
    # any attribute post-commit triggers a lazy refresh — which raises in
    # async code, because implicit IO cannot be awaited. This is the single
    # most common async-SQLAlchemy footgun.
    expire_on_commit=False,
    autoflush=True,
)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session.

    Route handlers declare `session: AsyncSession = Depends(get_session)` and
    FastAPI calls this, injects the yielded value, then resumes the generator
    after the response to clean up. The `yield` is what makes it a scoped
    resource rather than a plain value — closest Rails analogue is an
    around_action that opens and closes a resource, but declared per-handler
    instead of per-controller.

    Note there is no commit here. Committing is the caller's decision: a
    service that performs several writes should commit once, and a read-only
    handler should not commit at all. `async with` guarantees the session is
    closed and its connection returned to the pool even if the handler raises.
    """
    async with SessionFactory() as session:
        yield session
