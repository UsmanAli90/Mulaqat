"""ORM models.

Importing this package is what registers every model class on
`Base.metadata`. `migrations/env.py` imports it for exactly that reason — a
model that never reaches the metadata is invisible to Alembic autogenerate,
which would then cheerfully generate a migration dropping nothing and
creating nothing.
"""

from app.models.availability import AvailabilityRule, DateOverride, DateOverrideType
from app.models.service import Service
from app.models.settings import Settings

__all__ = [
    "AvailabilityRule",
    "DateOverride",
    "DateOverrideType",
    "Service",
    "Settings",
]
