"""Health endpoint tests."""

from httpx import AsyncClient


async def test_liveness_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_queries_the_database(client: AsyncClient) -> None:
    """Proves the injected test session reaches a real Postgres.

    If the dependency override were broken, this would either hit the dev
    database or fail to connect at all.
    """
    response = await client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_docs_are_available_outside_production(client: AsyncClient) -> None:
    response = await client.get("/docs")

    assert response.status_code == 200
