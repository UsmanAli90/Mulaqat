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

from sqlalchemy import Boolean, CheckConstraint, Date, Enum, Index, Integer, Text, Time, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class DateOverrideType(StrEnum):
    """Whether an override removes availability or replaces it."""

    BLOCKED = "blocked"
    CUSTOM_HOURS = "custom_hours"


class AvailabilityRule(TimestampMixin, Base):
    """One recurring weekly window of availability.

    ========================================================================
    A WINDOW THAT ENDS AT MIDNIGHT MUST USE end_time = 23:59:59.
    NEVER 24:00:00. Read this before touching Phase 3's slot generation.
    ========================================================================

    Postgres accepts `'24:00:00'::time` and `end_time > start_time` passes for
    it, so the database will happily store that row. But Python's
    `datetime.time` tops out at 23:59:59.999999, so asyncpg raises
    `ValueError: hour must be in 0..23` when reading it back. The row becomes
    permanently unreadable by the application — every query touching that table
    fails, not just the one rule. `ck_availability_rules_end_time_before_24h`
    now rejects the value outright so it cannot be inserted by any path,
    including raw SQL.

    **The consequence, stated loudly because it will look like a bug:**
    23:59:59 is one second short of midnight. A rule of 22:00-23:59:59 with
    30-minute slots does NOT yield a slot at 23:30, because that slot would end
    at 00:00:00, one second past the window. Expect exactly one missing slot at
    the end of any midnight-ending window.

    This is not hypothetical for this host. 23:00-02:00 PKT is 14:00-17:00 US
    Eastern, so an overnight-in-Karachi window is a normal working slot for
    international clients. Such a window is entered as two rules
    (Mon 22:00-23:59:59 and Tue 00:00-02:00), and the seam between them is
    where the missing slot appears.

    Phase 3 must decide how to close that one-second seam. The cheapest fix is
    to treat a window end of 23:59:59 as exclusive-midnight when generating
    slots; the alternative is storing minutes-from-midnight integers (0-1440)
    instead of TIME, which removes the problem entirely at the cost of
    readability in psql. Flagged in PHASE_2_NOTES.md rather than decided here.
    """

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
        # Blocks the 24:00:00 poison value described in the class docstring.
        # Postgres considers it a valid TIME; Python cannot represent it, so a
        # single such row makes the whole table unreadable through asyncpg.
        CheckConstraint("end_time < TIME '24:00:00'", name="end_time_before_24h"),
        CheckConstraint("start_time < TIME '24:00:00'", name="start_time_before_24h"),
        Index("ix_availability_rules_day_of_week", "day_of_week"),
    )

    def __repr__(self) -> str:
        return (
            f"<AvailabilityRule id={self.id} dow={self.day_of_week} "
            f"{self.start_time}-{self.end_time}>"
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

    The 24:00:00 warning on `AvailabilityRule` applies to this table's time
    columns too, and is enforced by the same kind of CHECK.
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
        # Same 24:00:00 guard as AvailabilityRule. NULL-safe: a blocked row has
        # NULL times, and `NULL < TIME '24:00:00'` is NULL, which a CHECK
        # treats as satisfied.
        CheckConstraint("end_time < TIME '24:00:00'", name="end_time_before_24h"),
        CheckConstraint("start_time < TIME '24:00:00'", name="start_time_before_24h"),
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
