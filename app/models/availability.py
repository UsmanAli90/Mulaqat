"""When the host is available.

Two tables, one recurring and one exceptional:

  availability_rules — the normal week ("Monday 20:00 to 23:00")
  date_overrides     — deviations for a specific date

Both store **host-local wall-clock times as INTEGER minutes from midnight**,
deliberately without a date or a timezone. The spec is explicit that
availability is authored in the host's local time and converted to UTC at
slot-generation time (Phase 3), which is what makes the schedule survive a DST
transition: "Monday 8pm" stays 8pm on both sides of the jump, and it is the
corresponding UTC instant that moves.

Storing these as TIMESTAMPTZ would force a date onto a rule that has none, and
storing a UTC offset would rot twice a year. Integers were chosen over `TIME`
for arithmetic reasons explained in `app/core/wall_clock.py`.

    Conversion reference (see app/core/wall_clock.py for helpers)
    ─────────────────────────────────────────────────────────────
        0  →  00:00       540  →  09:00      1320  →  22:00
      720  →  12:00      1200  →  20:00      1440  →  24:00

Minute 1440 is the exclusive end of the day and is how a window ending at
midnight is expressed. A window crossing midnight is still entered as two
rules, one per day — see the CHECK on `end_minute > start_minute`.
"""

from datetime import date as date_type
from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, Date, Enum, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.wall_clock import MINUTES_PER_DAY
from app.db.base import Base
from app.models.mixins import TimestampMixin


class DateOverrideType(StrEnum):
    """Whether an override removes availability or replaces it."""

    BLOCKED = "blocked"
    CUSTOM_HOURS = "custom_hours"


class AvailabilityRule(TimestampMixin, Base):
    """One recurring weekly window of availability.

    Times are minutes from midnight, host-local. "Monday 20:00 to 23:00" is
    `day_of_week=0, start_minute=1200, end_minute=1380`.

    A window crossing midnight is two rules. "Karachi 22:00 to 02:00" — which
    is 14:00-17:00 US Eastern, a normal working window for international
    clients rather than an edge case — is entered as Monday 1320..1440 plus
    Tuesday 0..120. Because 1440 and 0 are the same instant, the two halves
    meet exactly with no gap; this is the concrete improvement over the old
    `TIME` representation, where the seam cost a slot.
    """

    __tablename__ = "availability_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 0 = Monday .. 6 = Sunday, matching Python's date.weekday() so Phase 3
    # never needs a conversion. Note this differs from Postgres EXTRACT(DOW),
    # where 0 = Sunday — worth remembering before writing raw SQL against it.
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)

    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="day_of_week_range"),
        CheckConstraint(
            f"start_minute >= 0 AND start_minute <= {MINUTES_PER_DAY}",
            name="start_minute_range",
        ),
        CheckConstraint(
            f"end_minute >= 0 AND end_minute <= {MINUTES_PER_DAY}",
            name="end_minute_range",
        ),
        # Forbids a window that wraps past midnight; such availability is two
        # rules. Allowing end < start would put a wrap-around special case into
        # every calculation in the Phase 3 availability engine.
        CheckConstraint("end_minute > start_minute", name="end_after_start"),
        Index("ix_availability_rules_day_of_week", "day_of_week"),
    )

    def __repr__(self) -> str:
        return (
            f"<AvailabilityRule id={self.id} dow={self.day_of_week} "
            f"{self.start_minute}-{self.end_minute}>"
        )


class DateOverride(TimestampMixin, Base):
    """A deviation from the weekly rules for one specific date.

    **Precedence rule, authoritative:** a `blocked` row blocks the entire date
    regardless of any `custom_hours` rows for that same date. Phase 3
    implements it; the rule is recorded here because it is a property of the
    data model, not of the engine that reads it.

    The shape of the table follows from that rule:

      * At most one `blocked` row per date — a second one would be meaningless,
        so `uq_date_overrides_blocked_date` (a partial unique index) rejects
        it.
      * Any number of `custom_hours` rows per date, so a split day such as
        09:00-12:00 plus 17:00-20:00 is expressible as two rows.
      * A `blocked` row coexisting with `custom_hours` rows is permitted by the
        database and resolved by the precedence rule above. The alternative —
        forbidding the combination — would mean the host cannot block a day
        without first deleting custom hours they may want back tomorrow.

    Times are minutes from midnight, as on `AvailabilityRule`.
    """

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
            # rejected.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )

    # Populated only for CUSTOM_HOURS; the CHECK below enforces that.
    start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # One constraint expressing "hours are required for custom_hours and
        # forbidden for blocked", rather than three overlapping ones. Written
        # against the enum's string values because CHECK runs in SQL, where the
        # Python enum does not exist.
        CheckConstraint(
            "(type = 'blocked' AND start_minute IS NULL AND end_minute IS NULL) "
            "OR (type = 'custom_hours' AND start_minute IS NOT NULL "
            "AND end_minute IS NOT NULL AND end_minute > start_minute)",
            name="hours_match_type",
        ),
        # NULL-safe: a blocked row has NULL minutes, and `NULL >= 0` is NULL,
        # which a CHECK treats as satisfied.
        CheckConstraint(
            f"start_minute >= 0 AND start_minute <= {MINUTES_PER_DAY}",
            name="start_minute_range",
        ),
        CheckConstraint(
            f"end_minute >= 0 AND end_minute <= {MINUTES_PER_DAY}",
            name="end_minute_range",
        ),
        # Partial unique index: at most one blocked row per date, while
        # custom_hours rows stay unconstrained. A plain unique index on `date`
        # would wrongly forbid split days.
        Index(
            "uq_date_overrides_blocked_date",
            "date",
            unique=True,
            postgresql_where=text("type = 'blocked'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<DateOverride id={self.id} date={self.date} type={self.type.value}>"
