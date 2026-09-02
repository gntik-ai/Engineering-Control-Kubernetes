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


def _reflected_predicate() -> tuple[str | None, bool]:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        row = bind.execute(
            sa.text(
                "SELECT pg_get_expr(index.indpred, index.indrelid), index.indisvalid "
                "FROM pg_class AS index_relation "
                "JOIN pg_index AS index ON index.indexrelid = index_relation.oid "
                "JOIN pg_class AS table_relation ON table_relation.oid = index.indrelid "
                "JOIN pg_namespace AS namespace ON namespace.oid = table_relation.relnamespace "
                "WHERE namespace.nspname = current_schema() "
                "AND table_relation.relname = 'operation_requests' "
                "AND index_relation.relkind = 'i' "
                "AND index_relation.relname = :index_name"
            ),
            {"index_name": INDEX_NAME},
        ).first()
        if row is None:
            return None, False
        return str(row[0]) if row[0] is not None else None, bool(row[1])
    if bind.dialect.name == "sqlite":
        definition = bind.scalar(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :index_name"),
            {"index_name": INDEX_NAME},
        )
        if not isinstance(definition, str) or " WHERE " not in definition.upper():
            return None, False
        parts = re.split(r"\bwhere\b", definition, maxsplit=1, flags=re.IGNORECASE)
        return parts[-1], True
    return None, False


def _predicate_is_compatible(value: str | None) -> bool:
    if value is None:
        return False
    normalized = " ".join(value.lower().split())
    if (
        "status" not in normalized
        or re.search(r"\bnot\b", normalized)
        or "<>" in normalized
        or "!=" in normalized
        or (" in " not in normalized and "= any" not in normalized)
    ):
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
    predicate, valid = _reflected_predicate()
    if not valid or not _predicate_is_compatible(predicate):
        raise RuntimeError("revision 0015 found an incompatible active-owner predicate")
    return True


def upgrade() -> None:
    if op.get_bind().dialect.name not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0015 supports only PostgreSQL and SQLite")
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
            "revision 0015 refuses active-owner duplicates; reconcile "
            f"action={str(duplicate.action)[:80]!r} target_id={str(duplicate.target_id)[:240]!r} "
            f"owner_count={int(duplicate.owner_count)}"
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
