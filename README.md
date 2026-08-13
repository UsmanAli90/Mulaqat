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
uv run python -m app.seed     # three services, a weekly rule, settings
uv run pytest
```

The seed is safe to run twice: it inserts what is missing and never overwrites
what is already there, so it will not revert edits you have made. It creates no
admin user — Phase 5 adds a CLI command that prompts for a password, so an
admin never exists without credentials.

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

**Phase 2 of 8 complete** — domain models and migrations: ten tables, the `btree_gist`-free exclusion constraint preventing double-booking, encrypted TOTP secrets, and an idempotent seed script. Phase 1 delivered the foundation (scaffold, Compose, typed settings, async engine, app factory, health endpoints, Alembic, test fixtures).

Phase 3 (the availability engine) is next. The full build plan lives in [BOOKING_PLATFORM_SPEC.md](BOOKING_PLATFORM_SPEC.md); see [PHASE_1_NOTES.md](PHASE_1_NOTES.md) and [PHASE_2_NOTES.md](PHASE_2_NOTES.md) for what each completed phase decided and why.

This README is intentionally brief; it gets expanded in Phase 8 with deployment, the architecture overview, and the booking state machine diagram.
