"""Evaluations that go through the past runs of another flow.

Revision ID: 0044
Revises: 0043

``evaluations.of_flow`` names the flow whose past runs an evaluation goes through, when
that is not the flow it tries: a flow in which a model asked over an API does the work,
tried on the runs of the flow in which an agent in a sandbox did it. Empty, it is the
flow tried, as before.

Locking: one nullable column; bounded as in 0022-0043.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "evaluations"
_COLUMN = "of_flow"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0044 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _there() -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has it already.
    if _COLUMN not in _there():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=64), nullable=True))


def downgrade() -> None:
    _bound()
    if _COLUMN in _there():
        op.drop_column(_TABLE, _COLUMN)
