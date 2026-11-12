"""Sections: the kinds of work on a project, each with a flow or none.

Revision ID: 0026
Revises: 0025

Every project gets the six core sections. For the projects the workflows know by name, the
three sections whose work already runs get the flow that stands for it; every other
section starts without a flow. Nothing reads the assignment to start work yet.

Locking: a new table only, created in this revision's transaction, bounded as in
0022-0025. The downgrade refuses while a section of the administrator's own exists.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

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
    "kind",
    "position",
    "flow_key",
    "created_at",
    "updated_at",
}
# The core sections as they are when this revision is written: key, name, description,
# and the flow given to the projects of the registry. Frozen here on purpose; the console
# adds whatever a later release adds (SectionRecords.sync_core).
_CORE = (
    (
        "code-audit",
        "Code audit",
        "Reviews of the code base for defects, security weaknesses, dead code and risky "
        "patterns. Confirmed findings become backlog items.",
        "audit-code",
    ),
    (
        "ui-audit",
        "UI audit",
        "Checks of the running application's pages against what they should show and do. "
        "Needs the address the application runs at.",
        None,
    ),
    (
        "ux-audit",
        "UX audit",
        "Walks through the application's navigation: what can be reached, dead ends, "
        "consistency and accessibility. Needs the address the application runs at.",
        None,
    ),
    (
        "code-clean",
        "Code clean",
        "Clean-up that does not change behaviour: unused code, duplication, naming and "
        "small restructurings.",
        None,
    ),
    (
        "enhancement",
        "Enhancement",
        "New capabilities and improvements, found by the platform or asked for by the "
        "administrator.",
        "discover-enhancements",
    ),
    (
        "issues",
        "Issues",
        "Issues reported on the repository: defects and tasks to resolve.",
        "deliver-issue",
    ),
)


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
    if columns != _COLUMNS:
        raise RuntimeError("revision 0026 found a partial bootstrap schema")
    return True


def _give_core_sections() -> None:
    """The core sections each project lacks. Safe to repeat."""
    bind = op.get_bind()
    sections = sa.table(
        _TABLE,
        sa.column("id", sa.Uuid()),
        sa.column("project_id", sa.Uuid()),
        sa.column("key", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("position", sa.Integer()),
        sa.column("flow_key", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    projects = sa.table("projects", sa.column("id", sa.Uuid()), sa.column("origin", sa.String()))
    existing = {
        (row[0], str(row[1]))
        for row in bind.execute(sa.select(sections.c.project_id, sections.c.key))
    }
    now = datetime.now(UTC)
    rows = [
        {
            "id": uuid4(),
            "project_id": project_id,
            "key": key,
            "name": name,
            "description": description,
            "kind": "core",
            "position": position,
            "flow_key": flow if origin == "registry" else None,
            "created_at": now,
            "updated_at": now,
        }
        for project_id, origin in bind.execute(sa.select(projects.c.id, projects.c.origin))
        for position, (key, name, description, flow) in enumerate(_CORE)
        if (project_id, key) not in existing
    ]
    if rows:
        op.bulk_insert(sections, rows)


def upgrade() -> None:
    _bound()
    if not _present():
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("key", sa.String(40), nullable=False),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("description", sa.String(500), nullable=False),
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
        op.create_index(
            op.f("ix_project_sections_project_id"), _TABLE, ["project_id"], unique=False
        )
    _give_core_sections()


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
