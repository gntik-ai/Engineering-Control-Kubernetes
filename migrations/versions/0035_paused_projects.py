"""A project can be paused.

Revision ID: 0035
Revises: 0034

``projects`` gains since when no work may start on a project, why, and who said so: the
administrator, or the supervisor when the project's runs kept failing.

Locking: three nullable columns without defaults, a catalogue change only; bounded as in
0022-0034. The downgrade refuses while a project is paused: it would start working again
without anyone having decided so.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROJECTS = "projects"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0035 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _columns() -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(_PROJECTS)}


def _added() -> list[sa.Column[object]]:
    """Revision 0001 bootstraps the current models, so a fresh install has the columns."""
    return [
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pause_reason", sa.String(300), nullable=True),
        sa.Column("paused_by", sa.String(16), nullable=True),
    ]


def upgrade() -> None:
    _bound()
    present = _columns()
    for column in _added():
        if column.name not in present:
            op.add_column(_PROJECTS, column)


def downgrade() -> None:
    _bound()
    present = _columns()
    if "paused_at" in present:
        paused = (
            op.get_bind()
            .execute(sa.text("SELECT COUNT(*) FROM projects WHERE paused_at IS NOT NULL"))
            .scalar()
        )
        if paused:
            raise RuntimeError("revision 0035 downgrade refuses to resume paused projects")
    for column in _added():
        if column.name in present:
            op.drop_column(_PROJECTS, column.name)
