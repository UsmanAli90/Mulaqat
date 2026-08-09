"""When the host is available.

Two tables, one recurring and one exceptional:

  availability_rules — the normal week ("Monday 20:00 to 23:00")
  date_overrides     — deviations for a specific date

Both store **host-local wall clock** times, deliberately without timezone.
The spec is explicit that availability is authored in the host's local time
and converted to UTC at slot-generation time (Phase 3), which is what makes
the schedule survive a DST transition: "Monday 8pm" stays 8pm on both sides
of the jump, and it is the corresponding UTC instant that moves.

Storing these as TIMESTAMPTZ would force a date onto a rule that has none,
and storing a UTC offset would rot twice a year. A bare TIME is the honest
representation: it is not a moment, it is a wall-clock reading.
"""

from datetime import date as date_type
from datetime import time as time_type
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, Date, Enum, Index, Integer, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class DateOverrideType(StrEnum):
    """Whether an override removes availability or replaces it."""

    BLOCKED = "blocked"
    CUSTOM_HOURS = "custom_hours"


class AvailabilityRule(TimestampMixin, Base):
    """One recurring weekly window of availability."""

    __tablename__ = "availability_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 0 = Monday .. 6 = Sunday, matching Python's date.weekday() so Phase 3
    # never needs a conversion. Note this differs from Postgres EXTRACT(DOW),
    # where 0 = Sunday — worth remembering before writing raw SQL against it.
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)

    start_time: Mapped[time_type] = mapped_column(Time(timezone=False), nullable=False)
    end_time: Mapped[time_type] = mapped_column(Time(timezone=False), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="day_of_week_range"),
        # This forbids a window that wraps past midnight (22:00 to 02:00).
        # Such availability must be entered as two rules, one per day. The
        # alternative — allowing end < start and treating it as a wrap — makes
        # every downstream calculation in Phase 3 carry a special case, and
        # this host does not work past midnight.
        CheckConstraint("end_time > start_time", name="end_after_start"),
        Index("ix_availability_rules_day_of_week", "day_of_week"),
    )

    def __repr__(self) -> str:
        return (
            f"<AvailabilityRule id={self.id} dow={self.day_of_week} "
            f"{self.start_time}-{self.end_time}>"
        )


class DateOverride(TimestampMixin, Base):
    """A deviation from the weekly rules for one specific date."""

    __tablename__ = "date_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)

    # A native Postgres enum type rather than a VARCHAR + CHECK. It documents
    # itself in psql and rejects bad values at the database level. The cost is
    # that adding a value later needs a hand-written ALTER TYPE in a migration,
    # since Alembic autogenerate does not detect enum value changes — an
    # acceptable trade for a set the spec fixes at two.
    type: Mapped[DateOverrideType] = mapped_column(
        Enum(
            DateOverrideType,
            name="date_override_type",
            native_enum=True,
            # Store the member *values* ("blocked"), not the member *names*
            # ("BLOCKED"), which is SQLAlchemy's default for a Python enum.
            # Without this the database would hold uppercase names while the
            # CHECK below compares lowercase values, so every insert would be
            # rejected — and `psql` output would not match the JSON the API
            # emits either.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    # Populated only for CUSTOM_HOURS; the CHECK below enforces that.
    start_time: Mapped[time_type | None] = mapped_column(Time(timezone=False), nullable=True)
    end_time: Mapped[time_type | None] = mapped_column(Time(timezone=False), nullable=True)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # One constraint expressing "hours are required for custom_hours and
        # forbidden for blocked", rather than three overlapping ones. Written
        # against the enum's string values because CHECK runs in SQL, where the
        # Python enum does not exist.
        CheckConstraint(
            "(type = 'blocked' AND start_time IS NULL AND end_time IS NULL) "
            "OR (type = 'custom_hours' AND start_time IS NOT NULL "
            "AND end_time IS NOT NULL AND end_time > start_time)",
            name="hours_match_type",
        ),
    )

    def __repr__(self) -> str:
        return f"<DateOverride id={self.id} date={self.date} type={self.type.value}>"
