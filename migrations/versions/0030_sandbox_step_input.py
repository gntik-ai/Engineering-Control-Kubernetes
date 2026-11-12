"""What a step of a run was given.

Revision ID: 0030
Revises: 0029

One nullable column. ``sandbox_steps.input`` holds what a step was given to work with: the
round it is, the findings of a review to act on. Never a credential.

Locking: adding a nullable column without a default is a catalogue change only, bounded as
in 0022-0029. The downgrade drops the column; what steps were given is then no longer shown.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "sandbox_steps"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0030 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _has_column() -> bool:
    """Revision 0001 bootstraps the current models, so a fresh install has the column."""
    columns = sa.inspect(op.get_bind()).get_columns(_TABLE)
    return "input" in {str(item["name"]) for item in columns}


def upgrade() -> None:
    _bound()
    if not _has_column():
        op.add_column(_TABLE, sa.Column("input", sa.JSON(), nullable=True))


def downgrade() -> None:
    _bound()
    if _has_column():
        op.drop_column(_TABLE, "input")
