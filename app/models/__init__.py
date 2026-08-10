"""ORM models.

Importing this package is what registers every model class on
`Base.metadata`. `migrations/env.py` imports it for exactly that reason — a
model that never reaches the metadata is invisible to Alembic autogenerate,
which would then produce a migration dropping every table it cannot see.
"""

from app.models.availability import AvailabilityRule, DateOverride, DateOverrideType
from app.models.booking import Booking, compute_blocked_range, generate_cancellation_token
from app.models.enums import (
    SLOT_OCCUPYING_STATUSES,
    BookingStatus,
    CancellationReason,
    Currency,
    PaymentProvider,
    PaymentStatus,
)
from app.models.intake_response import IntakeResponse
from app.models.payment import Payment
from app.models.service import Service
from app.models.settings import Settings

__all__ = [
    "SLOT_OCCUPYING_STATUSES",
    "AvailabilityRule",
    "Booking",
    "BookingStatus",
    "CancellationReason",
    "Currency",
    "DateOverride",
    "DateOverrideType",
    "IntakeResponse",
    "Payment",
    "PaymentProvider",
    "PaymentStatus",
    "Service",
    "Settings",
    "compute_blocked_range",
    "generate_cancellation_token",
]
