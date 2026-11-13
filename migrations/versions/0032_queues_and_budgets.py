"""Queues of work, and budgets.

Revision ID: 0032
Revises: 0031

Three tables. ``work_queues`` holds the queues of a project, ``work_queue_entries`` the
place of each item in one, ``budgets`` how much may run for the platform, a project or an
account.

Locking: new tables only, created in this revision's transaction, bounded as in 0022-0031.
The downgrade refuses while a queue is running or paused: the order still to be worked on
would be lost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_QUEUES = "work_queues"
_ENTRIES = "work_queue_entries"
_BUDGETS = "budgets"
_COLUMNS = {
    _QUEUES: {"id", "project_id", "name", "state", "at_once", "proposed_by", "reason"},
    _ENTRIES: {"id", "queue_id", "item_id", "position", "state", "run_id"},
    _BUDGETS: {"id", "scope", "scope_id", "at_once", "per_day"},
}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0032 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _present(table: str) -> bool:
    """Revision 0001 bootstraps the current models, so a fresh install has the tables."""
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return False
    columns = {str(item["name"]) for item in inspector.get_columns(table)}
    if not _COLUMNS[table] <= columns:
        raise RuntimeError("revision 0032 found a partial bootstrap schema")
    return True


def _stamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    _bound()
    if not _present(_QUEUES):
        op.create_table(
            _QUEUES,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("at_once", sa.Integer(), nullable=False),
            sa.Column("proposed_by", sa.String(16), nullable=True),
            sa.Column("reason", sa.String(600), nullable=True),
            sa.Column("pause_reason", sa.String(300), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            *_stamps(),
            sa.CheckConstraint(
                "state IN ('draft', 'running', 'paused', 'finished')",
                name=op.f("ck_work_queues_state"),
            ),
            sa.CheckConstraint("at_once BETWEEN 1 AND 4", name=op.f("ck_work_queues_at_once")),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["projects.id"],
                name=op.f("fk_work_queues_project_id_projects"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_work_queues")),
        )
        for column in ("project_id", "state"):
            op.create_index(op.f(f"ix_work_queues_{column}"), _QUEUES, [column])
    if not _present(_ENTRIES):
        op.create_table(
            _ENTRIES,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("queue_id", sa.Uuid(), nullable=False),
            sa.Column("item_id", sa.Uuid(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("run_id", sa.Uuid(), nullable=True),
            sa.Column("reason", sa.String(300), nullable=True),
            sa.Column("detail", sa.String(300), nullable=True),
            *_stamps(),
            sa.CheckConstraint(
                "state IN ('waiting', 'running', 'done', 'failed', 'skipped')",
                name=op.f("ck_work_queue_entries_state"),
            ),
            sa.ForeignKeyConstraint(
                ["queue_id"],
                ["work_queues.id"],
                name=op.f("fk_work_queue_entries_queue_id_work_queues"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["item_id"],
                ["backlog_items.id"],
                name=op.f("fk_work_queue_entries_item_id_backlog_items"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["sandbox_runs.id"],
                name=op.f("fk_work_queue_entries_run_id_sandbox_runs"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_work_queue_entries")),
            sa.UniqueConstraint(
                "queue_id", "item_id", name=op.f("uq_work_queue_entries_queue_item")
            ),
        )
        for column in ("queue_id", "item_id", "state", "run_id"):
            op.create_index(op.f(f"ix_work_queue_entries_{column}"), _ENTRIES, [column])
    if not _present(_BUDGETS):
        op.create_table(
            _BUDGETS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("scope", sa.String(16), nullable=False),
            sa.Column("scope_id", sa.Uuid(), nullable=True),
            sa.Column("at_once", sa.Integer(), nullable=True),
            sa.Column("per_day", sa.Integer(), nullable=True),
            *_stamps(),
            sa.CheckConstraint(
                "scope IN ('platform', 'project', 'account')", name=op.f("ck_budgets_scope")
            ),
            sa.CheckConstraint(
                "at_once IS NULL OR at_once BETWEEN 1 AND 100", name=op.f("ck_budgets_at_once")
            ),
            sa.CheckConstraint(
                "per_day IS NULL OR per_day BETWEEN 1 AND 10000", name=op.f("ck_budgets_per_day")
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_budgets")),
        )
        op.create_index(op.f("ix_budgets_scope_id"), _BUDGETS, ["scope_id"])


def downgrade() -> None:
    _bound()
    active = (
        op.get_bind()
        .execute(sa.text("SELECT COUNT(*) FROM work_queues WHERE state IN ('running', 'paused')"))
        .scalar()
    )
    if active:
        raise RuntimeError("revision 0032 downgrade refuses to drop queues that are under way")
    op.drop_table(_BUDGETS)
    op.drop_table(_ENTRIES)
    op.drop_table(_QUEUES)
