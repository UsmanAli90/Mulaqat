"""Declarative base for all ORM models.

Every model in Phase 2 inherits from `Base`. Alembic's autogenerate compares
`Base.metadata` against the live database to produce migrations, so a model
that does not reach this metadata is invisible to migrations.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic names for every index and constraint.
#
# Without this, Postgres assigns names like `bookings_service_id_fkey` and
# SQLAlchemy invents its own for unnamed constraints, so `op.drop_constraint()`
# in a downgrade has to guess — and guesses wrong. Rails sidesteps the whole
# problem because it regenerates schema.rb from scratch; Alembic writes real
# DDL that must name the exact object it is dropping.
#
# This has to be in place *before* the first schema is generated. Adding it
# afterwards would mean every existing constraint keeps its old auto-generated
# name while new ones follow the convention, which is the worst of both.
#
# The tokens are SQLAlchemy's: %(table_name)s, %(column_0_name)s,
# %(column_0_label)s (table+column), %(referred_table_name)s for the FK target,
# and %(constraint_name)s for the name you passed in.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 declarative base.

    Unlike ActiveRecord, this carries no persistence methods: there is no
    `Booking.find` or `booking.save`. Models are plain typed classes, and all
    querying goes through a Session. That separation is why the test fixtures
    can hand every test its own transaction-scoped session.
    """

    # Note the "ck" convention interpolates %(constraint_name)s, so every
    # CheckConstraint must be given an explicit `name=`. An unnamed one raises
    # at class-definition time rather than silently getting a bad name — which
    # is the behaviour we want, but it will bite in Phase 2 if you forget.
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
