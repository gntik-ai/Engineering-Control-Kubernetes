"""The backlog: the work wanted on a project, routed to a section.

Revision ID: 0027
Revises: 0026

Two tables. ``backlog_items`` holds one row per piece of work: an issue of the project's
repository or an item the administrator wrote. ``backlog_syncs`` records when a project was
last brought in step with GitHub. The older issue records of the workflows are untouched.

Locking: new tables only, created in this revision's transaction, bounded as in 0022-0026.
The downgrade refuses while an item the administrator wrote or decided on exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ITEMS = "backlog_items"
_SYNCS = "backlog_syncs"
_ITEM_COLUMNS = {
    "id",
    "project_id",
    "section_id",
    "title",
    "source",
    "state",
    "routed_by",
    "routing_reason",
    "repository",
    "issue_number",
    "issue_url",
    "issue_state",
    "issue_labels",
    "issue_updated_at",
    "created_at",
    "updated_at",
}
_SYNC_COLUMNS = {"project_id", "synced_at", "open_issues", "error"}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0027 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _present(table: str, expected: set[str]) -> bool:
    """Revision 0001 bootstraps the current models, so a fresh install has the tables."""
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return False
    columns = {str(item["name"]) for item in inspector.get_columns(table)}
    # A later revision may add columns, which a fresh install then has from the start.
    if not expected <= columns:
        raise RuntimeError("revision 0027 found a partial bootstrap schema")
    return True


def upgrade() -> None:
    _bound()
    if not _present(_ITEMS, _ITEM_COLUMNS):
        op.create_table(
            _ITEMS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("section_id", sa.Uuid(), nullable=True),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("routed_by", sa.String(16), nullable=True),
            sa.Column("routing_reason", sa.String(300), nullable=True),
            sa.Column("repository", sa.String(140), nullable=True),
            sa.Column("issue_number", sa.Integer(), nullable=True),
            sa.Column("issue_url", sa.String(300), nullable=True),
            sa.Column("issue_state", sa.String(16), nullable=True),
            sa.Column("issue_labels", sa.JSON(), nullable=False),
            sa.Column("issue_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "source IN ('github', 'manual')", name=op.f("ck_backlog_items_source")
            ),
            sa.CheckConstraint(
                "state IN ('proposed', 'accepted', 'skipped', 'done')",
                name=op.f("ck_backlog_items_state"),
            ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["projects.id"],
                name=op.f("fk_backlog_items_project_id_projects"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["section_id"],
                ["project_sections.id"],
                name=op.f("fk_backlog_items_section_id_project_sections"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_backlog_items")),
            sa.UniqueConstraint(
                "project_id",
                "repository",
                "issue_number",
                name=op.f("uq_backlog_items_project_issue"),
            ),
        )
        op.create_index(op.f("ix_backlog_items_project_id"), _ITEMS, ["project_id"])
        op.create_index(op.f("ix_backlog_items_section_id"), _ITEMS, ["section_id"])
        op.create_index(op.f("ix_backlog_items_state"), _ITEMS, ["state"])
    if not _present(_SYNCS, _SYNC_COLUMNS):
        op.create_table(
            _SYNCS,
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("open_issues", sa.Integer(), nullable=False),
            sa.Column("error", sa.String(300), nullable=True),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["projects.id"],
                name=op.f("fk_backlog_syncs_project_id_projects"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("project_id", name=op.f("pk_backlog_syncs")),
        )


def downgrade() -> None:
    _bound()
    kept = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM backlog_items "
                "WHERE source = 'manual' OR routed_by = 'administrator' OR state <> 'proposed'"
            )
        )
        .scalar()
    )
    if kept:
        raise RuntimeError(
            "revision 0027 downgrade refuses to drop backlog items the administrator "
            "wrote or decided on"
        )
    op.drop_table(_SYNCS)
    op.drop_table(_ITEMS)
