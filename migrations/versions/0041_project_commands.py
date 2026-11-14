"""A project's own commands: how it is installed and how its tests are run.

Revision ID: 0041
Revises: 0040

``projects.toolchain`` holds the command lines the administrator wrote on the project's
page for installing the project and for running its tests. Not set: the commands of the
platform's own file for the project, as before.

Locking: one nullable column without a default; bounded as in 0022-0040.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "projects"
_COLUMN = "toolchain"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0041 supports PostgreSQL and SQLite only")
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
