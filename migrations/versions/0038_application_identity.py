"""The other hosts of a project's application, and whether a test identity is stored.

Revision ID: 0038
Revises: 0037

``projects.application_hosts`` names the hosts an application uses beside its address's
own (its API, where it signs in). ``projects.application_identity_at`` says since when
the cluster holds a test identity for the application. The identity itself is a Secret
of the sandbox namespace.

Locking: two nullable columns without a default; bounded as in 0022-0037.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROJECTS = "projects"
_COLUMNS = {
    "application_identity_at": sa.DateTime(timezone=True),
    "application_hosts": sa.JSON(),
}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0038 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _there() -> set[str]:
    columns = sa.inspect(op.get_bind()).get_columns(_PROJECTS)
    return {str(item["name"]) for item in columns}


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has them already.
    there = _there()
    for name, kind in _COLUMNS.items():
        if name not in there:
            op.add_column(_PROJECTS, sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    _bound()
    there = _there()
    for name in _COLUMNS:
        if name in there:
            op.drop_column(_PROJECTS, name)
