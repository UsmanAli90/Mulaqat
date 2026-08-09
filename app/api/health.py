"""Health endpoints.

Two of them, because they answer different questions and a load balancer
needs both:

  /health     — liveness. Is the process up? Never touches the database, so a
                database outage does not cause the orchestrator to kill and
                restart otherwise-healthy app containers.
  /health/db  — readiness. Can this process actually serve traffic? Runs a
                real query, so a broken pool or an unreachable database shows
                up as 503 rather than as failures on user-facing routes.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter(tags=["health"])

# Annotated[...] is the modern way to declare a dependency. It attaches the
# injection rule to the *type*, so the alias can be reused across handlers
# instead of repeating `= Depends(get_session)` in every signature.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class HealthResponse(BaseModel):
    """Response schema.

    Declaring it as the route's response_model does three jobs at once:
    serialisation, validation of what we return, and the OpenAPI schema at
    /docs. Rails would need a serializer plus separate API documentation.
    """

    status: Literal["ok"]


class DatabaseHealthResponse(BaseModel):
    status: Literal["ok", "error"]
    database: Literal["ok", "error"]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe. Deliberately does no IO."""
    return HealthResponse(status="ok")


@router.get("/health/db", response_model=DatabaseHealthResponse)
async def health_db(session: SessionDep) -> DatabaseHealthResponse | JSONResponse:
    """Readiness probe. Issues a real round-trip to Postgres.

    This handler is `async def`, so FastAPI runs it directly on the event
    loop. That is correct here because every operation inside it is awaited.
    A plain `def` handler would instead be pushed to a thread pool — which is
    what you want for genuinely blocking work, and exactly what you do not
    want for an async driver like asyncpg.

    The rule for this codebase: `async def` plus awaited IO, or `def` plus
    blocking IO. Never blocking IO inside `async def` — that stalls the whole
    event loop, not just the one request.
    """
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        # Return the failure as 503 rather than letting it become a 500, so
        # the shape stays machine-readable for probes. The exception detail is
        # deliberately not echoed: it can contain the connection string.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=DatabaseHealthResponse(status="error", database="error").model_dump(),
        )
    return DatabaseHealthResponse(status="ok", database="ok")
