"""Items with their own text, incidents, and a project's cut-over.

Revision ID: 0034
Revises: 0033

``backlog_items`` gains what an item needs when it is not a GitHub issue: its own
description, facts worth showing, the run that produced it and a stable name for where it
came from, unique within the project so that nothing is proposed twice.
``supervisor_incidents`` holds what went wrong with the work and what the supervisor made
of it. ``supervisor_definitions.operations`` says whether and how often the supervisor
looks at such trouble by itself. ``projects`` records when a project's work moved here
from the original system and what was brought along.

Locking: one new table, nullable columns without defaults (catalogue changes only) and one
unique index on a column that is empty in every existing row; bounded as in 0022-0033. The
downgrade refuses while an incident is still open or items carry a description: both would
be lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ITEMS = "backlog_items"
_INCIDENTS = "supervisor_incidents"
_DEFINITIONS = "supervisor_definitions"
_PROJECTS = "projects"
_ORIGIN = "uq_backlog_items_project_origin"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0034 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(table)}


def _has_origin() -> bool:
    inspector = sa.inspect(op.get_bind())
    names = {item["name"] for item in inspector.get_unique_constraints(_ITEMS)}
    names |= {item["name"] for item in inspector.get_indexes(_ITEMS)}
    return _ORIGIN in names


def _stamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _added() -> dict[str, list[sa.Column[object]]]:
    """Revision 0001 bootstraps the current models, so a fresh install has every column."""
    return {
        _ITEMS: [
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("facts", sa.JSON(), nullable=True),
            sa.Column("source_run_id", sa.Uuid(), nullable=True),
            sa.Column("origin_ref", sa.String(160), nullable=True),
        ],
        _DEFINITIONS: [sa.Column("operations", sa.JSON(), nullable=True)],
        _PROJECTS: [
            sa.Column("cut_over_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("brought_over", sa.JSON(), nullable=True),
        ],
    }


def upgrade() -> None:
    _bound()
    for table, columns in _added().items():
        present = _columns(table)
        for column in columns:
            if column.name not in present:
                op.add_column(table, column)
    if "ix_backlog_items_source_run_id" not in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes(_ITEMS)
    }:
        op.create_index(op.f("ix_backlog_items_source_run_id"), _ITEMS, ["source_run_id"])
    if not _has_origin():
        # A unique index rather than a constraint: SQLite cannot add a constraint to a
        # table that exists, and both engines treat empty values as different.
        op.create_index(_ORIGIN, _ITEMS, ["project_id", "origin_ref"], unique=True)
    if _INCIDENTS not in _tables():
        op.create_table(
            _INCIDENTS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("ref", sa.String(120), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("summary", sa.String(600), nullable=False),
            sa.Column("facts", sa.JSON(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=True),
            sa.Column("run_id", sa.Uuid(), nullable=True),
            sa.Column("item_id", sa.Uuid(), nullable=True),
            sa.Column("turn_run_id", sa.Uuid(), nullable=True),
            sa.Column("diagnosis", sa.Text(), nullable=True),
            sa.Column("confidence", sa.String(8), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            *_stamps(),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_supervisor_incidents")),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["projects.id"],
                name=op.f("fk_supervisor_incidents_project_id_projects"),
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint("kind", "ref", name=op.f("uq_supervisor_incidents_kind_ref")),
            sa.CheckConstraint(
                "state IN ('open', 'looking', 'handled', 'waiting', 'dismissed')",
                name=op.f("ck_supervisor_incidents_state"),
            ),
        )
        for column in ("kind", "state", "project_id", "run_id", "item_id", "turn_run_id"):
            op.create_index(op.f(f"ix_supervisor_incidents_{column}"), _INCIDENTS, [column])


def downgrade() -> None:
    _bound()
    bind = op.get_bind()
    waiting = bind.execute(
        sa.text("SELECT COUNT(*) FROM supervisor_incidents WHERE state IN ('open', 'waiting')")
    ).scalar()
    described = bind.execute(
        sa.text("SELECT COUNT(*) FROM backlog_items WHERE description IS NOT NULL")
    ).scalar()
    if waiting or described:
        raise RuntimeError(
            "revision 0034 downgrade refuses to drop open incidents or items' descriptions"
        )
    op.drop_table(_INCIDENTS)
    if _has_origin():
        op.drop_index(_ORIGIN, table_name=_ITEMS)
    op.drop_index(op.f("ix_backlog_items_source_run_id"), table_name=_ITEMS)
    for table, columns in _added().items():
        present = _columns(table)
        for column in columns:
            if column.name in present:
                op.drop_column(table, column.name)
