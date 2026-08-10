"""Booking, payment and intake-response model tests.

The centre of gravity here is `excl_bookings_no_overlap`, the Postgres
exclusion constraint that is layer two of the three-layer double-booking
defence. Layer one (the application pre-check) and layer three (returning a
clean 409) are Phase 4; this file proves the layer that has to hold when the
other two lose a race.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Booking,
    BookingStatus,
    CancellationReason,
    Currency,
    PaymentStatus,
    compute_blocked_range,
    generate_cancellation_token,
)
from tests.factories import (
    BASE_START,
    build_booking,
    build_intake_response,
    build_payment,
    build_service,
)

EXCLUSION = "excl_bookings_no_overlap"


@asynccontextmanager
async def expect_violation(session: AsyncSession, constraint: str) -> AsyncIterator[None]:
    """As in test_models_core: SAVEPOINT, assert IntegrityError names `constraint`."""
    savepoint = await session.begin_nested()
    try:
        with pytest.raises(IntegrityError) as exc_info:
            yield
    finally:
        await savepoint.rollback()
    message = str(exc_info.value.orig)
    assert constraint in message, f"expected {constraint!r} to be violated, got: {message}"


async def _persisted_service(session: AsyncSession, **overrides: object) -> object:
    service = build_service(**overrides)
    session.add(service)
    await session.flush()
    return service


# ---------------------------------------------------------------------------
# compute_blocked_range — pure function, no database
# ---------------------------------------------------------------------------


def test_blocked_range_without_buffers_is_the_meeting_itself() -> None:
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)

    result = compute_blocked_range(start, end, 0, 0)

    assert result.lower == start
    assert result.upper == end


def test_blocked_range_extends_by_the_buffers() -> None:
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)

    result = compute_blocked_range(start, end, 15, 10)

    assert result.lower == start - timedelta(minutes=15)
    assert result.upper == end + timedelta(minutes=10)


def test_blocked_range_uses_half_open_bounds() -> None:
    """`'[)'`, never the default.

    With inclusive upper bounds two back-to-back bookings would share an
    endpoint, `&&` would call them overlapping, and the second would be
    rejected for no reason. Verified against Postgres before being relied on.
    """
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

    result = compute_blocked_range(start, start + timedelta(minutes=30), 0, 0)

    assert result.bounds == "[)"


def test_blocked_range_rejects_a_backwards_meeting() -> None:
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="must be after"):
        compute_blocked_range(start, start - timedelta(minutes=30), 0, 0)


def test_blocked_range_rejects_negative_buffers() -> None:
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="cannot be negative"):
        compute_blocked_range(start, start + timedelta(minutes=30), -5, 0)


def test_cancellation_tokens_are_unique_and_long() -> None:
    """Opaque, stored, and unguessable — itsdangerous was dropped."""
    tokens = {generate_cancellation_token() for _ in range(200)}

    assert len(tokens) == 200
    assert all(len(token) >= 43 for token in tokens)


# ---------------------------------------------------------------------------
# bookings — basics
# ---------------------------------------------------------------------------


async def test_booking_defaults(session: AsyncSession) -> None:
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()
    await session.refresh(booking)

    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert booking.cancellation_token
    assert booking.starts_at_utc.tzinfo is not None
    assert booking.blocked_range.lower == booking.starts_at_utc


async def test_booking_status_stored_as_lowercase_value(session: AsyncSession) -> None:
    """Same values_callable trap as date_override_type; the WHERE clause on the
    exclusion constraint compares these literals, so names would break it."""
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()

    stored = await session.execute(
        text("SELECT status::text FROM bookings WHERE id = :id"), {"id": booking.id}
    )

    assert stored.scalar_one() == "pending_payment"


async def test_booking_rejects_end_before_start(session: AsyncSession) -> None:
    service = await _persisted_service(session)
    async with expect_violation(session, "ck_bookings_ends_after_starts"):
        booking = build_booking(service)  # type: ignore[arg-type]
        booking.ends_at_utc = booking.starts_at_utc - timedelta(minutes=5)
        session.add(booking)
        await session.flush()


async def test_booking_cancellation_fields_travel_together(session: AsyncSession) -> None:
    service = await _persisted_service(session)
    async with expect_violation(session, "ck_bookings_cancellation_fields_together"):
        session.add(
            build_booking(
                service,  # type: ignore[arg-type]
                status=BookingStatus.CANCELLED,
                cancelled_at=datetime.now(UTC),
                cancellation_reason=None,
            )
        )
        await session.flush()


async def test_booking_accepts_a_cancellation_reason(session: AsyncSession) -> None:
    """An enum, not free text — rejection analytics come out of this column."""
    service = await _persisted_service(session)
    booking = build_booking(
        service,  # type: ignore[arg-type]
        status=BookingStatus.CANCELLED,
        cancelled_at=datetime.now(UTC),
        cancellation_reason=CancellationReason.PAYMENT_REJECTED,
    )
    session.add(booking)
    await session.flush()
    await session.refresh(booking)

    assert booking.cancellation_reason is CancellationReason.PAYMENT_REJECTED


async def test_booking_cancellation_token_is_unique(session: AsyncSession) -> None:
    service = await _persisted_service(session)
    first = build_booking(service, cancellation_token="fixed-token")  # type: ignore[arg-type]
    session.add(first)
    await session.flush()

    async with expect_violation(session, "ix_bookings_cancellation_token"):
        session.add(
            build_booking(
                service,  # type: ignore[arg-type]
                cancellation_token="fixed-token",
                status=BookingStatus.CANCELLED,
                cancelled_at=datetime.now(UTC),
                cancellation_reason=CancellationReason.INVITEE_REQUESTED,
                starts_at_utc=BASE_START + timedelta(days=5),
            )
        )
        await session.flush()


async def test_booking_cannot_be_rescheduled_from_itself(session: AsyncSession) -> None:
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()

    async with expect_violation(session, "ck_bookings_not_rescheduled_from_self"):
        await session.execute(
            text("UPDATE bookings SET rescheduled_from_id = id WHERE id = :id"),
            {"id": booking.id},
        )


async def test_rescheduled_from_id_links_the_chain(session: AsyncSession) -> None:
    """The self-FK Phase 4 walks to find the originating payment."""
    service = await _persisted_service(session)
    original = build_booking(
        service,  # type: ignore[arg-type]
        status=BookingStatus.RESCHEDULED,
    )
    session.add(original)
    await session.flush()

    replacement = build_booking(
        service,  # type: ignore[arg-type]
        starts_at_utc=BASE_START + timedelta(days=1),
        status=BookingStatus.CONFIRMED,
        rescheduled_from_id=original.id,
    )
    session.add(replacement)
    await session.flush()
    await session.refresh(replacement)

    assert replacement.rescheduled_from_id == original.id


# ---------------------------------------------------------------------------
# excl_bookings_no_overlap — the double-booking defence
# ---------------------------------------------------------------------------


async def test_overlapping_confirmed_bookings_are_rejected(session: AsyncSession) -> None:
    """The core guarantee: the host cannot be in two meetings at once."""
    service = await _persisted_service(session)
    session.add(build_booking(service, status=BookingStatus.CONFIRMED))  # type: ignore[arg-type]
    await session.flush()

    async with expect_violation(session, EXCLUSION):
        session.add(
            build_booking(
                service,  # type: ignore[arg-type]
                status=BookingStatus.CONFIRMED,
                starts_at_utc=BASE_START + timedelta(minutes=15),
            )
        )
        await session.flush()


async def test_pending_booking_also_holds_the_slot(session: AsyncSession) -> None:
    """pending_payment occupies the calendar too, or two people could pay for
    the same slot and only one could be honoured."""
    service = await _persisted_service(session)
    session.add(build_booking(service, status=BookingStatus.PENDING_PAYMENT))  # type: ignore[arg-type]
    await session.flush()

    async with expect_violation(session, EXCLUSION):
        session.add(
            build_booking(
                service,  # type: ignore[arg-type]
                status=BookingStatus.CONFIRMED,
                starts_at_utc=BASE_START + timedelta(minutes=15),
            )
        )
        await session.flush()


async def test_back_to_back_bookings_are_allowed(session: AsyncSession) -> None:
    """The '[)' bounds earning their keep.

    One booking ends exactly when the next begins. With inclusive upper bounds
    these would share an endpoint and the second would be wrongly rejected.
    """
    service = await _persisted_service(session, duration_minutes=30)
    session.add(build_booking(service, status=BookingStatus.CONFIRMED))  # type: ignore[arg-type]
    await session.flush()

    session.add(
        build_booking(
            service,  # type: ignore[arg-type]
            status=BookingStatus.CONFIRMED,
            starts_at_utc=BASE_START + timedelta(minutes=30),
        )
    )
    await session.flush()

    count = await session.execute(text("SELECT count(*) FROM bookings"))
    assert count.scalar_one() == 2


async def test_booking_may_overlap_a_cancelled_one(session: AsyncSession) -> None:
    """A cancelled booking must release its slot."""
    service = await _persisted_service(session)
    session.add(
        build_booking(
            service,  # type: ignore[arg-type]
            status=BookingStatus.CANCELLED,
            cancelled_at=datetime.now(UTC),
            cancellation_reason=CancellationReason.INVITEE_REQUESTED,
        )
    )
    await session.flush()

    session.add(build_booking(service, status=BookingStatus.CONFIRMED))  # type: ignore[arg-type]
    await session.flush()

    count = await session.execute(text("SELECT count(*) FROM bookings"))
    assert count.scalar_one() == 2


async def test_booking_may_overlap_an_expired_one(session: AsyncSession) -> None:
    """Same for expiry: the 10-minute pending timeout must free the slot,
    which is what makes the Phase 5 expiry job meaningful."""
    service = await _persisted_service(session)
    session.add(build_booking(service, status=BookingStatus.EXPIRED))  # type: ignore[arg-type]
    await session.flush()

    session.add(build_booking(service, status=BookingStatus.CONFIRMED))  # type: ignore[arg-type]
    await session.flush()

    count = await session.execute(text("SELECT count(*) FROM bookings"))
    assert count.scalar_one() == 2


async def test_booking_may_overlap_a_rescheduled_one(session: AsyncSession) -> None:
    """The old row of a reschedule must not block its own replacement.

    If RESCHEDULED occupied a slot, moving a booking to a *later* time inside
    the original's buffered window would be impossible.
    """
    service = await _persisted_service(session)
    session.add(build_booking(service, status=BookingStatus.RESCHEDULED))  # type: ignore[arg-type]
    await session.flush()

    session.add(build_booking(service, status=BookingStatus.CONFIRMED))  # type: ignore[arg-type]
    await session.flush()

    count = await session.execute(text("SELECT count(*) FROM bookings"))
    assert count.scalar_one() == 2


async def test_buffers_block_bookings_whose_meetings_do_not_overlap(
    session: AsyncSession,
) -> None:
    """**The reason blocked_range exists.**

    Two meetings that do not overlap at all on their own times, but whose
    buffered footprints do. Meeting A is 10:00-10:30 with a 15-minute after
    buffer, so it holds the calendar until 10:45. Meeting B starts at 10:35
    with a 15-minute before buffer, so it needs the calendar from 10:20.

    A constraint over `starts_at_utc`/`ends_at_utc` would happily accept this
    and leave the host with five minutes between calls instead of fifteen.
    Only a constraint over the buffered range catches it.
    """
    service = await _persisted_service(session, duration_minutes=30)

    first = build_booking(
        service,  # type: ignore[arg-type]
        status=BookingStatus.CONFIRMED,
        starts_at_utc=BASE_START,  # 10:00-10:30, blocked until 10:45
        buffer_after_minutes=15,
    )
    session.add(first)
    await session.flush()

    second_start = BASE_START + timedelta(minutes=35)  # 10:35
    # Sanity: the meetings themselves genuinely do not overlap.
    assert second_start >= first.ends_at_utc

    async with expect_violation(session, EXCLUSION):
        session.add(
            build_booking(
                service,  # type: ignore[arg-type]
                status=BookingStatus.CONFIRMED,
                starts_at_utc=second_start,  # blocked from 10:20
                buffer_before_minutes=15,
            )
        )
        await session.flush()


async def test_buffered_bookings_that_clear_each_other_are_allowed(
    session: AsyncSession,
) -> None:
    """The mirror of the test above, so it is not passing for the wrong reason.

    Same buffers, but B starts late enough that the footprints just clear:
    A blocks until 10:45, B blocks from 10:45.
    """
    service = await _persisted_service(session, duration_minutes=30)
    session.add(
        build_booking(
            service,  # type: ignore[arg-type]
            status=BookingStatus.CONFIRMED,
            buffer_after_minutes=15,
        )
    )
    await session.flush()

    session.add(
        build_booking(
            service,  # type: ignore[arg-type]
            status=BookingStatus.CONFIRMED,
            starts_at_utc=BASE_START + timedelta(minutes=60),  # blocked from 10:45
            buffer_before_minutes=15,
        )
    )
    await session.flush()

    count = await session.execute(text("SELECT count(*) FROM bookings"))
    assert count.scalar_one() == 2


async def test_editing_service_buffers_does_not_move_existing_footprints(
    session: AsyncSession,
) -> None:
    """Documents the agreed, intended consequence of computing in the app.

    `blocked_range` is set at write time from the buffers as they were then. A
    generated column could not do this — the buffers live on another table —
    and rewriting history would risk making already-confirmed bookings overlap.
    """
    service = await _persisted_service(session, buffer_after_minutes=15)
    booking = build_booking(service, buffer_after_minutes=15)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()
    original_upper = booking.blocked_range.upper

    service.buffer_after_minutes = 60  # type: ignore[attr-defined]
    await session.flush()
    await session.refresh(booking)

    assert booking.blocked_range.upper == original_upper


# ---------------------------------------------------------------------------
# payments and intake_responses
# ---------------------------------------------------------------------------


async def test_payment_defaults(session: AsyncSession) -> None:
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()

    payment = build_payment(booking)
    session.add(payment)
    await session.flush()
    await session.refresh(payment)

    assert payment.status is PaymentStatus.PENDING
    assert payment.raw_payload is None


async def test_payment_verification_fields_travel_together(session: AsyncSession) -> None:
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()

    async with expect_violation(session, "ck_payments_verification_fields_together"):
        session.add(build_payment(booking, verified_at=datetime.now(UTC)))
        await session.flush()


async def test_payment_stores_raw_payload_verbatim(session: AsyncSession) -> None:
    """Deliberately uninterpreted, so a provider changing shape never costs us
    the ability to debug a failure after the fact."""
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()

    payload = {"event": "checkout.completed", "nested": {"amount": 3000}, "extra": [1, 2]}
    payment = build_payment(booking, raw_payload=payload)
    session.add(payment)
    await session.flush()
    await session.refresh(payment)

    assert payment.raw_payload == payload


async def test_a_booking_can_have_several_payments(session: AsyncSession) -> None:
    """A rejected manual transfer followed by a successful one."""
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()

    session.add(build_payment(booking, status=PaymentStatus.FAILED))
    session.add(build_payment(booking, status=PaymentStatus.SUCCEEDED))
    await session.flush()

    count = await session.execute(
        text("SELECT count(*) FROM payments WHERE booking_id = :id"), {"id": booking.id}
    )
    assert count.scalar_one() == 2


async def test_intake_response_is_unique_per_question_per_booking(
    session: AsyncSession,
) -> None:
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()

    session.add(build_intake_response(booking, question_key="topic"))
    await session.flush()

    async with expect_violation(session, "uq_intake_responses_booking_id_question_key"):
        session.add(build_intake_response(booking, question_key="topic"))
        await session.flush()


async def test_deleting_a_booking_cascades_to_intake_responses(session: AsyncSession) -> None:
    """Answers have no meaning without their booking, and no financial record
    to preserve — unlike payments, which use RESTRICT."""
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]
    session.add(booking)
    await session.flush()
    session.add(build_intake_response(booking))
    await session.flush()

    await session.execute(text("DELETE FROM bookings WHERE id = :id"), {"id": booking.id})
    await session.flush()

    count = await session.execute(text("SELECT count(*) FROM intake_responses"))
    assert count.scalar_one() == 0


async def test_currency_and_amount_are_captured_on_the_booking(session: AsyncSession) -> None:
    """Price is copied, not referenced: a later price change must not alter
    what someone already booked and paid."""
    service = await _persisted_service(session)
    booking = build_booking(
        service,  # type: ignore[arg-type]
        currency=Currency.USD,
        amount=Decimal("30.00"),
    )
    session.add(booking)
    await session.flush()
    await session.refresh(booking)

    assert booking.currency is Currency.USD
    assert booking.amount == Decimal("30.00")


async def test_service_with_bookings_cannot_be_deleted(session: AsyncSession) -> None:
    """RESTRICT: deleting a service that has bookings must fail loudly rather
    than erase paid bookings."""
    service = await _persisted_service(session)
    session.add(build_booking(service))  # type: ignore[arg-type]
    await session.flush()

    async with expect_violation(session, "fk_bookings_service_id_services"):
        await session.execute(
            text("DELETE FROM services WHERE id = :id"),
            {"id": service.id},  # type: ignore[attr-defined]
        )


async def test_booking_repr_is_useful(session: AsyncSession) -> None:
    """Cheap, but these show up in every failing assertion message."""
    service = await _persisted_service(session)
    booking = build_booking(service)  # type: ignore[arg-type]

    assert "Booking" in repr(booking)
    assert isinstance(booking, Booking)
