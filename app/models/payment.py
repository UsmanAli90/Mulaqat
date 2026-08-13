"""Payments against a booking."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import Currency, PaymentProvider, PaymentStatus
from app.models.mixins import TimestampMixin


class Payment(TimestampMixin, Base):
    """One payment attempt against a booking.

    A booking can have several: a manual transfer rejected and resubmitted, or
    a checkout abandoned and retried. So this is many-to-one, and "the booking
    is paid" is a question about the presence of a SUCCEEDED row rather than a
    flag here.

    Deliberately *not* modelled: any coupling between `Payment.status` and
    `Booking.status`. Phase 4's state machine owns booking transitions, and a
    payment row that quietly flipped a booking to confirmed would be exactly
    the kind of second write path the spec forbids.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    provider: Mapped[PaymentProvider] = mapped_column(
        Enum(
            PaymentProvider,
            name="payment_provider",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    # The provider's own identifier for this payment: a checkout session id, or
    # the bank reference an invitee typed in. Nullable because a manual
    # transfer may have none until the host verifies it.
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[Currency] = mapped_column(
        Enum(
            Currency,
            name="currency",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="payment_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        server_default=PaymentStatus.PENDING.value,
        index=True,
    )

    # Whatever the provider sent us, stored verbatim. Plain JSONB rather than a
    # validated type: the entire value of this column is that it is *not*
    # interpreted, so a provider changing its payload shape never costs us the
    # ability to debug a failure after the fact.
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Manual bank transfer only: where the uploaded receipt image lives.
    receipt_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set when the host approves a manual transfer from the admin panel.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(320), nullable=True)

    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        CheckConstraint(
            "(verified_at IS NULL) = (verified_by IS NULL)",
            name="verification_fields_together",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Payment id={self.id} booking={self.booking_id} "
            f"{self.provider.value} {self.status.value}>"
        )
