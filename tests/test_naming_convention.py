"""Audit every constraint and index name against the metadata naming convention.

`NAMING_CONVENTION` in `app/db/base.py` gives constraints deterministic names so
`op.drop_constraint()` in a downgrade names the right object instead of
guessing. That only holds if names actually follow it — and some cannot, because
the convention has a real blind spot: the `uq` and `ix` rules interpolate only
`column_0_name` / `column_0_label`, so a *composite* constraint would be named
after its first column alone and hide the rest.

Those exceptions are legitimate, but they must be deliberate and listed. This
test enumerates every name in the schema, works out what the convention would
have produced, and fails on any mismatch that is not in `KNOWN_EXCEPTIONS` —
so a future hand-named constraint has to be justified here rather than
discovered later.
"""

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    UniqueConstraint,
)

# Importing the models package is what populates Base.metadata. Without it this
# whole audit passes vacuously against an empty schema — the same trap that
# makes `import app.models` load-bearing in migrations/env.py.
import app.models  # noqa: F401
from app.db.base import Base

# Names that deliberately depart from the convention, each with the reason.
# Adding to this list is fine; doing it silently is not.
KNOWN_EXCEPTIONS: dict[str, str] = {
    "uq_intake_responses_booking_id_question_key": (
        "Composite unique. The uq rule interpolates only column_0_name, so the "
        "convention would give uq_intake_responses_booking_id and hide that "
        "question_key is part of the key."
    ),
    "uq_processed_webhook_events_provider_provider_event_id": (
        "Composite unique, same blind spot: the convention would name it after "
        "`provider` alone, which is the opposite of what makes it unique."
    ),
    "uq_date_overrides_blocked_date": (
        "Partial unique INDEX (WHERE type = 'blocked'), not a UniqueConstraint. "
        "The ix rule would name it ix_date_overrides_date, which reads as an "
        "ordinary non-unique index and hides both the uniqueness and the "
        "predicate."
    ),
}

# Constraints that exist only in the database, because SQLAlchemy metadata
# cannot express them. Listed so the audit is honest about its own coverage.
SQL_ONLY_CONSTRAINTS: dict[str, str] = {
    "excl_bookings_no_overlap": (
        "Exclusion constraint, hand-written in migration c6bfff7c86ef. Alembic "
        "and SQLAlchemy metadata cannot express EXCLUDE USING gist."
    ),
}


def _expected_name(table_name: str, obj: object) -> str | None:
    """What the convention would produce, or None if it cannot be derived."""
    if isinstance(obj, PrimaryKeyConstraint):
        return f"pk_{table_name}"
    if isinstance(obj, ForeignKeyConstraint):
        column = list(obj.columns)[0].name
        referred = list(obj.elements)[0].column.table.name
        return f"fk_{table_name}_{column}_{referred}"
    if isinstance(obj, UniqueConstraint):
        return f"uq_{table_name}_{list(obj.columns)[0].name}"
    if isinstance(obj, Index):
        return f"ix_{table_name}_{list(obj.columns)[0].name}"
    # CheckConstraint interpolates %(constraint_name)s — the author-supplied
    # part — which is not recoverable from the final name. Only the prefix can
    # be checked, which _audit handles separately.
    return None


def _audit() -> list[tuple[str, str, str, bool]]:
    """(table, kind, actual_name, follows_convention) for everything in the schema."""
    rows: list[tuple[str, str, str, bool]] = []

    for table in Base.metadata.sorted_tables:
        for constraint in sorted(table.constraints, key=lambda c: c.name or ""):
            name = constraint.name
            if not isinstance(name, str):
                continue
            if isinstance(constraint, CheckConstraint):
                # Prefix-only check: ck_<table>_<author supplied>.
                rows.append((table.name, "check", name, name.startswith(f"ck_{table.name}_")))
                continue
            expected = _expected_name(table.name, constraint)
            kind = type(constraint).__name__.replace("Constraint", "").lower()
            rows.append((table.name, kind, name, name == expected))

        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            if not isinstance(index.name, str):
                continue
            expected = _expected_name(table.name, index)
            rows.append((table.name, "index", index.name, index.name == expected))

    return rows


def test_every_name_follows_the_convention_or_is_a_known_exception() -> None:
    """The audit itself. Fails on an undocumented hand-named object."""
    undocumented = [
        (table, kind, name)
        for table, kind, name, ok in _audit()
        if not ok and name not in KNOWN_EXCEPTIONS
    ]

    assert not undocumented, (
        "These names do not match the naming convention and are not in "
        f"KNOWN_EXCEPTIONS: {undocumented}. Either rename them, or add them "
        "there with the reason."
    )


def test_known_exceptions_are_all_still_real() -> None:
    """Keeps the exception list from rotting.

    If a name is renamed to follow the convention, its entry here becomes a
    lie that would quietly grant a future mismatch a free pass.
    """
    actual_names = {name for _, _, name, _ in _audit()}
    stale = [name for name in KNOWN_EXCEPTIONS if name not in actual_names]

    assert not stale, f"KNOWN_EXCEPTIONS lists names that no longer exist: {stale}"


def test_exceptions_are_only_the_ones_the_convention_cannot_express() -> None:
    """Every exception must be a composite key or a partial index.

    Both are cases where the convention interpolates one column and would
    produce a misleading name. Any *other* reason for a hand-named object means
    someone worked around the convention rather than hitting its limit, which
    is worth catching in review.
    """
    mismatched = {name for _, _, name, ok in _audit() if not ok}

    assert mismatched == set(KNOWN_EXCEPTIONS), (
        f"convention mismatches {mismatched} do not equal the documented "
        f"exceptions {set(KNOWN_EXCEPTIONS)}"
    )


def test_sql_only_constraints_are_absent_from_metadata() -> None:
    """The exclusion constraint is deliberately not declared in the models.

    Declaring it in both places would let the two drift. This asserts the
    split is intentional rather than an oversight.
    """
    metadata_names = {name for _, _, name, _ in _audit()}

    for name in SQL_ONLY_CONSTRAINTS:
        assert name not in metadata_names, (
            f"{name} is documented as SQL-only but now appears in metadata"
        )


if __name__ == "__main__":  # pragma: no cover
    # `uv run python -m tests.test_naming_convention` prints the full audit.
    rows = _audit()
    width = max(len(name) for _, _, name, _ in rows)
    print(f"{'TABLE':<26} {'KIND':<8} {'NAME':<{width}}  CONVENTION")
    print("-" * (26 + 8 + width + 14))
    for table, kind, name, ok in sorted(rows):
        status = "match" if ok else "EXCEPTION"
        print(f"{table:<26} {kind:<8} {name:<{width}}  {status}")
    print()
    print(
        f"{len(rows)} named objects in metadata, "
        f"{sum(1 for *_, ok in rows if not ok)} documented exceptions."
    )
    for name, reason in SQL_ONLY_CONSTRAINTS.items():
        print(f"\nSQL-only (not in metadata): {name}\n  {reason}")
