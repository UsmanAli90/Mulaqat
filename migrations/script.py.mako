"""${message}

REVIEW CHECKLIST — autogenerate cannot see any of these. Delete the ones
that do not apply, and say what you did for the ones that do.

  [ ] Column type change? Autogenerate renders it as drop-column +
      add-column, which DESTROYS THE DATA. Replace with
      `ALTER COLUMN ... USING <conversion>`, then verify by inserting a row
      at the previous revision and migrating it, not on an empty table.

  [ ] Dropping or retyping a column that a CHECK constraint mentions?
      Postgres silently drops constraints that depend on it, and
      autogenerate will not emit a replacement — it compares check
      constraints by NAME only, so an unchanged name looks like no change.
      Drop and recreate the constraint explicitly.

  [ ] Exclusion constraint? Alembic cannot express one. Hand-write the
      `ALTER TABLE ... ADD CONSTRAINT ... EXCLUDE USING gist (...)` and its
      matching drop.

  [ ] Adding a NOT NULL column to a table with rows? Needs a server_default
      or a three-step add-nullable / backfill / set-not-null.

  [ ] Does `downgrade()` actually reverse `upgrade()`? Run
      up -> down -> up against a database with real rows in it.

  [ ] Anything lossy in the downgrade? Say so in this docstring rather
      than letting someone discover it during an incident.

Handled automatically, no action needed (see migrations/env.py):
  * Custom application column types render as their storage type.
  * Postgres enum CREATE TYPE / DROP TYPE / ALTER TYPE lifecycle.

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from collections.abc import Sequence

import sqlalchemy as sa
${imports if imports else ""}
from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade schema."""
    ${downgrades if downgrades else "pass"}
