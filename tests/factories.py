"""Model builders for tests.

Plain functions rather than factory-boy, per the spec's "factories over
fixtures, factory-boy or plain builder functions". Plain functions win here
because factory-boy's async SQLAlchemy support needs its own session plumbing,
and these models are simple enough that a keyword-argument builder is shorter
than the factory class would be.

Every builder supplies valid defaults for the NOT NULL columns and lets a test
override exactly the field it cares about — so a test about buffers does not
have to know or care what a valid price looks like. They return unsaved
objects; the caller decides when to add and flush, because roughly half these
tests are specifically about what happens *at* flush.
"""

from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from app.models import (
    AvailabilityRule,
    Booking,
    BookingStatus,
    Currency,
    DateOverride,
    DateOverrideType,
    IntakeResponse,
    Payment,
    PaymentProvider,
    PaymentStatus,
    Service,
    Settings,
)
from app.models.settings import SETTINGS_ID

# Module-level counter so slugs and tokens are unique without the caller
# thinking about it. Tests are transaction-isolated, but a single test creating
# three services would otherwise trip the unique index on slug.
_sequence = 0


def _next() -> int:
    global _sequence
    _sequence += 1
    return _sequence


def build_service(**overrides: Any) -> Service:
    """A valid, active, 30-minute service priced in both currencies."""
    n = _next()
    defaults: dict[str, Any] = {
        "name": f"Consultation {n}",
        "slug": f"consultation-{n}",
        "description": "A conversation.",
        "duration_minutes": 30,
        "price_pkr": Decimal("5000.00"),
        "price_usd": Decimal("30.00"),
        "is_active": True,
        "sort_order": 0,
        "buffer_before_minutes": 0,
        "buffer_after_minutes": 0,
        "intake_questions": [],
    }
    return Service(**(defaults | overrides))


def build_availability_rule(**overrides: Any) -> AvailabilityRule:
    """Monday 20:00-23:00 host-local, matching the spec's example."""
    defaults: dict[str, Any] = {
        "day_of_week": 0,
        "start_minute": 20 * 60,
        "end_minute": 23 * 60,
        "is_active": True,
    }
    return AvailabilityRule(**(defaults | overrides))


def build_date_override(**overrides: Any) -> DateOverride:
    """A blocked date. Pass type=CUSTOM_HOURS with minutes for the other case."""
    defaults: dict[str, Any] = {
        "date": date_type(2026, 12, 25),
        "type": DateOverrideType.BLOCKED,
        "start_minute": None,
        "end_minute": None,
        "reason": "Holiday",
    }
    return DateOverride(**(defaults | overrides))


def build_settings(**overrides: Any) -> Settings:
    """The singleton settings row, with the schema's own defaults spelled out."""
    defaults: dict[str, Any] = {
        "id": SETTINGS_ID,
        "host_timezone": "Asia/Karachi",
        "min_notice_hours": 12,
        "max_bookings_per_day": 5,
        "slot_granularity_minutes": 15,
        "cancellation_cutoff_hours": 24,
    }
    return Settings(**(defaults | overrides))


# A fixed future instant, so booking tests never depend on the wall clock.
BASE_START = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def build_booking(
    service: Service,
    *,
    starts_at_utc: datetime | None = None,
    **overrides: Any,
) -> Booking:
    """A pending booking, built through the sanctioned constructor.

    Goes through `Booking.schedule()` rather than `Booking(...)` on purpose:
    if the tests built bookings by a different route than the application
    does, they would stop being evidence about the application. Buffers and
    duration therefore come off the `service` — set them there when a test
    needs a particular footprint.
    """
    n = _next()
    defaults: dict[str, Any] = {
        "status": BookingStatus.PENDING_PAYMENT,
        "invitee_name": f"Invitee {n}",
        "invitee_email": f"invitee{n}@example.com",
        "invitee_timezone": "America/New_York",
        "currency": Currency.PKR,
        "amount": Decimal("5000.00"),
    }
    return Booking.schedule(
        service=service,
        starts_at_utc=starts_at_utc or BASE_START,
        **(defaults | overrides),
    )


def build_payment(booking: Booking, **overrides: Any) -> Payment:
    """A pending manual bank transfer against the given booking."""
    defaults: dict[str, Any] = {
        "booking_id": booking.id,
        "provider": PaymentProvider.MANUAL_BANK_TRANSFER,
        "amount": booking.amount,
        "currency": booking.currency,
        "status": PaymentStatus.PENDING,
    }
    return Payment(**(defaults | overrides))


def build_intake_response(booking: Booking, **overrides: Any) -> IntakeResponse:
    defaults: dict[str, Any] = {
        "booking_id": booking.id,
        "question_key": "topic",
        "answer_text": "Career advice",
    }
    return IntakeResponse(**(defaults | overrides))
