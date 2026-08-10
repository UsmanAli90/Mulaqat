"""Custom SQLAlchemy column types."""

from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator

from app.schemas.intake import IntakeQuestion, IntakeQuestionSet


class IntakeQuestions(TypeDecorator[list[IntakeQuestion]]):
    """A JSONB column that is always a validated list of `IntakeQuestion`.

    A TypeDecorator wraps an existing type and intercepts the two conversion
    points: Python -> database on write, database -> Python on read. Putting
    validation here rather than in a service function means there is no way to
    write an invalid question set through the ORM — including from the seed
    script, a migration data step, or the admin panel — because every path
    goes through the column.

    The nearest Rails equivalent is an ActiveRecord custom type with
    `cast`/`serialize`, or a `serialize :column, JSON` plus a validation. The
    difference is that this one also gives you typed objects back on read, so
    `service.intake_questions[0].label` is checked by mypy.

    `cache_ok = True` tells SQLAlchemy this type has no per-instance state, so
    statements using it can be cached in the compiled-query cache. Omitting it
    produces a warning and a slower path.
    """

    impl = JSONB
    cache_ok = True

    def process_bind_param(
        self, value: list[IntakeQuestion] | None, dialect: Dialect
    ) -> list[dict[str, Any]] | None:
        """Python -> database. Validates, so bad input never reaches Postgres.

        Accepts already-built `IntakeQuestion` objects or raw dicts; Pydantic
        parses both. A failure raises ValidationError at flush time.
        """
        if value is None:
            return None
        validated = IntakeQuestionSet.model_validate(value)
        # mode="json" so enums become their string values rather than Python
        # objects the JSONB serialiser would choke on.
        #
        # RootModel.model_dump is typed as returning Any, so the cast is what
        # tells mypy the shape we know it has. Under strict mode an unannotated
        # Any leaking out of here would silently disable checking downstream.
        dumped: list[dict[str, Any]] = validated.model_dump(mode="json")
        return dumped

    def process_result_value(
        self, value: list[dict[str, Any]] | None, dialect: Dialect
    ) -> list[IntakeQuestion] | None:
        """Database -> Python.

        Re-validates on read as well. That is not redundant: rows written by a
        migration, a manual psql session, or an older version of this code can
        all be malformed, and failing loudly beats handing the rest of the app
        a dict that it expects to be a model.
        """
        if value is None:
            return None
        return IntakeQuestionSet.model_validate(value).root
