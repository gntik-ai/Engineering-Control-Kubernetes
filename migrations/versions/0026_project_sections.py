"""Sections: the kinds of work on a project, each with a routing guide and a flow or none.

Revision ID: 0026
Revises: 0025

The table only. The sections themselves are created by the console when it starts
(SectionRecords.sync_core): every project gets the six core sections with their routing
guides, and for the projects the workflows know by name, the three sections whose work
already runs get the flow that stands for it. Nothing reads an assignment to start work yet.

Locking: a new table only, created in this revision's transaction, bounded as in
0022-0025. The downgrade refuses while a section of the administrator's own exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "project_sections"
_COLUMNS = {
    "id",
    "project_id",
    "key",
    "name",
    "description",
    "use_when",
    "avoid_when",
    "labels",
    "examples",
    "kind",
    "position",
    "flow_key",
    "created_at",
    "updated_at",
}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0026 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _present() -> bool:
    """Revision 0001 bootstraps the current models, so a fresh install has the table."""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return False
    columns = {str(item["name"]) for item in inspector.get_columns(_TABLE)}
    # A later revision may add columns, which a fresh install then has from the start.
    if not _COLUMNS <= columns:
        raise RuntimeError("revision 0026 found a partial bootstrap schema")
    return True


def upgrade() -> None:
    _bound()
    if _present():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("use_when", sa.String(600), nullable=False),
        sa.Column("avoid_when", sa.String(600), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("examples", sa.JSON(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("flow_key", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('core', 'custom')", name=op.f("ck_project_sections_kind")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_sections_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_sections")),
        sa.UniqueConstraint("project_id", "key", name=op.f("uq_project_sections_project_key")),
    )
    op.create_index(op.f("ix_project_sections_project_id"), _TABLE, ["project_id"], unique=False)


def downgrade() -> None:
    _bound()
    custom = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM project_sections WHERE kind = 'custom'"))
        .scalar()
    )
    if custom:
        raise RuntimeError(
            "revision 0026 downgrade refuses to drop sections the administrator added"
        )
    op.drop_table(_TABLE)
