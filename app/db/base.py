"""Declarative base for all ORM models.

Every model in Phase 2 inherits from `Base`. Alembic's autogenerate compares
`Base.metadata` against the live database to produce migrations, so a model
that does not reach this metadata is invisible to migrations.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base.

    Unlike ActiveRecord, this carries no persistence methods: there is no
    `Booking.find` or `booking.save`. Models are plain typed classes, and all
    querying goes through a Session. That separation is why the test fixtures
    can hand every test its own transaction-scoped session.
    """
