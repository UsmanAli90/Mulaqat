"""Enums shared by more than one model.

Kept here only because `Currency` is genuinely used by both `bookings` and
`payments`. Enums used by a single table stay next to that table.
"""

from enum import StrEnum


class Currency(StrEnum):
    """The two currencies V1 supports, one per payment provider."""

    PKR = "PKR"
    USD = "USD"


class BookingStatus(StrEnum):
    """The booking lifecycle.

    The legal transitions, enforced by the Phase 4 state machine service and
    nowhere else:

        pending_payment -> confirmed | expired | cancelled
        confirmed       -> completed | cancelled | rescheduled | no_show

    Terminal: completed, expired, cancelled, rescheduled, no_show.

    Only PENDING_PAYMENT and CONFIRMED occupy the calendar. That pair is
    exactly the predicate on the `excl_bookings_no_overlap` exclusion
    constraint, which is why a cancelled or expired booking frees its slot for
    reuse.
    """

    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"
    NO_SHOW = "no_show"


# The statuses that hold a slot. Defined next to the enum so it cannot drift
# away from the exclusion constraint's WHERE clause in the migration.
SLOT_OCCUPYING_STATUSES = (BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED)


class CancellationReason(StrEnum):
    """Why a booking was cancelled.

    An enum rather than free text, per the agreed amendment: rejection
    analytics come out of this column without anyone having to grep prose, and
    a rejected manual payment is `cancelled` + `payment_rejected` rather than a
    separate booking status.
    """

    INVITEE_REQUESTED = "invitee_requested"
    HOST_CANCELLED = "host_cancelled"
    PAYMENT_REJECTED = "payment_rejected"
    PAYMENT_EXPIRED = "payment_expired"
    NO_SHOW_POLICY = "no_show_policy"


class PaymentProvider(StrEnum):
    """The two providers V1 ships, selected by currency."""

    MANUAL_BANK_TRANSFER = "manual_bank_transfer"
    EXTERNAL_CHECKOUT = "external_checkout"


class PaymentStatus(StrEnum):
    """Payment lifecycle, independent of the booking's own status."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"
