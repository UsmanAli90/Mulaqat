"""Shared column mixins."""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """`created_at` / `updated_at`, both TIMESTAMPTZ.

    A mixin rather than a base class: it contributes columns to whatever table
    includes it and has no table of its own. Four tables in this phase need
    these two columns, which is why it exists at all — it is not speculative
    structure.

    `timezone=True` makes these TIMESTAMPTZ. Every timestamp in this database
    is TIMESTAMPTZ and stored as UTC, per the spec; Postgres normalises to UTC
    on write and psycopg/asyncpg hand back aware datetimes on read, so a naive
    datetime can never sneak in and be silently misinterpreted as local time.

    `server_default=func.now()` puts the clock in the database rather than in
    Python. That matters because the app, the ARQ worker and a psql session
    are three different processes whose clocks can disagree.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        # onupdate fires on ORM-issued UPDATEs. It does NOT fire for raw SQL
        # run outside the ORM — a trigger would be needed for that. Acceptable
        # here because nothing writes to these tables outside the ORM.
        onupdate=func.now(),
        nullable=False,
    )
