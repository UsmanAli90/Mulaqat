# Phase 2 Notes — Domain Models and Migrations

Ten tables, three migrations, 211 tests. Merged into `development` as four
feature PRs (#1–#4), each with its own migration, kept sequential so the
revision chain stayed linear.

| Branch | What landed |
|---|---|
| `feature/p2-01-core-models` | `services`, `availability_rules`, `date_overrides`, `settings` |
| `feature/p2-02-booking-models` | `bookings`, `payments`, `intake_responses`, the exclusion constraint, availability as minutes |
| `feature/p2-03-supporting-tables` | `admin_users`, `processed_webhook_events`, `notification_log`, secret encryption, autogenerate automation |
| `feature/p2-04-seed-script` | Seed script, no admin user |

**Status:** 211 tests passing, `ruff check` and `ruff format --check` clean,
`mypy app/` clean under `strict = true`. All three migrations verified
up → down → up on a fresh database, single head (`bc737c0339e0`), and
autogenerate produces an empty diff against the models.

---

## The 3–5 concepts to understand before approving

### 1. SQLAlchemy 2.0 typed declarative vs ActiveRecord

`Mapped[int]` is not decoration. It is read at class-creation time to infer
the column type and, critically, **nullability**: `Mapped[str]` is NOT NULL and
`Mapped[str | None]` is nullable. Get the annotation wrong and you get the
wrong schema, silently.

That is the reverse of ActiveRecord, where the database is the source of truth
and the model knows nothing until it reflects. Here the model *is* the source
of truth and the migration is generated output. Three consequences worth
internalising:

- There is no `Booking.find` or `booking.save`. Models carry no persistence
  methods; all querying goes through a `Session`. That separation is what lets
  the test fixtures hand every test its own transaction-scoped session.
- A model not imported into `app/models/__init__.py` is invisible to Alembic,
  which will cheerfully generate a migration that drops nothing and creates
  nothing.
- Constraints declared in `__table_args__` are part of the model, so the model
  file is where you read what must be true — not the schema dump.

### 2. Why an exclusion constraint rather than a unique index

A unique index answers "is this exact value already present?". Double-booking
is not that question — it is "does this *range* overlap any existing range?",
and two different overlapping ranges are never equal, so no unique index can
express it.

Postgres exclusion constraints generalise uniqueness from `=` to any operator.
Ours is:

```sql
EXCLUDE USING gist (blocked_range WITH &&)
  WHERE (status IN ('pending_payment', 'confirmed'))
```

Read as: no two rows may have `blocked_range` values that overlap (`&&`),
considering only rows matching the predicate. The `WHERE` clause is what makes
a cancelled or expired booking release its slot.

This is the second of the spec's three layers. The application pre-check
(layer one) and the clean 409 (layer three) are Phase 4. Layer two is the one
that has to hold when the other two lose a race, which is why it is in the
database and not in Python.

**`btree_gist` turned out to be unnecessary.** The spec called for it, but that
extension exists to mix *scalar equality* into a GIST index — `host_id WITH =,
range WITH &&` — which a multi-tenant schema needs. This one is single-tenant:
there is no scalar column in the constraint, and the status filter is a `WHERE`
predicate rather than a constraint column. `tstzrange` has GIST support in core
Postgres. Verified on a scratch database with the extension deliberately absent
before removing it.

### 3. `blocked_range`, and why it is computed by the application

The constraint guards `blocked_range` — the meeting **plus its buffers** — not
`starts_at_utc`/`ends_at_utc`. A constraint over the raw meeting times would
accept two bookings five minutes apart when the service demands a fifteen
minute gap. There is a test for exactly that case, which asserts the meetings
genuinely do not overlap before asserting the constraint fires.

It is set by the application rather than by a generated column because the
buffers live on `services`, and a Postgres generated column cannot read another
table.

**The agreed consequence: editing a service's buffers does not retroactively
change existing bookings.** A booking keeps the footprint it was created with.
This is intended — rewriting history could make already-confirmed bookings
overlap.

Because the column is NOT NULL and application-set, the constraint is only as
trustworthy as the value written there. So there is exactly one constructor,
`Booking.schedule()`, which derives both `ends_at_utc` (from the service's
duration) and `blocked_range` (from its buffers) and raises `TypeError` if a
caller passes either. Two construction paths would eventually disagree, and the
database would be enforcing a footprint that no longer describes its booking.

Bounds are `'[)'` — lower inclusive, upper exclusive — never the default. With
`'[]'` two back-to-back bookings share an endpoint, `&&` calls them
overlapping, and the second is rejected for no reason. Confirmed against
Postgres, not assumed.

### 4. Wall-clock time is not a moment

`availability_rules` and `date_overrides` store **INTEGER minutes from
midnight**, host-local, with no date and no timezone. A rule is not an instant;
it is a reading on a clock face. Storing TIMESTAMPTZ would force a date onto a
rule that has none, and storing a UTC offset would rot twice a year.

Integers rather than `TIME` for arithmetic reasons: Phase 3 does constant maths
on these values — add granularity, add and subtract buffers, subtract min
notice, compare boundaries — and `datetime.time` supports none of it, so every
operation became combine-to-datetime, add timedelta, extract time again. As
integers, slot generation is `range(start, end, granularity)` and a boundary
check is a plain `<=`.

`app/core/wall_clock.py` converts, and `app/schemas/availability.py` keeps
`"22:00"` at the API boundary so integers never leak into a response.

### 5. Autogenerate is a draft, not an answer

Every migration in this phase needed hand-editing, and two of the fixes were
for defects that would have shipped silently:

- Autogenerate rendered a column type change as **drop-column + add-column**,
  which destroys the data. It has no way to know the conversion you intend.
- Dropping a column takes any CHECK constraint that mentions it — Postgres does
  that automatically — and autogenerate emitted no replacement, because it
  compares check constraints **by name only** and the name had not changed.
  `date_overrides` would have quietly lost the rule keeping blocked and
  custom_hours rows coherent.

Both are invisible in a green test run against an empty database. The habit
that catches them is verifying a data migration **with a real row present**:
insert at the previous revision, migrate, read the value back.

---

## Decisions where I picked one option over another

**Prices nullable, with a CHECK that at least one is set.** PKR-only (manual
transfer) and USD-only (external checkout) are both real, so requiring both
would be wrong; a service priced in neither is unbookable, so the database
rejects it.

**Enum values, not names.** SQLAlchemy stores a Python enum's *member name* by
default, so `date_overrides.type` would have held `'BLOCKED'` while the CHECK
constraint compared `'blocked'` — rejecting every insert. `values_callable`
fixes it on every enum column, and tests assert the stored text.

**Native Postgres enums over VARCHAR + CHECK.** They document themselves in
`psql` and reject bad values at the database level. The cost was that adding a
member needed a hand-written `ALTER TYPE` — since resolved by automation, see
below.

**Intake questions as validated JSONB.** Per the agreed amendment, questions
live on `services` rather than in their own table. The `IntakeQuestions` column
type validates against Pydantic models on every write, so no path — seed
script, admin panel, migration data step — can store a malformed question, and
reads come back as typed models. Note the error *shape*: a bad payload surfaces
as SQLAlchemy `StatementError` wrapping `ValidationError` on `.orig`, because
it is raised during parameter binding. Phase 4 must unwrap that for a 422.

**`settings` is a database-enforced singleton** via `CHECK (id = 1)`, so "which
settings apply" can never become query-order-dependent.

**Cancellation tokens are stored and opaque**, `secrets.token_urlsafe(32)`.
itsdangerous is gone: a signed token with a short TTL is wrong for a link that
must still work for a booking three weeks out, and a stored token can be
invalidated on cancel, which a stateless one cannot.

**`ON DELETE` rules are deliberate per table.** `RESTRICT` on
`bookings.service_id`, `payments.booking_id`, `notification_log.booking_id`,
and `bookings.rescheduled_from_id`; `CASCADE` only on `intake_responses`. The
self-FK started as `SET NULL`, which is not a cascade but severs the link
silently — leaving a rescheduled booking unable to find the payment that paid
for it. `RESTRICT` refuses the delete, the only outcome that cannot lose money.

**TOTP secret encrypted, not hashed.** Forced, not chosen: verifying a code
means recomputing it from the original, so the value must be recoverable. The
threat model is written into the `AdminUser` docstring and deliberately kept
narrow — it protects a leaked dump, a leaked backup, and a read-only SQL
injection. It does **not** protect against a compromised server, because the
key lives there too.

More important than the encryption: `__repr__` excludes both secrets, with
tests covering `repr`, `str`, f-strings, containing structures, and freshly
loaded instances. Secrets rarely escape through a stolen database; they escape
through a log line.

**Single Fernet key; MultiFernet deferred to Phase 8.** With one admin row a
manual re-encrypt loop finishes in milliseconds inside one transaction, and
MultiFernet exists to avoid coordinating a rewrite too large for that. The
trigger to revisit is wanting zero-downtime rotation, not row count. Recorded
in the docstring along with the point most likely to be got wrong under
pressure: **a suspected key leak is not a rotation problem.** Rotating
re-encrypts the same plaintext seeds, which an attacker who held the key
already has. Recovery is re-enrolling every authenticator.

**Webhook bodies stored as BYTEA.** Signature verification is an HMAC over the
exact bytes on the wire, and any re-serialisation changes them — JSON key
order, whitespace, unicode escaping, a trailing newline. A parsed-and-redumped
copy would produce a different signature from the one the provider sent, which
is actively misleading when a signature mismatch is the bug being chased. The
parsed payload is stored alongside, never instead.

**`notification_log` carries `retry_count` and `last_attempted_at`.** `status`
and `sent_at` alone cannot distinguish "failed once, ARQ will retry" from
"exhausted its retries, nobody knows" — and the second is the one that needs a
human.

**No admin user in the seed.** Seeding one would mean committing a credential
to a public repository, or relaxing `password_hash` to nullable. The second
weakens the schema permanently: the database would accept a credential-less
admin forever, in a project whose whole pattern is that the database enforces
what must be true. Phase 5 creates the admin via a CLI command that prompts for
a password.

**The seed inserts what is missing and never updates what exists.** An
upserting script would look equally idempotent by row count while silently
reverting a price the host had raised.

---

## `feature/p2-05-model-tests` — skipped, deliberately

The build plan reserved a fifth branch for cross-cutting model tests that did
not belong in the earlier ones, with permission to skip it rather than pad it.
Skipping it, because every case that branch was for already landed. This is the
list I actually considered, and where each one went:

| Cross-cutting case | Where it landed |
|---|---|
| Naming convention holds across the whole schema | `tests/test_naming_convention.py` (p2-03) — audits all 61 named objects |
| Constraint predicate agrees with the Python constant | `test_slot_occupying_statuses_matches_the_constraint_predicate` (p2-02) — reads `pg_constraint` and compares against `SLOT_OCCUPYING_STATUSES` |
| Every migration reverses cleanly | Verified per branch, up → down → up on a fresh database, and again with real rows present for the data migration |
| Models and migrations agree (no drift) | Verified per branch by autogenerating a throwaway revision and asserting it is empty |
| Single Alembic head | `test_migrations_have_been_applied` derives head from the scripts, so a second head fails it |
| Enum storage is values not names | Per-enum tests in p2-01, p2-02, p2-03 |
| Timestamps are TIMESTAMPTZ and tz-aware on read | Asserted in `test_service_defaults_are_applied_by_the_database` and `test_booking_defaults` |
| Seeded data satisfies every constraint | `tests/test_seed.py` (p2-04), each service also inserted individually |
| Money keeps exact decimal scale | `test_service_price_keeps_exact_decimal_scale` (p2-01) |

Three genuinely cross-cutting things are **not** tested, and none of them
belong in Phase 2:

- **The concurrency test** — two simultaneous booking requests, one 409. It
  needs the committing fixture (which ships unused, from Phase 1) *and* an HTTP
  layer. Phase 4, where the 409 handling lives.
- **Relationship traversal** (`booking.service`, `booking.payments`).
  Relationships are not configured yet; the factories resolve foreign keys by
  id. Phase 4 adds them.
- **`updated_at` firing on update.** `onupdate` fires only on ORM-issued
  UPDATEs and nothing updates these rows yet. Worth revisiting when the admin
  panel writes to them.

Adding a branch to restate what is already covered would make the suite look
larger while testing nothing new.

---

## Flagged additions to the data model

Beyond the spec's column lists:

- **`bookings.blocked_range`** (TSTZRANGE) — buffer-inclusive footprint, as
  agreed. Without it the exclusion constraint cannot enforce buffers.
- **`bookings.rescheduled_from_id`** — self-FK, as agreed.
- **`services.intake_questions`** (JSONB) — as agreed.
- **`notification_log.retry_count` / `last_attempted_at`** — as agreed.
- **`processed_webhook_events.raw_body` / `payload` / `event_type` /
  `processed_at`** — the spec said "payload"; split so the raw bytes survive
  intact. `processed_at` distinguishes a delivery that arrived and then crashed
  mid-handling.
- **`admin_users.is_active`** — so an admin can be disabled without deleting
  the row and losing the audit trail.
- **`payments.status`, `created_at`, `updated_at`** — the spec omitted a status
  column; without one there is no way to express a failed or refunded attempt.
- **`created_at` / `updated_at` on every table except
  `processed_webhook_events`**, which is written once and never modified, where
  an `updated_at` would be misleading.

---

## Infrastructure that came out of this phase

**Autogenerate automation.** The same three hand-fixes appeared in three
consecutive migrations, which is a defect in the setup rather than bad luck.
Two are now automated in `migrations/env.py`: a `render_item` hook maps custom
column types to their storage types, and `alembic-postgresql-enum` manages the
native-enum lifecycle including `ALTER TYPE` on member changes, which Alembic
never detected at all. Verified by regenerating the entire schema against an
empty database. What cannot be automated is now a checklist in
`migrations/script.py.mako`, rendered into every new migration.

**`alembic-postgresql-enum` is pinned exactly (`==1.10.0`), not as a range.**
`env.py` imports it, so it runs during `alembic upgrade head` in production —
it is load-bearing for deploys, not a development convenience. A minor release
that changed how enum operations render would alter migration behaviour on a
deploy that changed nothing else. Upgrade deliberately, with a regenerated
migration to inspect.

**Naming convention audit.** 61 named objects, 3 documented exceptions, each
one a case the convention cannot express: two composite uniques and one partial
unique index, where interpolating `column_0_name` alone would produce a
misleading name. The audit is a test, so an undocumented hand-named object
fails, and so does a stale allowlist entry.

---

## Obligations carried into later phases

**Phase 3 (availability engine)**
- Busy-time detection must query **`blocked_range`**, not
  `starts_at_utc`/`ends_at_utc`. The buffers are the whole reason the column
  exists, and the GIST index backing the exclusion constraint is already on it
  — the correct answer and the fast one.
- Filter on `SLOT_OCCUPYING_STATUSES`, which is asserted to match the
  constraint predicate.
- `day_of_week` is 0 = Monday, matching `date.weekday()`. Postgres
  `EXTRACT(DOW)` is 0 = Sunday — do not mix them in raw SQL.
- A `blocked` date override blocks the entire date regardless of any
  `custom_hours` rows for that date. Recorded on the model; Phase 3 implements
  it.
- Availability windows never cross midnight; an overnight window is two rules,
  and minute 1440 of one day meets minute 0 of the next exactly.

**Phase 4 (booking API and state machine)**
- Nullable prices mean a service can be PKR-only or USD-only. **The booking
  flow must not offer a currency the service is not priced in**, or it will be
  discovered as a 500.
- Unwrap `StatementError.orig` to return 422 rather than 500 on a bad
  `intake_questions` payload.
- Build bookings only through `Booking.schedule()`.
- The helper that walks `rescheduled_from_id` to the originating payment lands
  here, with the state machine.
- `create_from_reschedule()` is the single documented exception to
  "only `pending_payment -> confirmed`".
- Add ORM relationships; the factories currently resolve foreign keys by id.
- The concurrency test uses the `committing_sessionmaker` fixture and
  `@pytest.mark.commits`.

**Phase 5 (payments and admin auth)**
- Admin creation is a CLI command that prompts for a password, so an admin
  never exists without credentials.
- `has_totp_enrolled` exists so callers never pull the secret into a local.
- The hourly `confirmed -> completed` cron marks bookings completed once
  `ends_at_utc` is two hours past.

**Phase 8 (deploy and harden)**
- Revisit MultiFernet if zero-downtime key rotation is wanted.
- Add secret redaction to the structured-logging pass; `password_hash` and
  `totp_secret` must never reach a log aggregator.
- `TOTP_ENCRYPTION_KEY` must be generated per environment and backed up
  separately from the database it protects. The app refuses to boot production
  with the key published in `.env.example`.
