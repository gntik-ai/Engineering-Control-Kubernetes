"""Tasks managed from the console: what a saved task is for, and the ones not offered.

Revision ID: 0040
Revises: 0039

``saved_tasks.description`` says what a task kept under a name is for. ``hidden_tasks``
names the platform's own kinds of task the administrator does not want offered in the
designer's list; a flow that has one keeps it.

Locking: one nullable column without a default and one new table; bounded as in
0022-0039.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SAVED = "saved_tasks"
_COLUMN = "description"
_HIDDEN = "hidden_tasks"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0040 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _described() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_SAVED)
    return _COLUMN in {str(item["name"]) for item in columns}


def _hidden() -> bool:
    return _HIDDEN in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has both already.
    if not _described():
        op.add_column(_SAVED, sa.Column(_COLUMN, sa.Text(), nullable=True))
    if not _hidden():
        op.create_table(
            _HIDDEN,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("task", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_hidden_tasks")),
            sa.UniqueConstraint("task", name=op.f("uq_hidden_tasks_task")),
        )


def downgrade() -> None:
    _bound()
    if _hidden():
        op.drop_table(_HIDDEN)
    if _described():
        op.drop_column(_SAVED, _COLUMN)
