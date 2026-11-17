"""The day a backlog item was done.

Revision ID: 0047
Revises: 0046

``backlog_items.done_at`` is when an item was done: merged by a run, closed on GitHub or
marked by the administrator. The console's start page counts the items done on each
day from it. An item that was done before this revision is dated by when it was last
changed, which is when it was done unless something was noted on it afterwards.

Locking: one nullable column and one update of the rows that are done; bounded as in
0022-0046.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "backlog_items"
_COLUMN = "done_at"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0047 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _there() -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has it already.
    if _COLUMN not in _there():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True))
    op.execute(
        sa.text(
            "UPDATE backlog_items SET done_at = updated_at "
            "WHERE state = 'done' AND done_at IS NULL"
        )
    )


def downgrade() -> None:
    _bound()
    if _COLUMN in _there():
        op.drop_column(_TABLE, _COLUMN)
