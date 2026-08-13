"""Answers to a service's intake questions."""

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class IntakeResponse(TimestampMixin, Base):
    """One answer, keyed by the question's stable `key`.

    `question_key` is a plain string rather than a foreign key, because the
    questions themselves live in `services.intake_questions` (JSONB) and have
    no rows to point at. That is a deliberate consequence of the agreed
    JSONB-not-a-table decision.

    It also means answers survive the question being edited or removed, which
    is the behaviour we want: an answer someone gave in March must still be
    readable in June even if the host has since reworded or deleted the
    question. It is why `IntakeQuestion.key` is constrained to a stable slug
    and documented as never-reuse — reusing a key for a different meaning would
    silently re-label historical answers.
    """

    __tablename__ = "intake_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    booking_id: Mapped[int] = mapped_column(
        # RESTRICT, matching every other foreign key into `bookings`. This was
        # CASCADE at first, on the reasoning that an answer has no meaning
        # without its booking and there is no financial record to preserve.
        # That was wrong, for a reason only visible once the other keys were
        # RESTRICT: a booking with a payment, a notification or a reschedule
        # link cannot be deleted at all, so the cascade could only ever fire
        # for a booking with none of those — an expired pending one — where
        # the intake answers are the *only* surviving record of what the
        # person actually asked for.
        #
        # Mixed rules also let a delete half-succeed. Uniform RESTRICT makes
        # it a single rule: nothing about a booking disappears quietly.
        # See the deletion policy on the Booking model.
        ForeignKey("bookings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    question_key: Mapped[str] = mapped_column(String(50), nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # One answer per question per booking. A multi-select answer is stored
        # as a single joined string rather than several rows, so that a repeat
        # key always means a bug rather than a legitimate second answer.
        # Named in full rather than left to the metadata convention: the "uq"
        # rule interpolates only column_0_name, which would produce
        # uq_intake_responses_booking_id and hide the fact that question_key is
        # part of the key.
        UniqueConstraint(
            "booking_id", "question_key", name="uq_intake_responses_booking_id_question_key"
        ),
    )

    def __repr__(self) -> str:
        return f"<IntakeResponse booking={self.booking_id} key={self.question_key!r}>"
