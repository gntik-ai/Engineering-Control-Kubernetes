"""Enforce one active operation owner per action and target.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_operation_requests_active_target"
ACTIVE_PREDICATE = "status IN ('PENDING', 'DISPATCHING', 'STARTED', 'RUNNING')"


def _existing_index_is_compatible() -> bool:
    inspector = sa.inspect(op.get_bind())
    indexes = {
        str(index["name"]): index
        for index in inspector.get_indexes("operation_requests")
        if index.get("name")
    }
    existing = indexes.get(INDEX_NAME)
    if existing is None:
        return False
    if not bool(existing.get("unique")) or list(existing.get("column_names") or []) != [
        "action",
        "target_id",
    ]:
        raise RuntimeError("revision 0015 found an incompatible active-owner index")
    return True


def upgrade() -> None:
    if _existing_index_is_compatible():
        return
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT action, target_id, count(*) AS owner_count "
                "FROM operation_requests "
                "WHERE status IN ('PENDING', 'DISPATCHING', 'STARTED', 'RUNNING') "
                "GROUP BY action, target_id HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            "revision 0015 refuses active-owner duplicates; reconcile the exact rows first"
        )
    predicate = sa.text(ACTIVE_PREDICATE)
    op.create_index(
        INDEX_NAME,
        "operation_requests",
        ["action", "target_id"],
        unique=True,
        postgresql_where=predicate,
        sqlite_where=predicate,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="operation_requests")
