"""Host-wide settings. Exactly one row, ever."""

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.models.mixins import TimestampMixin

SETTINGS_ID = 1


class Settings(TimestampMixin, Base):
    """The single settings row.

    Singleton-ness is enforced by the database, not by convention: the primary
    key is pinned to 1 by a CHECK, so a second row is rejected by the PK. This
    is better than "just always use id=1 and be careful", because being careful
    is not a mechanism.

    Named `Settings` like the config class in `app/core/config.py`, but they
    are different things and never interchangeable: that one is deploy-time
    configuration from the environment, this one is runtime configuration the
    host edits from the admin panel. Import one or the other, never both
    unaliased, in the same module.
    """

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    # The host's IANA timezone name. This row is the source of truth once it
    # exists; HOST_TIMEZONE in the environment is only the bootstrap fallback.
    host_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="Asia/Karachi"
    )

    # How far ahead a booking must be made. Stops someone booking a slot that
    # starts in four minutes.
    min_notice_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="12")
    # Counted in the host's timezone, per the agreed ruling.
    max_bookings_per_day: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    # Slot start times are generated on this grid: 15 gives 09:00, 09:15, ...
    slot_granularity_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="15"
    )
    # How close to the start an invitee may still cancel.
    cancellation_cutoff_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="24"
    )

    __table_args__ = (
        CheckConstraint(f"id = {SETTINGS_ID}", name="singleton"),
        CheckConstraint("min_notice_hours >= 0", name="min_notice_non_negative"),
        CheckConstraint("max_bookings_per_day > 0", name="max_bookings_positive"),
        CheckConstraint("slot_granularity_minutes > 0", name="slot_granularity_positive"),
        CheckConstraint("cancellation_cutoff_hours >= 0", name="cancellation_cutoff_non_negative"),
    )

    @validates("host_timezone")
    def validate_host_timezone(self, key: str, value: Any) -> str:
        """Reject anything zoneinfo cannot resolve.

        `@validates` fires on attribute assignment, before any flush — the
        closest SQLAlchemy analogue to an ActiveRecord validation, though it
        runs eagerly on assignment rather than on save.

        This lives in Python because Postgres cannot check an IANA name
        without the tz database loaded, and because a stored UTC offset is
        exactly the failure mode the spec forbids. Getting this wrong breaks
        every slot the app generates, so it is worth the belt and braces.
        """
        if not isinstance(value, str):
            raise ValueError("host_timezone must be a string IANA timezone name")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"host_timezone must be a valid IANA timezone name (got {value!r}). "
                "Use a name like 'Asia/Karachi', never a UTC offset."
            ) from exc
        return value

    def __repr__(self) -> str:
        return f"<Settings tz={self.host_timezone!r} granularity={self.slot_granularity_minutes}m>"
