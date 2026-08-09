"""baseline empty

Intentionally empty. This revision exists to prove the Alembic wiring works
end to end — upgrade and downgrade both run against a real Postgres — and to
give Phase 2's real schema migration a parent to hang off, so the first
substantive migration is a normal revision rather than a special case.

Revision ID: 15cc4a0cad0a
Revises:
Create Date: 2026-08-09 18:00:24.517767

"""

from collections.abc import Sequence

revision: str = "15cc4a0cad0a"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op. Phase 2 introduces the first real tables."""


def downgrade() -> None:
    """No-op."""
