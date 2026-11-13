"""Whether a test identity for a project's application is stored.

Revision ID: 0038
Revises: 0037

``projects.application_identity_at`` says since when the cluster holds a test identity
for the project's application. The identity itself is a Secret of the sandbox namespace.

Locking: one nullable column without a default; bounded as in 0022-0037.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROJECTS = "projects"
_COLUMN = "application_identity_at"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0038 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _has() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_PROJECTS)
    return _COLUMN in {str(item["name"]) for item in columns}


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has it already.
    if not _has():
        op.add_column(_PROJECTS, sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    _bound()
    if _has():
        op.drop_column(_PROJECTS, _COLUMN)
