# Mulaqat

A single-tenant 1:1 booking platform: visitors pick a service and a time slot, pay, and get a confirmed booking with a meeting link.

Built with FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic and PostgreSQL 16.

## Local setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
docker-compose up -d          # Postgres 16 + Redis 7
cp .env.example .env
uv sync                       # creates .venv and installs pinned deps
uv run alembic upgrade head
uv run pytest
```

Run the API:

```bash
uv run uvicorn app.main:app --reload
```

Interactive docs at `http://127.0.0.1:8000/docs`.

### Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app/
uv run pytest
```

CI runs exactly these four on every push and pull request.

## Status

**Phase 1 of 8 complete** — foundation: project scaffold, Docker Compose for Postgres and Redis, typed settings, async SQLAlchemy engine and session dependency, FastAPI app factory, health endpoints, Alembic wiring, and the test-database fixtures.

Phase 2 (domain models and migrations) is next. The full build plan lives in [BOOKING_PLATFORM_SPEC.md](BOOKING_PLATFORM_SPEC.md), and each completed phase has a `PHASE_N_NOTES.md`.

This README is intentionally brief; it gets expanded in Phase 8 with deployment, the architecture overview, and the booking state machine diagram.
