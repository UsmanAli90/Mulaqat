# Phase 1 Notes — Foundation

## What I built

| Area | Files |
|---|---|
| Project scaffold | `pyproject.toml`, `.python-version`, `uv.lock`, `.gitignore` |
| Infrastructure | `docker-compose.yml` (Postgres 16 + Redis 7, healthchecked, ports 5433/6380) |
| Config | `app/core/config.py`, `.env.example` |
| Database | `app/db/base.py`, `app/db/session.py` |
| App | `app/main.py` (factory), `app/api/health.py` |
| Migrations | `alembic.ini`, `migrations/env.py`, one empty baseline revision |
| Tests | `tests/conftest.py`, `tests/test_health.py`, `tests/test_database.py` |

**Status:** 6 tests passing, `ruff check` clean, `ruff format --check` clean,
`mypy app/` clean under `strict = true`. Migrations verified up, down, and up
again against real Postgres. Server boots under uvicorn and both health probes
answer.

### Running it

```bash
docker-compose up -d          # Postgres + Redis
cp .env.example .env
uv sync                       # creates .venv, installs pinned deps
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run pytest
```

Note: `docker compose` (the plugin subcommand) is not installed on this machine,
only the standalone `docker-compose` binary. Both work; I used the latter.

---

## The 3–5 concepts to understand before approving

### 1. The app factory, and why there is no global `app`

`create_app()` in [app/main.py](app/main.py) builds and returns a fresh
`FastAPI` instance. The module-level `app = create_app()` at the bottom exists
only so `uvicorn app.main:app` has something to point at.

Why it matters: the test client in [tests/conftest.py](tests/conftest.py)
builds its *own* app per test and installs a dependency override on it. With a
single global app, those overrides would be process-wide mutable state, and one
test's override could leak into another. Rails gets away with a global
`Rails.application` because it does not have this injection seam; here the
factory is what makes the seam safe.

### 2. `Depends` — dependency injection as the unit of composition

```python
async def health_db(session: AsyncSession = Depends(get_session)) -> ...:
```

FastAPI reads the handler's signature, sees `Depends(get_session)`, calls that
function, and passes the result in. Because `get_session` is a *generator* that
`yield`s, FastAPI also resumes it after the response to tear the resource down —
so the session is opened per request and always closed, even if the handler
raises.

The closest Rails analogue is an `around_action` that opens and closes a
resource, but the differences are the point:

- It is declared **per handler**, in the signature, not inherited from a
  controller hierarchy. What a route needs is visible where the route is.
- It is **overridable** (`app.dependency_overrides`), which is how tests swap
  the real session for a transaction-scoped one without the route knowing.
- It **composes** — dependencies can depend on dependencies. Phase 4's admin
  auth will be `Depends(require_admin)` layered on top of `Depends(get_session)`.

I used `Annotated[AsyncSession, Depends(get_session)]` aliased as `SessionDep`,
which is the current idiom — it attaches the injection rule to the type so the
alias is reusable rather than repeating `= Depends(...)` in every signature.

### 3. `async def` vs `def` handlers — the rule for this codebase

- `async def` → FastAPI runs it **directly on the event loop**.
- plain `def` → FastAPI pushes it to a **thread pool** so it cannot block the loop.

So: **`async def` with awaited IO, or `def` with blocking IO. Never blocking IO
inside `async def`** — one blocking call there stalls every concurrent request
in the process, not just its own. This is the single biggest behavioural
difference from Rails' thread-per-request model, where a slow call only hurts
the request making it.

Everything here is `async def` because asyncpg is an async driver. The place
this will bite is Phase 6: the Google Calendar client. If I reach for a
synchronous HTTP library there, it has to be a `def` handler or run in a thread.

### 4. Async sessions, and the `expire_on_commit` footgun

`AsyncSession` is not ActiveRecord. Models carry no persistence methods —
there is no `Booking.find` or `booking.save`. You get objects from a session
and write through that session.

The trap worth knowing now: by default SQLAlchemy expires all attributes after
`commit()`, so touching any attribute afterwards triggers a lazy reload. In sync
code that is a silent extra query. In async code it **raises**, because implicit
IO cannot be awaited. I set `expire_on_commit=False` on both session factories.
The tradeoff is that objects can be stale after a commit if another transaction
changed them; for this app's write patterns that is fine, and the alternative
fails far more confusingly.

Also: `get_session` deliberately **does not commit**. Committing is the caller's
decision, so a service doing several writes commits once and a read-only handler
commits never. Phase 4's booking service is where that starts to matter.

### 5. How the test DB isolation works — and where it is deliberately bypassed

This is the part I'd most want you to read closely, because everything else
trusts it. Full explanation is in the module docstring of
[tests/conftest.py](tests/conftest.py).

**Default mode (`session` fixture) — rollback isolation.** Open one connection,
begin a transaction, bind the session to that connection. Roll back at teardown.
Same idea as Rails' `use_transactional_fixtures`.

The subtlety is `join_transaction_mode="create_savepoint"`. Without it, a
`commit()` in application code would commit our outer transaction and defeat the
whole scheme. With it, that commit opens and releases a **SAVEPOINT** nested
inside our transaction — application code sees normal commit semantics, and the
outer rollback still erases everything. `test_a_write_is_visible_within_its_own_test`
and `test_b_previous_test_was_rolled_back` are a deliberate pair proving exactly
this: A commits a table and a row, B asserts the table does not exist.

**Escape-hatch mode (`committing_sessionmaker` fixture) — real commits, then
TRUNCATE.** As we agreed under point (c). Rollback isolation *cannot* test the
double-booking constraint: two connections inside one uncommitted transaction
cannot see each other's rows, so the exclusion constraint never fires and the
test passes for the wrong reason. That is the worst possible failure mode for
the check that protects against double-booking, so Phase 4's concurrency test
gets real commits and a truncating cleanup instead.

Tests using it must be marked `@pytest.mark.commits` (registered in
`pyproject.toml` with an explanatory string). Nothing uses it yet — the fixture
ships now so Phase 4 does not have to invent it under pressure.

**The database is built by running the real migrations**, once per session, not
by `Base.metadata.create_all()`. That means a migration that is wrong or missing
fails in the test suite rather than on deploy — which matters a lot for Phase 2,
where the exclusion constraint is hand-written SQL that `create_all()` would
never produce.

---

## Decisions where I picked one option over another

**Two health endpoints, not one.** `/health` is liveness and touches nothing;
`/health/db` is readiness and runs a real `SELECT 1`, returning 503 on failure.
One combined endpoint means a database blip makes the orchestrator kill and
restart healthy app containers, which makes an outage worse. The DB failure
response deliberately does not echo the exception — SQLAlchemy errors can carry
the connection string.

**Compose runs infrastructure only; the app runs on the host.** Per the spec.
Keeps the edit-reload loop instant and means no image rebuild to change code.

**Non-default ports (5433 / 6380).** So this project cannot collide with a
Postgres or Redis you already have running locally. Costs one line in
`.env.example` and removes a whole category of confusing failure.

**The database URL is not in `alembic.ini`.** `migrations/env.py` resolves it
from `Settings`, or from the override the test suite sets. One source of truth
for credentials, and nothing secret is committed. The stock Alembic template
hardcodes a URL in the ini file; that is fine for a tutorial and wrong here.

**Test DB name is derived, not separately configured.** `booking_test` is built
from the dev URL by swapping the database name, so the two cannot drift apart.

**`extra="forbid"` on Settings.** A typo'd variable in `.env` fails at startup
instead of silently doing nothing. `HOST_TIMEZONE` is validated through
`zoneinfo` at construction, so an invalid IANA name — or a UTC offset, which the
spec forbids — cannot get into the process at all.

**Python 3.13, not the system 3.14.6.** As agreed. uv downloaded and pinned it
via `.python-version`; `uv sync` uses it automatically.

**Removed `sqlalchemy.url` interpolation hazard.** URLs get `%` escaped before
being handed to Alembic's ConfigParser, which would otherwise mangle
percent-encoded characters in generated passwords. Not hypothetical — it bites
the first time you deploy with a managed-Postgres password.

---

## Flagged for later

- `target_metadata` is `Base.metadata`, but there is a commented-out
  `import app.models` in `migrations/env.py`. **Phase 2 must uncomment it**,
  or autogenerate will see no models and cheerfully produce an empty migration.
- The baseline revision (`15cc4a0cad0a`) is intentionally empty. Its revision ID
  is asserted in `test_migrations_have_been_applied`, so that test updates when
  Phase 2 adds the real schema.
- No `README.md` yet — that is Phase 8 per the build plan. The commands above
  cover local setup in the meantime.

---

## Test coverage honesty

Six tests is thin, and that is appropriate here: Phase 1 has almost no logic to
test. What the tests actually prove is **infrastructure**, which is what this
phase delivers — the app boots, the dependency injection wiring works, the test
database really is created and migrated, and the isolation mechanism genuinely
isolates. The real test mass starts in Phase 2 (constraints) and Phase 3
(the availability engine), where the spec rightly asks for full coverage.
