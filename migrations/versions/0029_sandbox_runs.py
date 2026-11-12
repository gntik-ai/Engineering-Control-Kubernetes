"""Runs: work done in sandboxes, step by step.

Revision ID: 0029
Revises: 0028

Two tables. ``sandbox_runs`` holds one row per piece of work done in sandboxes, ``sandbox_steps`` one
row per step of it, each step one pod. The older execution records of the workflows are
untouched.

Locking: new tables only, created in this revision's transaction, bounded as in 0022-0028.
The downgrade refuses while a run is not finished: its sandbox would be left without a
record.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = "sandbox_runs"
_STEPS = "sandbox_steps"
_RUN_COLUMNS = {
    "id",
    "kind",
    "state",
    "title",
    "project_id",
    "item_id",
    "account_id",
    "outcome",
    "detail",
    "cancel_requested",
    "data",
    "started_at",
    "finished_at",
    "created_at",
    "updated_at",
}
_STEP_COLUMNS = {
    "id",
    "run_id",
    "position",
    "name",
    "state",
    "provider",
    "account_id",
    "pod_name",
    "attempt",
    "exit_code",
    "outcome",
    "detail",
    "output",
    "log",
    "started_at",
    "finished_at",
    "created_at",
    "updated_at",
}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0029 supports PostgreSQL and SQLite only")
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
        raise RuntimeError("revision 0029 found a partial bootstrap schema")
    return True


def upgrade() -> None:
    _bound()
    if not _present(_RUNS, _RUN_COLUMNS):
        op.create_table(
            _RUNS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("state", sa.String(20), nullable=False),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("project_id", sa.Uuid(), nullable=True),
            sa.Column("item_id", sa.Uuid(), nullable=True),
            sa.Column("account_id", sa.Uuid(), nullable=True),
            sa.Column("outcome", sa.String(40), nullable=True),
            sa.Column("detail", sa.String(600), nullable=True),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "state IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled')",
                name=op.f("ck_sandbox_runs_state"),
            ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["projects.id"],
                name=op.f("fk_sandbox_runs_project_id_projects"),
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["item_id"],
                ["backlog_items.id"],
                name=op.f("fk_sandbox_runs_item_id_backlog_items"),
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["account_id"],
                ["provider_accounts.id"],
                name=op.f("fk_sandbox_runs_account_id_provider_accounts"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_sandbox_runs")),
        )
        for column in ("kind", "state", "project_id", "item_id", "account_id"):
            op.create_index(op.f(f"ix_sandbox_runs_{column}"), _RUNS, [column])
    if not _present(_STEPS, _STEP_COLUMNS):
        op.create_table(
            _STEPS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("run_id", sa.Uuid(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(40), nullable=False),
            sa.Column("state", sa.String(20), nullable=False),
            sa.Column("provider", sa.String(16), nullable=True),
            sa.Column("account_id", sa.Uuid(), nullable=True),
            sa.Column("pod_name", sa.String(80), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column("outcome", sa.String(40), nullable=True),
            sa.Column("detail", sa.String(600), nullable=True),
            sa.Column("output", sa.JSON(), nullable=True),
            sa.Column("log", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'skipped')",
                name=op.f("ck_sandbox_steps_state"),
            ),
            sa.ForeignKeyConstraint(
                ["run_id"],
                ["sandbox_runs.id"],
                name=op.f("fk_sandbox_steps_run_id_sandbox_runs"),
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["account_id"],
                ["provider_accounts.id"],
                name=op.f("fk_sandbox_steps_account_id_provider_accounts"),
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_sandbox_steps")),
            sa.UniqueConstraint("run_id", "position", name=op.f("uq_sandbox_steps_run_position")),
        )
        for column in ("run_id", "state", "account_id"):
            op.create_index(op.f(f"ix_sandbox_steps_{column}"), _STEPS, [column])


def downgrade() -> None:
    _bound()
    unfinished = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM sandbox_runs WHERE state IN ('queued', 'running', 'waiting')"
            )
        )
        .scalar()
    )
    if unfinished:
        raise RuntimeError("revision 0029 downgrade refuses to drop runs that are not finished")
    op.drop_table(_STEPS)
    op.drop_table(_RUNS)
