"""Bookings: the core table, and the one with the real constraints on it."""

import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)

# Range lives under the postgresql dialect, not the top-level sqlalchemy
# namespace. It is the driver-agnostic value object for a range column: it
# carries the bounds string ('[)') alongside the endpoints, which is exactly
# the detail this table depends on.
from sqlalchemy.dialects.postgresql import TSTZRANGE, Range
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import BookingStatus, CancellationReason, Currency
from app.models.mixins import TimestampMixin

CANCELLATION_TOKEN_BYTES = 32


def generate_cancellation_token() -> str:
    """A long random opaque token for the emailed cancel/reschedule link.

    `token_urlsafe(32)` is 32 bytes of CSPRNG entropy rendered as 43 URL-safe
    characters. Per the agreed amendment this replaces itsdangerous entirely:
    a signed token with a short TTL is wrong for a link that must still work
    for a booking three weeks out, and a stored token can be invalidated on
    cancel, which a stateless signed token cannot.
    """
    return secrets.token_urlsafe(CANCELLATION_TOKEN_BYTES)


def compute_blocked_range(
    starts_at_utc: datetime,
    ends_at_utc: datetime,
    buffer_before_minutes: int,
    buffer_after_minutes: int,
) -> Range[datetime]:
    """Build the calendar footprint of a booking: the meeting plus its buffers.

    This is what the exclusion constraint indexes, and it is deliberately
    computed in the application rather than by a generated column — the buffers
    live on `services`, and a Postgres generated column cannot read another
    table.

    The consequence, which is intended: **editing a service's buffers does not
    retroactively change existing bookings.** A booking keeps the footprint it
    was created with. Changing that would silently rewrite history and could
    make already-confirmed bookings overlap.

    Bounds are `'[)'` — lower inclusive, upper exclusive — and never the
    default. With the default `'[]'` two back-to-back bookings would share an
    endpoint, `&&` would report them as overlapping, and the second would be
    rejected for no reason. Verified against Postgres, not assumed.
    """
    if ends_at_utc <= starts_at_utc:
        raise ValueError("ends_at_utc must be after starts_at_utc")
    if buffer_before_minutes < 0 or buffer_after_minutes < 0:
        raise ValueError("buffers cannot be negative")

    return Range(
        starts_at_utc - timedelta(minutes=buffer_before_minutes),
        ends_at_utc + timedelta(minutes=buffer_after_minutes),
        bounds="[)",
    )


class Booking(TimestampMixin, Base):
    """One booked (or half-booked) meeting.

    Three layers protect against double-booking, per the spec. This model
    carries the second: `excl_bookings_no_overlap`, a Postgres exclusion
    constraint over `blocked_range` scoped to the two slot-occupying statuses.
    The application pre-check and the 409 handling are Phase 4.
    """

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    service_id: Mapped[int] = mapped_column(
        # RESTRICT: deleting a service that has bookings must fail loudly.
        # Cascading would erase paid bookings, and SET NULL would leave a
        # booking that cannot say what was booked.
        ForeignKey("services.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[BookingStatus] = mapped_column(
        Enum(
            BookingStatus,
            name="booking_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        server_default=BookingStatus.PENDING_PAYMENT.value,
        index=True,
    )

    # The meeting itself. TIMESTAMPTZ, always UTC.
    starts_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # The meeting plus buffers — the calendar footprint the exclusion
    # constraint actually guards. Set by the application via
    # compute_blocked_range(); see that function for why it is not generated.
    blocked_range: Mapped[Range[datetime]] = mapped_column(TSTZRANGE, nullable=False)

    invitee_name: Mapped[str] = mapped_column(String(200), nullable=False)
    invitee_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    # IANA name, never an offset — this is what the confirmation email and the
    # calendar invite are rendered in.
    invitee_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    invitee_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    currency: Mapped[Currency] = mapped_column(
        Enum(
            Currency,
            name="currency",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    # The price agreed at booking time, copied from the service rather than
    # referenced. A later price change must not alter what someone already
    # booked and paid.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    google_calendar_event_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    meet_link: Mapped[str | None] = mapped_column(Text, nullable=True)

    cancellation_token: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        default=generate_cancellation_token,
    )

    notes_private: Mapped[str | None] = mapped_column(Text, nullable=True)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[CancellationReason | None] = mapped_column(
        Enum(
            CancellationReason,
            name="cancellation_reason",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=True,
    )

    # Set when this booking replaces an earlier one. The old row moves to
    # RESCHEDULED and this one is created by the state machine's single
    # documented exception, create_from_reschedule() (Phase 4). Walking this
    # chain is how a rescheduled booking finds the payment that paid for it.
    rescheduled_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    utm_source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(String(200), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        CheckConstraint("ends_at_utc > starts_at_utc", name="ends_after_starts"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        # A booking cannot be its own predecessor. Does not prevent a longer
        # cycle, which the application has no way to create.
        CheckConstraint(
            "rescheduled_from_id IS NULL OR rescheduled_from_id <> id",
            name="not_rescheduled_from_self",
        ),
        # Cancellation metadata travels together.
        CheckConstraint(
            "(cancelled_at IS NULL) = (cancellation_reason IS NULL)",
            name="cancellation_fields_together",
        ),
        # Supports the availability engine's "which bookings touch this
        # window" query in Phase 3.
        Index("ix_bookings_starts_at_utc", "starts_at_utc"),
        # NOTE: excl_bookings_no_overlap is hand-written in the migration.
        # Alembic autogenerate cannot express an ExcludeConstraint, so it lives
        # in SQL there and deliberately not here — declaring it in both places
        # would let them drift.
    )

    def __repr__(self) -> str:
        return f"<Booking id={self.id} status={self.status.value} starts={self.starts_at_utc}>"

    @property
    def is_slot_occupying(self) -> bool:
        """Whether this booking currently holds its slot against others."""
        return self.status in (BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED)


def utcnow() -> datetime:
    """Timezone-aware UTC now.

    `datetime.utcnow()` returns a *naive* datetime and is deprecated in 3.12+.
    Mixing naive and aware datetimes raises on comparison, which is exactly the
    bug you do not want in slot arithmetic.

    Database-side defaults use `func.now()` instead; this is for application
    code that needs the value before a flush.
    """
    return datetime.now(UTC)


__all__ = [
    "Booking",
    "compute_blocked_range",
    "generate_cancellation_token",
    "utcnow",
]
