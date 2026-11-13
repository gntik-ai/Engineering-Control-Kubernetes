"""Triggers, and where a project's application runs.

Revision ID: 0037
Revises: 0036

``flow_triggers`` holds what starts a section's flow without anyone asking: a schedule, a
moved branch, an arriving item. ``saved_tasks`` holds tasks kept under a name to be used
in other flows. ``projects.application_url`` is where the project's application runs,
for flows that look at it in a browser.

Locking: two new tables and one nullable column without a default; bounded as in
0022-0036. The downgrade refuses while a trigger is switched on: dropping it would stop
work the administrator arranged, silently.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TRIGGERS = "flow_triggers"
_PROJECTS = "projects"
_SAVED = "saved_tasks"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0037 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _has_address() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(_PROJECTS)
    return "application_url" in {str(item["name"]) for item in columns}


def upgrade() -> None:
    _bound()
    # Revision 0001 bootstraps the current models, so a fresh install has both already.
    if _TRIGGERS not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            _TRIGGERS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("section_key", sa.String(40), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("spec", sa.JSON(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("state", sa.JSON(), nullable=True),
            sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_result", sa.String(300), nullable=True),
            sa.Column("fired", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_flow_triggers")),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["projects.id"],
                name=op.f("fk_flow_triggers_project_id_projects"),
                ondelete="CASCADE",
            ),
            sa.CheckConstraint(
                "kind IN ('schedule', 'push', 'item')", name=op.f("ck_flow_triggers_kind")
            ),
        )
        op.create_index(op.f("ix_flow_triggers_project_id"), _TRIGGERS, ["project_id"])
    if _SAVED not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            _SAVED,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("task", sa.String(64), nullable=False),
            sa.Column("params", sa.JSON(), nullable=False),
            sa.Column("wording", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_tasks")),
            sa.UniqueConstraint("name", name=op.f("uq_saved_tasks_name")),
        )
    if not _has_address():
        op.add_column(_PROJECTS, sa.Column("application_url", sa.String(300), nullable=True))


def downgrade() -> None:
    _bound()
    switched_on = (
        op.get_bind().execute(sa.text("SELECT COUNT(*) FROM flow_triggers WHERE enabled")).scalar()
    )
    if switched_on:
        raise RuntimeError("revision 0037 downgrade refuses to drop triggers that are switched on")
    if _has_address():
        op.drop_column(_PROJECTS, "application_url")
    op.drop_table(_TRIGGERS)
    op.drop_table(_SAVED)
