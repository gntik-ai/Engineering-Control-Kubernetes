"""How hard the supervisor's model reasons, by provider.

Revision ID: 0039
Revises: 0038

``supervisor_definitions.efforts`` holds, for a provider, the reasoning level its client is
started with. Not set: the client's own default, as before.

Locking: one nullable column without a default; bounded as in 0022-0038.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "supervisor_definitions"
_COLUMN = "efforts"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0039 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _there() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_TABLE)
    return _COLUMN in {str(item["name"]) for item in columns}


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has it already.
    if not _there():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    _bound()
    if _there():
        op.drop_column(_TABLE, _COLUMN)
