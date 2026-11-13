"""The chat channel's memory.

Revision ID: 0033
Revises: 0032

Two tables. ``chat_state`` holds how far the chat channel has read its transport's updates
and since when it reports; ``chat_deliveries`` what it already told the administrator, so
that each thing is told once.

Locking: new tables only, created in this revision's transaction, bounded as in 0022-0032.
The downgrade drops both: the channel then starts again from now and repeats nothing older.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE = "chat_state"
_DELIVERIES = "chat_deliveries"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0033 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _present(table: str) -> bool:
    """Revision 0001 bootstraps the current models, so a fresh install has the tables."""
    return table in set(sa.inspect(op.get_bind()).get_table_names())


def _stamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    _bound()
    if not _present(_STATE):
        op.create_table(
            _STATE,
            sa.Column("key", sa.String(40), nullable=False),
            sa.Column("value", sa.String(200), nullable=False),
            *_stamps(),
            sa.PrimaryKeyConstraint("key", name=op.f("pk_chat_state")),
        )
    if not _present(_DELIVERIES):
        op.create_table(
            _DELIVERIES,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("ref", sa.String(120), nullable=False),
            *_stamps(),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_deliveries")),
            sa.UniqueConstraint("kind", "ref", name=op.f("uq_chat_deliveries_kind_ref")),
        )
        op.create_index(op.f("ix_chat_deliveries_kind"), _DELIVERIES, ["kind"])


def downgrade() -> None:
    _bound()
    op.drop_table(_DELIVERIES)
    op.drop_table(_STATE)
