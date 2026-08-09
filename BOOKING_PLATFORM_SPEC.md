# Project Spec: Personal 1:1 Booking Platform

## Read this first, Claude

You are building a single-tenant booking platform for one person (me, the repo owner). I am a software engineer with about a year of experience. My working stack is Ruby on Rails. **I am learning FastAPI through this project, and that is the primary goal.** The product is real and I will use it, but if I end up with a working app I cannot read, this project has failed.

That constraint changes how you work. Follow the ground rules below strictly.

---

## Ground rules

1. **Work in phases.** The build plan below has 8 phases. Complete one phase, then stop and wait for my review. Do not start the next phase until I say so. Do not "helpfully" get ahead.

2. **Teach as you go.** Whenever you use a FastAPI, Pydantic v2, or SQLAlchemy 2.0 pattern that has no direct Rails equivalent, add a short comment explaining what it does and why. Examples worth explaining: dependency injection with `Depends`, `async def` vs `def` route handlers, Pydantic model validation and serialization, SQLAlchemy sessions vs ActiveRecord, Alembic vs Rails migrations. Keep comments short. I want orientation, not a textbook.

3. **At the end of each phase, write a `PHASE_N_NOTES.md`** containing: what you built, the 3 to 5 concepts I should understand before approving, and any decision where you picked one option over another and why.

4. **No premature abstraction.** Do not build base classes, generic repositories, or plugin systems until there are at least two concrete cases that need them. The one exception is the payment provider interface, which is specified below because I know a second implementation is coming.

5. **Write tests as you go, not at the end.** Every phase ships with passing tests. If a phase has no meaningful tests, say so and explain why.

6. **Ask before deviating.** If something in this spec is wrong, unclear, or a bad idea, say so before you build it. Do not silently substitute your own design.

7. **Small commits with clear messages.** One logical change per commit.

---

## What this is

A booking page for one host. Visitors browse services, pick a time slot, fill in an intake form, pay, and get a confirmed booking with a Google Meet link. The host manages availability and views bookings from an admin panel.

**Explicitly not:** a marketplace, a multi-tenant SaaS, or a Topmate clone. There is exactly one host. There are no visitor accounts.

---

## Tech stack (pinned, do not substitute)

| Layer | Choice |
|---|---|
| API | FastAPI |
| Validation | Pydantic v2 |
| ORM | SQLAlchemy 2.0, declarative + typed |
| Migrations | Alembic |
| Database | PostgreSQL 16 |
| Background jobs | ARQ (Redis-backed) |
| Testing | pytest, pytest-asyncio, real Postgres test DB (not SQLite) |
| Dependency management | uv |
| Linting / formatting | ruff |
| Type checking | mypy, strict on `app/`, relaxed on tests |
| Local dev | Docker Compose (Postgres + Redis only; app runs on host) |
| Frontend | Next.js App Router, TypeScript, Tailwind, separate directory, separate deploy |

Frontend is Phase 7. Do not touch it before then.

---

## The four hard problems

Most of this app is CRUD. These four parts are where real bugs live. Treat them with extra care, extra tests, and extra explanation.

### 1. Timezones and DST

- Store every timestamp in the database as UTC, in `TIMESTAMPTZ` columns.
- Store the IANA timezone **name** (`Asia/Karachi`, `America/New_York`) for both host and invitee. Never store a UTC offset. Offsets change twice a year in DST regions.
- Host availability is defined in the host's local time ("Monday 8pm to 11pm"), then converted to UTC at slot generation time. This means the UTC time of a slot shifts when the invitee's region enters DST, which is correct behavior.
- Use Python's `zoneinfo`. No pytz.
- **Required tests:** generate slots across a US DST transition date and a Europe DST transition date, and assert the local wall-clock times stay correct on both sides of the jump.

### 2. Booking state machine

Bookings have exactly these states:

```
pending_payment -> confirmed -> completed
pending_payment -> expired
pending_payment -> cancelled
confirmed -> cancelled
confirmed -> rescheduled
confirmed -> no_show
```

Any other transition is invalid and must raise.

Implement transitions in **one place**, a service module. Route handlers call the service. Route handlers never write `booking.status = ...` directly. Add a test that asserts every invalid transition raises.

### 3. Double-booking prevention

Three layers, all required:

- **Application check:** before creating a pending booking, verify the slot is still free.
- **Database constraint:** a Postgres exclusion constraint (`btree_gist`) preventing overlapping time ranges among bookings whose status is `pending_payment` or `confirmed`. Partial index so cancelled and expired bookings do not block reuse.
- **Graceful handling:** catch the constraint violation and return a clean 409 telling the user the slot just went, not a 500.

**Required test:** fire two concurrent booking requests for the same slot, assert exactly one succeeds and the other gets a 409.

### 4. Payment webhook idempotency

- Never confirm a booking from a browser redirect. Users close tabs. Confirm only from a verified server-side webhook.
- Every incoming webhook: verify the signature first, then look up the provider's event ID in a `processed_webhook_events` table. If it exists, return 200 immediately and do nothing. Gateways retry aggressively and will send the same event several times.
- Log every webhook payload raw before processing, so failures are debuggable.
- Pending bookings expire after 10 minutes via a background job, freeing the slot.

---

## Data model

Design these tables. Add fields you think are missing, but flag additions in your phase notes.

**`services`** - name, slug, description, duration_minutes, price_pkr, price_usd, is_active, sort_order, buffer_before_minutes, buffer_after_minutes

**`availability_rules`** - day_of_week (0-6), start_time, end_time (both host-local wall clock), is_active

**`date_overrides`** - date, type (`blocked` or `custom_hours`), start_time (nullable), end_time (nullable), reason

**`bookings`** - service_id, status, starts_at_utc, ends_at_utc, invitee_name, invitee_email, invitee_timezone, invitee_phone (nullable), currency, amount, google_calendar_event_id, meet_link, cancellation_token, notes_private, created_at, confirmed_at, cancelled_at, cancellation_reason, utm_source, utm_medium, utm_campaign

**`payments`** - booking_id, provider, provider_reference, amount, currency, status, raw_payload (JSONB), receipt_url (for manual transfers), verified_at, verified_by

**`intake_responses`** - booking_id, question_key, answer_text

**`processed_webhook_events`** - provider, provider_event_id (unique together), received_at, payload

**`notification_log`** - booking_id, type, channel, sent_at, status, error

**`settings`** - single-row table: host_timezone, min_notice_hours, max_bookings_per_day, slot_granularity_minutes, cancellation_cutoff_hours

**`admin_users`** - email, password_hash, totp_secret, last_login_at

---

## Payment strategy

This is the part most likely to derail the build, so it is deliberately scoped down.

Stripe does not operate in Pakistan. Local gateways (JazzCash, Easypaisa) require registered-business onboarding that takes weeks. So:

**V1 ships with two providers only:**

1. **`ManualBankTransferProvider`** (for PKR). Show the host's Raast / IBFT details, let the invitee upload a payment screenshot, create a `pending_payment` booking with a **48 hour** expiry instead of 10 minutes, notify the host, and let the host confirm or reject from the admin panel. This is completely normal for Pakistani users and costs zero integration time.

2. **`ExternalCheckoutProvider`** (for USD). Redirect to an external checkout URL configured per service, and expose a webhook endpoint that confirms the booking. Build it generically enough that a real gateway can slot in later.

**Build the interface first:**

```python
class PaymentProvider(Protocol):
    async def create_checkout(self, booking: Booking) -> CheckoutResult: ...
    async def verify_webhook(self, headers: dict, body: bytes) -> WebhookEvent: ...
    async def refund(self, payment: Payment) -> RefundResult: ...
```

Both providers implement it. Selection is config-driven by currency. Do not write a JazzCash or Stripe integration. If you find yourself doing that, stop and ask.

---

## Build plan

### Phase 1: Foundation
- Project scaffold with uv, ruff, mypy, pytest
- Docker Compose for Postgres and Redis
- FastAPI app factory, settings via Pydantic Settings, `.env.example`
- Health check endpoint
- SQLAlchemy async engine and session dependency
- Alembic configured and one empty migration proving it runs
- One trivial test proving the test DB spins up, migrates, and tears down per test

**Explain in notes:** app factory pattern, `Depends` for session injection, why async sessions, how the test DB isolation works.

### Phase 2: Domain models and migrations
- All tables from the data model above
- Alembic migration, including the `btree_gist` extension and the exclusion constraint
- Seed script: one admin user, three services (15/30/60 min), a default weekly availability rule, default settings
- Model-level tests: constraints fire, defaults apply, the exclusion constraint actually rejects overlaps

**Explain in notes:** SQLAlchemy 2.0 typed declarative vs ActiveRecord, why the exclusion constraint over a unique index.

### Phase 3: Availability engine
This is the heart of the app. **No HTTP layer in this phase.** Pure Python functions plus tests.

- `get_available_slots(service_id, date_from, date_to, invitee_timezone) -> list[Slot]`
- Applies: weekly rules, date overrides, existing bookings (both pending and confirmed), service buffers, min notice, max bookings per day, slot granularity
- Returns slots in UTC plus a rendered local time for the invitee

**Test hard.** At minimum: empty week, fully booked day, buffer overlap at boundaries, min-notice cutoff, max-per-day cap, US DST spring-forward, EU DST fall-back, invitee in a half-hour-offset zone (`Asia/Kolkata`), and a booking that straddles midnight in the invitee's timezone.

Do not move to Phase 4 until every one of those passes.

**Explain in notes:** how you structured the generation, and where the DST handling actually lives.

### Phase 4: Booking API + state machine
- `GET /api/services`, `GET /api/services/{slug}`
- `GET /api/availability?service=&from=&to=&tz=`
- `POST /api/bookings` creates `pending_payment`, returns checkout info
- `GET /api/bookings/{token}` (signed token, no auth)
- `POST /api/bookings/{token}/cancel`
- `POST /api/bookings/{token}/reschedule`
- Booking state machine service module
- Signed tokens: itsdangerous, short TTL, scoped to one booking
- Structured error responses, 409 on slot conflict
- Rate limiting on the booking creation endpoint

**Explain in notes:** Pydantic request/response model separation, why the state machine is isolated from routes.

### Phase 5: Payments
- `PaymentProvider` protocol and both implementations
- Webhook endpoint with signature verification and the idempotency table
- Manual transfer: receipt upload (validate MIME and size, store to local disk in dev, S3-compatible in prod), host approve/reject flow
- ARQ worker plus the pending-booking expiry job
- Tests: duplicate webhook processed once, invalid signature rejected, expiry job frees the slot, approval confirms the booking

**Explain in notes:** how ARQ jobs get enqueued and executed, and why webhook-not-redirect matters.

### Phase 6: Notifications and Google Calendar
- Email via a provider with a Pakistan-friendly free tier (Resend or Brevo, your call, justify it)
- Templates: booking confirmed, reminder 24h, reminder 1h, cancelled, rescheduled, manual payment received, manual payment rejected
- All sends go through ARQ, logged to `notification_log`, retried on failure
- Google Calendar OAuth for the host, refresh token stored encrypted
- On confirm: create event with Meet link, invite the invitee, store event ID
- On cancel: delete the event
- Host's existing calendar busy times block slot generation (wire this back into Phase 3)

**Explain in notes:** OAuth refresh token handling, and how you kept secrets out of logs.

### Phase 7: Admin panel and public frontend
Next.js, separate directory.

**Public:** profile page, service list, booking flow (service, calendar, slot, intake form, payment), confirmation page, cancel/reschedule page. Mobile-first. Timezone selector visible and obvious on every slot view. Capture UTM params from the URL into the booking.

**Admin:** login with password plus TOTP, upcoming and past bookings with intake answers, pending manual payments queue with receipt images, availability editor (weekly rules plus date overrides), settings, revenue split by currency, booking source breakdown by UTM.

Keep it plain and readable. No component library beyond Tailwind and headless primitives where needed.

### Phase 8: Deploy and harden
- Dockerfile, multi-stage, non-root
- Deploy config for Railway or a Hetzner VPS with Compose (recommend one, explain why)
- Structured JSON logging with request IDs, secrets redacted
- Sentry
- Postgres backup script on cron
- Security pass: CORS locked to my domain, security headers, no stack traces in prod responses, `.env` never committed
- `README.md`: local setup, running tests, running the worker, deploying, and an architecture overview with the state machine diagram

---

## Testing standards

- Real Postgres, transaction-rollback isolation per test, no SQLite
- Factories over fixtures for model creation (factory-boy or plain builder functions)
- Freeze time with `freezegun` for anything schedule-related
- Mock all external HTTP (Google, email, payments). Never hit a real API in tests.
- Target: full coverage on the availability engine and the state machine. Everything else, cover the paths that matter, not the getters.

---

## Do not build

Multi-tenancy, creator signup, in-house video calls, a mobile app, subscriptions, group sessions, promo codes, AI call summaries, an analytics dashboard beyond the basic UTM count, or a real payment gateway integration.

If any of these seem necessary, tell me why instead of building them.

---

## Start here

Read this whole spec. Then, before writing any code, reply with:

1. Anything in here you think is wrong or will cause problems later
2. Anything ambiguous you need me to decide
3. Your plan for Phase 1 as a short checklist

Then wait for my go-ahead.
