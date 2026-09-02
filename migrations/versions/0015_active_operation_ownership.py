"""Enforce one active operation owner per action and target.

Revision ID: 0015
Revises: 0014
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_operation_requests_active_target"
ACTIVE_STATUSES = ("PENDING", "DISPATCHING", "STARTED", "RUNNING")
ACTIVE_PREDICATE = "status IN ('PENDING', 'DISPATCHING', 'STARTED', 'RUNNING')"


def _reflected_predicate() -> str | None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return bind.scalar(
            sa.text(
                "SELECT pg_get_expr(index.indpred, index.indrelid) "
                "FROM pg_class AS relation "
                "JOIN pg_index AS index ON index.indexrelid = relation.oid "
                "WHERE relation.relname = :index_name"
            ),
            {"index_name": INDEX_NAME},
        )
    if bind.dialect.name == "sqlite":
        definition = bind.scalar(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :index_name"),
            {"index_name": INDEX_NAME},
        )
        if not isinstance(definition, str) or " WHERE " not in definition.upper():
            return None
        return definition.split(" WHERE ", 1)[-1]
    return None


def _predicate_is_compatible(value: str | None) -> bool:
    if value is None or "status" not in value.lower():
        return False
    quoted_values = re.findall(r"'([^']+)'", value)
    return len(quoted_values) == len(ACTIVE_STATUSES) and set(quoted_values) == set(ACTIVE_STATUSES)


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
    if not _predicate_is_compatible(_reflected_predicate()):
        raise RuntimeError("revision 0015 found an incompatible active-owner predicate")
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
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))
    op.create_index(
        INDEX_NAME,
        "operation_requests",
        ["action", "target_id"],
        unique=True,
        postgresql_where=predicate,
        sqlite_where=predicate,
    )


def downgrade() -> None:
    if any(
        index.get("name") == INDEX_NAME
        for index in sa.inspect(op.get_bind()).get_indexes("operation_requests")
    ):
        op.drop_index(INDEX_NAME, table_name="operation_requests")
