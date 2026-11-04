"""Store cleanup candidate lists outside the workflow context (NEW-245).

Revision ID: 0024
Revises: 0023

The CleanupWorkflow threaded every candidate list through each activity payload; on
2026-11-03 565 worktree and 1,487 file candidates overran Temporal's 2 MB payload limit
and cleanup stopped. cleanup.discover now stores the lists here, in chunks keyed by the
cleanup Run, and the workflow pages them through bounded activities.

Locking: a new table only, created in this revision's transaction; nothing reads it until
the release that ships it is running. Lock acquisition and statements are bounded as in
0022/0023. The downgrade refuses while any chunk exists (a cleanup run is in flight).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "cleanup_run_items"
_COLUMNS = {"id", "run_id", "list_name", "ordinal", "items_json", "created_at", "updated_at"}


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0024 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _present() -> bool:
    """Revision 0001 bootstraps the current models, so a fresh install has the table."""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return False
    columns = {str(item["name"]) for item in inspector.get_columns(_TABLE)}
    unique = {
        str(item["name"]): list(item["column_names"])
        for item in inspector.get_unique_constraints(_TABLE)
    }
    if columns != _COLUMNS or unique.get("uq_cleanup_run_items_chunk") != [
        "run_id",
        "list_name",
        "ordinal",
    ]:
        raise RuntimeError("revision 0024 found a partial bootstrap schema")
    return True


def upgrade() -> None:
    _bound()
    if _present():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("list_name", sa.String(80), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("items_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_cleanup_run_items_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cleanup_run_items")),
        sa.UniqueConstraint(
            "run_id", "list_name", "ordinal", name=op.f("uq_cleanup_run_items_chunk")
        ),
    )


def downgrade() -> None:
    _bound()
    chunks = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM cleanup_run_items")).scalar()
    if chunks:
        raise RuntimeError(
            "revision 0024 downgrade refuses stored cleanup lists (a cleanup run is in flight)"
        )
    op.drop_table(_TABLE)
