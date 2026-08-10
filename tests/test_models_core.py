"""Model-level tests for the core tables.

Two kinds of assertion here, and the distinction matters:

  * **Defaults apply** — insert a row omitting a column, flush, confirm the
    database filled it in. This proves the `server_default` is really in the
    schema, not just in the Python model.
  * **Constraints fire** — insert a deliberately invalid row and assert the
    database rejects it. These are the tests worth having: a CHECK constraint
    that was silently never created looks identical to a working one until the
    day bad data arrives.

Constraint violations are asserted with `pytest.raises(IntegrityError)` plus a
check on the constraint *name*, because asserting only the exception type would
let a test pass on the wrong violation entirely — a NOT NULL error where a
CHECK was expected, for instance.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, time
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AvailabilityRule, DateOverrideType, Service, Settings
from app.schemas.intake import IntakeQuestion, IntakeQuestionSet, IntakeQuestionType
from tests.factories import (
    build_availability_rule,
    build_date_override,
    build_service,
    build_settings,
)


@asynccontextmanager
async def expect_violation(session: AsyncSession, constraint: str) -> AsyncIterator[None]:
    """Assert the block raises an IntegrityError naming `constraint`.

    Wraps the body in a SAVEPOINT so the failed statement rolls back to a clean
    point and the session stays usable. Without it, Postgres leaves the
    transaction in an aborted state and every later query in the same test
    fails with "current transaction is aborted".

    **Add the offending object inside this block, not before it.**
    `begin_nested()` flushes pending state before emitting the SAVEPOINT, so an
    invalid object added beforehand raises during setup rather than in the
    body — which surfaces as a baffling "generator didn't yield" rather than
    the assertion you wrote.
    """
    savepoint = await session.begin_nested()
    try:
        with pytest.raises(IntegrityError) as exc_info:
            yield
    finally:
        await savepoint.rollback()
    message = str(exc_info.value.orig)
    assert constraint in message, f"expected {constraint!r} to be violated, got: {message}"


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


async def test_service_defaults_are_applied_by_the_database(session: AsyncSession) -> None:
    """Omit every defaulted column and let Postgres fill them in."""
    service = Service(
        name="Quick chat",
        slug="quick-chat",
        duration_minutes=15,
        price_usd=Decimal("20.00"),
    )
    session.add(service)
    await session.flush()
    # Re-read from the database so the assertions below reflect what Postgres
    # actually stored, not what Python assumed.
    await session.refresh(service)

    assert service.is_active is True
    assert service.sort_order == 0
    assert service.buffer_before_minutes == 0
    assert service.buffer_after_minutes == 0
    assert service.intake_questions == []
    assert service.description is None
    assert service.price_pkr is None
    assert service.created_at is not None
    # TIMESTAMPTZ round-trips as an aware datetime, so it can never be
    # misread as local time.
    assert service.created_at.tzinfo is not None


async def test_service_rejects_zero_duration(session: AsyncSession) -> None:
    async with expect_violation(session, "ck_services_duration_positive"):
        session.add(build_service(duration_minutes=0))
        await session.flush()


async def test_service_rejects_negative_buffer(session: AsyncSession) -> None:
    async with expect_violation(session, "ck_services_buffers_non_negative"):
        session.add(build_service(buffer_after_minutes=-5))
        await session.flush()


async def test_service_rejects_negative_price(session: AsyncSession) -> None:
    async with expect_violation(session, "ck_services_prices_non_negative"):
        session.add(build_service(price_pkr=Decimal("-1.00")))
        await session.flush()


async def test_service_requires_at_least_one_price(session: AsyncSession) -> None:
    """A service priced in no currency is unbookable, so the database says no."""
    async with expect_violation(session, "ck_services_at_least_one_price"):
        session.add(build_service(price_pkr=None, price_usd=None))
        await session.flush()


async def test_service_allows_a_single_currency(session: AsyncSession) -> None:
    """The mirror of the test above: PKR-only is legitimate and must work."""
    session.add(build_service(price_pkr=Decimal("3000.00"), price_usd=None))
    await session.flush()


async def test_service_slug_is_unique(session: AsyncSession) -> None:
    session.add(build_service(slug="duplicate"))
    await session.flush()

    async with expect_violation(session, "ix_services_slug"):
        session.add(build_service(slug="duplicate"))
        await session.flush()


async def test_service_price_keeps_exact_decimal_scale(session: AsyncSession) -> None:
    """Numeric, not float. 0.10 + 0.20 must be 0.30 exactly."""
    service = build_service(price_usd=Decimal("0.10"))
    session.add(service)
    await session.flush()
    await session.refresh(service)

    assert service.price_usd == Decimal("0.10")
    assert service.price_usd + Decimal("0.20") == Decimal("0.30")


# ---------------------------------------------------------------------------
# services.intake_questions — the validated JSONB column
# ---------------------------------------------------------------------------


async def test_intake_questions_round_trip_as_models(session: AsyncSession) -> None:
    """Written as Pydantic objects, read back as Pydantic objects."""
    questions = [
        IntakeQuestion(
            key="goal",
            label="What do you want to cover?",
            type=IntakeQuestionType.LONG_TEXT,
        ),
        IntakeQuestion(
            key="experience",
            label="Experience level",
            type=IntakeQuestionType.SINGLE_SELECT,
            options=["junior", "mid", "senior"],
            required=True,
        ),
    ]
    service = build_service(intake_questions=questions)
    session.add(service)
    await session.flush()
    service_id = service.id
    # Drop everything from the identity map so the select below really reads
    # from Postgres instead of handing back the object we just created.
    session.expunge_all()

    loaded = (await session.execute(select(Service).where(Service.id == service_id))).scalar_one()

    assert [q.key for q in loaded.intake_questions] == ["goal", "experience"]
    assert loaded.intake_questions[1].options == ["junior", "mid", "senior"]
    assert loaded.intake_questions[1].required is True
    # Not dicts. The column type reconstructs the models.
    assert isinstance(loaded.intake_questions[0], IntakeQuestion)


async def test_intake_questions_accept_raw_dicts(session: AsyncSession) -> None:
    """Seed scripts and admin payloads write dicts; the type validates them."""
    service = build_service(
        intake_questions=[{"key": "topic", "label": "Topic", "type": "short_text"}]
    )
    session.add(service)
    await session.flush()
    await session.refresh(service)

    assert isinstance(service.intake_questions[0], IntakeQuestion)
    assert service.intake_questions[0].required is False


async def test_intake_questions_reject_invalid_payload_on_write(session: AsyncSession) -> None:
    """The whole point of the custom type: garbage cannot reach the column.

    Note the exception *shape*. Pydantic raises ValidationError inside
    `process_bind_param`, and SQLAlchemy wraps anything thrown during parameter
    binding in `StatementError`, exposing the original on `.orig`. Phase 4's
    error handling has to unwrap that to turn a bad admin payload into a clean
    422 instead of a 500, so the test asserts on both layers.

    Because binding happens before the statement reaches Postgres, the
    transaction is not poisoned and no savepoint is needed here.
    """
    session.add(build_service(intake_questions=[{"key": "x", "label": "X", "type": "nope"}]))

    with pytest.raises(StatementError) as exc_info:
        await session.flush()

    assert isinstance(exc_info.value.orig, ValidationError)
    session.expunge_all()


async def test_intake_questions_reject_select_without_options() -> None:
    with pytest.raises(ValidationError):
        IntakeQuestion(key="level", label="Level", type=IntakeQuestionType.SINGLE_SELECT)


async def test_intake_questions_reject_options_on_text_question() -> None:
    with pytest.raises(ValidationError):
        IntakeQuestion(
            key="notes", label="Notes", type=IntakeQuestionType.SHORT_TEXT, options=["a"]
        )


async def test_intake_questions_reject_duplicate_keys() -> None:
    """Uniqueness spans the list, so it lives on the RootModel, not the item."""
    with pytest.raises(ValidationError, match="duplicate intake question keys"):
        IntakeQuestionSet.model_validate(
            [
                {"key": "topic", "label": "First", "type": "short_text"},
                {"key": "topic", "label": "Second", "type": "short_text"},
            ]
        )


async def test_intake_question_key_must_be_a_slug() -> None:
    """Keys end up in intake_responses.question_key and must stay stable."""
    with pytest.raises(ValidationError):
        IntakeQuestion(key="Not A Slug", label="X", type=IntakeQuestionType.SHORT_TEXT)


async def test_intake_question_rejects_unknown_field() -> None:
    """extra="forbid" — a typo'd field is an error, not silent data loss."""
    with pytest.raises(ValidationError):
        IntakeQuestion.model_validate(
            {"key": "topic", "label": "Topic", "type": "short_text", "requried": True}
        )


# ---------------------------------------------------------------------------
# availability_rules
# ---------------------------------------------------------------------------


async def test_availability_rule_defaults(session: AsyncSession) -> None:
    rule = AvailabilityRule(day_of_week=0, start_time=time(20, 0), end_time=time(23, 0))
    session.add(rule)
    await session.flush()
    await session.refresh(rule)

    assert rule.is_active is True


@pytest.mark.parametrize("day", [-1, 7, 99])
async def test_availability_rule_rejects_day_outside_week(session: AsyncSession, day: int) -> None:
    async with expect_violation(session, "ck_availability_rules_day_of_week_range"):
        session.add(build_availability_rule(day_of_week=day))
        await session.flush()


async def test_availability_rule_rejects_end_before_start(session: AsyncSession) -> None:
    async with expect_violation(session, "ck_availability_rules_end_after_start"):
        session.add(build_availability_rule(start_time=time(23, 0), end_time=time(20, 0)))
        await session.flush()


async def test_availability_rule_rejects_overnight_window(session: AsyncSession) -> None:
    """A window wrapping past midnight must be entered as two rules.

    Documents a deliberate modelling limitation rather than a bug: allowing
    end < start would put a wrap-around special case into every calculation in
    the Phase 3 availability engine.
    """
    async with expect_violation(session, "ck_availability_rules_end_after_start"):
        session.add(build_availability_rule(start_time=time(22, 0), end_time=time(2, 0)))
        await session.flush()


async def test_postgres_accepts_24h_time_but_python_cannot_read_it() -> None:
    """Documents *why* `ck_availability_rules_end_time_before_24h` exists.

    Postgres treats '24:00:00' as a valid TIME and `end_time > start_time`
    passes for it, so nothing at the SQL layer objects. Python's
    `datetime.time` maxes out at 23:59:59.999999, so a row containing it can
    never be read back — asyncpg raises while decoding, which takes down every
    query selecting that table, not just the offending rule.

    Asserting the Python limitation directly keeps this test honest even if a
    future Postgres or driver changes behaviour: if `time(24, 0)` ever becomes
    constructible, this fails and the guard can be revisited.
    """
    with pytest.raises(ValueError, match="hour must be in 0..23"):
        time(24, 0)


async def test_availability_rule_rejects_24h_end_time(session: AsyncSession) -> None:
    """The poison value cannot be inserted, even by raw SQL bypassing the ORM."""
    async with expect_violation(session, "ck_availability_rules_end_time_before_24h"):
        await session.execute(
            text(
                "INSERT INTO availability_rules (day_of_week, start_time, end_time) "
                "VALUES (0, '22:00:00', '24:00:00')"
            )
        )


async def test_availability_rule_accepts_one_second_before_midnight(
    session: AsyncSession,
) -> None:
    """23:59:59 is the documented value for a window ending at midnight.

    See the loud warning in the AvailabilityRule docstring: this leaves a
    one-second seam, which Phase 3 has to account for or lose the final slot.
    """
    rule = build_availability_rule(start_time=time(22, 0), end_time=time(23, 59, 59))
    session.add(rule)
    await session.flush()
    await session.refresh(rule)

    assert rule.end_time == time(23, 59, 59)


async def test_availability_rule_accepts_a_window_starting_at_midnight(
    session: AsyncSession,
) -> None:
    """The other half of a split overnight window.

    22:00-23:59:59 on Monday plus 00:00-02:00 on Tuesday is how
    "Karachi 22:00 to 02:00" is expressed — a normal working window for US
    Eastern clients, not an edge case.
    """
    rule = build_availability_rule(day_of_week=1, start_time=time(0, 0), end_time=time(2, 0))
    session.add(rule)
    await session.flush()
    await session.refresh(rule)

    assert rule.start_time == time(0, 0)


async def test_date_override_rejects_24h_end_time(session: AsyncSession) -> None:
    async with expect_violation(session, "ck_date_overrides_end_time_before_24h"):
        await session.execute(
            text(
                "INSERT INTO date_overrides (date, type, start_time, end_time) "
                "VALUES ('2026-06-01', 'custom_hours', '09:00:00', '24:00:00')"
            )
        )


async def test_availability_rule_times_have_no_timezone(session: AsyncSession) -> None:
    """These are host-local wall-clock readings, not moments in time.

    If this column were ever changed to TIME WITH TIME ZONE, DST handling in
    Phase 3 would break in a way that is very hard to see, so the storage type
    is asserted directly.
    """
    result = await session.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'availability_rules' AND column_name = 'start_time'"
        )
    )

    assert result.scalar_one() == "time without time zone"


# ---------------------------------------------------------------------------
# date_overrides
# ---------------------------------------------------------------------------


async def test_blocked_override_requires_no_hours(session: AsyncSession) -> None:
    override = build_date_override(type=DateOverrideType.BLOCKED)
    session.add(override)
    await session.flush()
    await session.refresh(override)

    assert override.type is DateOverrideType.BLOCKED
    assert override.start_time is None


async def test_blocked_override_rejects_hours(session: AsyncSession) -> None:
    async with expect_violation(session, "ck_date_overrides_hours_match_type"):
        session.add(
            build_date_override(
                type=DateOverrideType.BLOCKED,
                start_time=time(9, 0),
                end_time=time(17, 0),
            )
        )
        await session.flush()


async def test_custom_hours_override_requires_hours(session: AsyncSession) -> None:
    async with expect_violation(session, "ck_date_overrides_hours_match_type"):
        session.add(
            build_date_override(type=DateOverrideType.CUSTOM_HOURS, start_time=None, end_time=None)
        )
        await session.flush()


async def test_custom_hours_override_rejects_end_before_start(session: AsyncSession) -> None:
    async with expect_violation(session, "ck_date_overrides_hours_match_type"):
        session.add(
            build_date_override(
                type=DateOverrideType.CUSTOM_HOURS,
                start_time=time(17, 0),
                end_time=time(9, 0),
            )
        )
        await session.flush()


async def test_custom_hours_override_is_accepted(session: AsyncSession) -> None:
    override = build_date_override(
        type=DateOverrideType.CUSTOM_HOURS,
        start_time=time(9, 0),
        end_time=time(17, 0),
        reason="Conference day",
    )
    session.add(override)
    await session.flush()
    await session.refresh(override)

    assert override.type is DateOverrideType.CUSTOM_HOURS
    assert override.end_time == time(17, 0)


async def test_only_one_blocked_row_allowed_per_date(session: AsyncSession) -> None:
    """A second blocked row for the same date is meaningless, so it is rejected."""
    session.add(build_date_override(date=date(2026, 12, 25), type=DateOverrideType.BLOCKED))
    await session.flush()

    async with expect_violation(session, "uq_date_overrides_blocked_date"):
        session.add(build_date_override(date=date(2026, 12, 25), type=DateOverrideType.BLOCKED))
        await session.flush()


async def test_multiple_custom_hours_rows_allowed_per_date(session: AsyncSession) -> None:
    """The partial index must not catch custom_hours: split days are legitimate.

    09:00-12:00 plus 17:00-20:00 on one date is two rows, and a plain unique
    index on `date` would have wrongly forbidden it.
    """
    session.add(
        build_date_override(
            date=date(2026, 6, 1),
            type=DateOverrideType.CUSTOM_HOURS,
            start_time=time(9, 0),
            end_time=time(12, 0),
        )
    )
    session.add(
        build_date_override(
            date=date(2026, 6, 1),
            type=DateOverrideType.CUSTOM_HOURS,
            start_time=time(17, 0),
            end_time=time(20, 0),
        )
    )
    await session.flush()

    count = await session.execute(
        text("SELECT count(*) FROM date_overrides WHERE date = '2026-06-01'")
    )
    assert count.scalar_one() == 2


async def test_blocked_and_custom_hours_may_coexist_on_one_date(session: AsyncSession) -> None:
    """Permitted by the database, resolved by the precedence rule.

    A blocked row wins over any custom_hours rows for the same date (Phase 3
    implements this). Forbidding the combination would mean the host cannot
    block a day without first deleting custom hours they may want back.
    """
    session.add(
        build_date_override(
            date=date(2026, 6, 2),
            type=DateOverrideType.CUSTOM_HOURS,
            start_time=time(9, 0),
            end_time=time(12, 0),
        )
    )
    session.add(build_date_override(date=date(2026, 6, 2), type=DateOverrideType.BLOCKED))

    await session.flush()


async def test_date_override_type_is_stored_as_lowercase_value(session: AsyncSession) -> None:
    """Guards a real bug found while building this branch.

    SQLAlchemy stores Python enum *names* by default, so this column would have
    held 'BLOCKED' while the CHECK constraint compares 'blocked' — every insert
    rejected. `values_callable` on the column fixes it, and this test is what
    stops it regressing silently.
    """
    override = build_date_override(type=DateOverrideType.BLOCKED)
    session.add(override)
    await session.flush()

    stored = await session.execute(
        text("SELECT type::text FROM date_overrides WHERE id = :id"), {"id": override.id}
    )

    assert stored.scalar_one() == "blocked"


async def test_date_override_type_rejects_unknown_value(session: AsyncSession) -> None:
    """The native enum type refuses values outside its label set.

    Raises DBAPIError rather than IntegrityError — Postgres rejects this while
    parsing the literal, before any constraint is evaluated — so this one does
    not use `expect_violation`.
    """
    savepoint = await session.begin_nested()
    try:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("INSERT INTO date_overrides (date, type) VALUES ('2026-01-01', 'maybe')")
            )
    finally:
        await savepoint.rollback()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


async def test_settings_defaults(session: AsyncSession) -> None:
    settings = Settings(id=1)
    session.add(settings)
    await session.flush()
    await session.refresh(settings)

    assert settings.host_timezone == "Asia/Karachi"
    assert settings.min_notice_hours == 12
    assert settings.max_bookings_per_day == 5
    assert settings.slot_granularity_minutes == 15
    assert settings.cancellation_cutoff_hours == 24


async def test_settings_is_a_singleton(session: AsyncSession) -> None:
    """A second row is impossible: the CHECK pins the PK to 1."""
    session.add(build_settings())
    await session.flush()

    async with expect_violation(session, "ck_settings_singleton"):
        await session.execute(text("INSERT INTO settings (id) VALUES (2)"))


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("min_notice_hours", -1, "ck_settings_min_notice_non_negative"),
        ("max_bookings_per_day", 0, "ck_settings_max_bookings_positive"),
        ("slot_granularity_minutes", 0, "ck_settings_slot_granularity_positive"),
        ("cancellation_cutoff_hours", -1, "ck_settings_cancellation_cutoff_non_negative"),
    ],
)
async def test_settings_rejects_nonsense_values(
    session: AsyncSession, field: str, value: int, constraint: str
) -> None:
    async with expect_violation(session, constraint):
        session.add(build_settings(**{field: value}))
        await session.flush()


async def test_settings_rejects_invalid_timezone_name() -> None:
    """Validated in Python: Postgres cannot check an IANA name."""
    with pytest.raises(ValueError, match="valid IANA timezone name"):
        build_settings(host_timezone="Not/AZone")


async def test_settings_rejects_utc_offset_as_timezone() -> None:
    """The exact failure the spec forbids. An offset rots twice a year."""
    with pytest.raises(ValueError, match="valid IANA timezone name"):
        build_settings(host_timezone="+05:00")


async def test_settings_accepts_a_dst_observing_timezone() -> None:
    """The host need not be in Karachi; DST-observing zones must be allowed."""
    settings = build_settings(host_timezone="America/New_York")

    assert settings.host_timezone == "America/New_York"
