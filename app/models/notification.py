"""Delivery log for outbound notifications."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import NotificationChannel, NotificationStatus, NotificationType
from app.models.mixins import TimestampMixin


class NotificationLog(TimestampMixin, Base):
    """One attempt to deliver one message about one booking.

    Phase 6 sends everything through ARQ with retries, which is why this table
    records more than "did it go out". Four states have to be distinguishable,
    and `sent_at` plus `status` alone cannot separate the last two:

      * **queued** — enqueued, not yet attempted.
      * **sent** — delivered. `sent_at` set.
      * **failed, will retry** — the job errored and ARQ will run it again.
      * **failed, permanently** — retries exhausted. Nobody is coming. This is
        the one that needs a human, and without `retry_count` it is
        indistinguishable from the transient case.

    `retry_count` and `last_attempted_at` are what make that distinction
    possible: a failed row whose `retry_count` has reached the job's limit is
    dead, and `last_attempted_at` says how long it has been that way. A
    monitoring query for "notifications that silently gave up" is otherwise
    impossible to write.

    Note `last_attempted_at` is separate from `sent_at` on purpose. `sent_at`
    means success; `last_attempted_at` advances on every attempt including the
    failures, so an unsent row still has a meaningful timestamp.
    """

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    booking_id: Mapped[int] = mapped_column(
        # RESTRICT, matching payments: this is an audit record of what was sent
        # to a real person, and it should outlive any attempt to tidy up.
        ForeignKey("bookings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    type: Mapped[NotificationType] = mapped_column(
        Enum(
            NotificationType,
            name="notification_type",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(
            NotificationChannel,
            name="notification_channel",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        server_default=NotificationChannel.EMAIL.value,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(
            NotificationStatus,
            name="notification_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        server_default=NotificationStatus.QUEUED.value,
        index=True,
    )

    # Success only. NULL for anything not delivered.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Advances on every attempt, successful or not.
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Number of attempts *after* the first. 0 means it has been tried at most
    # once, so "failed with retry_count = 0" is a fresh failure, not a
    # permanent one.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # The last error, for the permanently-failed case. Free text because
    # provider errors have no shape worth modelling.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("retry_count >= 0", name="retry_count_non_negative"),
        # sent_at means delivered, and nothing else may claim it.
        CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)",
            name="sent_at_matches_status",
        ),
        # A row that has been attempted must say when. Guards against a worker
        # bumping retry_count without recording the time, which would make the
        # "gave up long ago" query silently wrong.
        CheckConstraint(
            "retry_count = 0 OR last_attempted_at IS NOT NULL",
            name="retried_rows_have_an_attempt_time",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationLog booking={self.booking_id} {self.type.value} "
            f"{self.status.value} retries={self.retry_count}>"
        )
