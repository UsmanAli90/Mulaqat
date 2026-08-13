"""The bookable services offered by the host."""

from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import IntakeQuestions
from app.models.mixins import TimestampMixin
from app.schemas.intake import IntakeQuestion


class Service(TimestampMixin, Base):
    """A thing an invitee can book: "30 minute consultation", etc.

    SQLAlchemy 2.0 typed declarative. `Mapped[int]` is not decoration — it is
    read at class-creation time to infer the column type and, critically,
    nullability: `Mapped[str]` is NOT NULL and `Mapped[str | None]` is
    nullable. That is the opposite of ActiveRecord, where the database is the
    source of truth and the model knows nothing until it reflects.
    """

    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Unique index, because the public booking URL is /book/{slug} and two
    # services sharing a slug would make one of them unreachable.
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Numeric, never Float. Binary floating point cannot represent 0.10
    # exactly, and money that is off by a ten-thousandth of a rupee after a few
    # additions is a support ticket. Numeric maps to Python Decimal.
    #
    # Both prices are nullable: a service may be offered only in PKR (manual
    # bank transfer) or only in USD (external checkout). A CHECK below enforces
    # that at least one is set, so a service can never be unbookable in every
    # currency.
    price_pkr: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # Padding around the meeting itself. These feed `bookings.blocked_range` in
    # the next branch, which is what the exclusion constraint actually guards.
    buffer_before_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    buffer_after_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # JSONB, but validated against the Pydantic models on every write by the
    # IntakeQuestions type. server_default is the JSON literal '[]' so rows
    # inserted outside the ORM still get a usable value rather than NULL.
    intake_questions: Mapped[list[IntakeQuestion]] = mapped_column(
        IntakeQuestions,
        nullable=False,
        server_default="[]",
        default=list,
    )

    __table_args__ = (
        # Every CheckConstraint gets an explicit name=, because the metadata
        # naming convention interpolates %(constraint_name)s. An unnamed one
        # raises at class-definition time rather than getting a bad name.
        # These render as ck_services_<name>.
        CheckConstraint("duration_minutes > 0", name="duration_positive"),
        CheckConstraint(
            "buffer_before_minutes >= 0 AND buffer_after_minutes >= 0",
            name="buffers_non_negative",
        ),
        CheckConstraint(
            "(price_pkr IS NULL OR price_pkr >= 0) AND (price_usd IS NULL OR price_usd >= 0)",
            name="prices_non_negative",
        ),
        CheckConstraint(
            "price_pkr IS NOT NULL OR price_usd IS NOT NULL",
            name="at_least_one_price",
        ),
    )

    def __repr__(self) -> str:
        return f"<Service id={self.id} slug={self.slug!r} duration={self.duration_minutes}m>"
