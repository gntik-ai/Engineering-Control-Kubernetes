"""Flows of the platform's own that the administrator deleted.

Revision ID: 0048
Revises: 0047

A flow made in the console is deleted with its versions. A flow the platform knows from
the start is in its code: ``removed_flows`` keeps that it was deleted, so that nothing
lists it, assigns it or starts it until it is put back.

Locking: one new table; bounded as in 0022-0047.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "removed_flows"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0048 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has it already.
    if _TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("flow_key", sa.String(length=64), nullable=False, unique=True),
        )


def downgrade() -> None:
    _bound()
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table(_TABLE)
