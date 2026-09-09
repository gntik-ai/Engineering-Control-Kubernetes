"""Unique identity for new correlated gateway events; preserve all history.

Revision ID: 0017
Revises: 0016
No old telemetry is deleted, relabelled, deduplicated or backfilled.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None
TABLE = "dashboard_events"
COLUMN = "gateway_ingestion_key"
INDEX = "uq_dashboard_events_gateway_ingestion_key"


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision0017 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _compatible() -> bool:
    inspector = sa.inspect(op.get_bind())
    columns = {value["name"]: value for value in inspector.get_columns(TABLE)}
    if COLUMN not in columns:
        return False
    value = columns[COLUMN]
    indexes = [value for value in inspector.get_indexes(TABLE) if value["name"] == INDEX]
    if (
        not isinstance(value["type"], sa.String)
        or value["type"].length != 64
        or value["nullable"] is not True
        or len(indexes) != 1
        or indexes[0]["column_names"] != [COLUMN]
        or not indexes[0]["unique"]
        or indexes[0].get("dialect_options", {}).get("postgresql_where") is not None
        or indexes[0].get("dialect_options", {}).get("sqlite_where") is not None
    ):
        raise RuntimeError("revision0017 found an incompatible gateway event identity")
    return True


def upgrade() -> None:
    _bound()
    if _compatible():
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(64), nullable=True))
    op.create_index(INDEX, TABLE, [COLUMN], unique=True)
    if not _compatible():
        raise RuntimeError("revision0017 gateway event identity was not installed")


def downgrade() -> None:
    _bound()
    if not _compatible():
        raise RuntimeError("revision0017 gateway event identity is absent")
    if (
        op.get_bind().scalar(
            sa.text(
                "SELECT 1 FROM dashboard_events WHERE gateway_ingestion_key IS NOT NULL LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError("revision0017 refuses to discard gateway ingestion receipts")
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
