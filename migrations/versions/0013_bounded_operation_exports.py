"""Add bounded redacted operation exports.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _current_bootstrap_schema_present() -> bool:
    """Recognize revision 0001's historical create-all bootstrap safely."""
    inspector = sa.inspect(op.get_bind())
    if "operation_exports" not in set(inspector.get_table_names()):
        return False
    required_columns = {
        "id",
        "operation_id",
        "export_kind",
        "target_id",
        "content_json",
        "content_sha256",
        "size_bytes",
        "created_at",
        "expires_at",
    }
    actual_columns = {str(column["name"]) for column in inspector.get_columns("operation_exports")}
    required_indexes = {
        "ix_operation_exports_operation_id",
        "ix_operation_exports_export_kind",
        "ix_operation_exports_created_at",
        "ix_operation_exports_expires_at",
    }
    actual_indexes = {
        str(index["name"])
        for index in inspector.get_indexes("operation_exports")
        if index.get("name")
    }
    if not required_columns.issubset(actual_columns) or not required_indexes.issubset(
        actual_indexes
    ):
        raise RuntimeError("revision 0013 found a partial bootstrap schema")
    return True


def upgrade() -> None:
    if _current_bootstrap_schema_present():
        return
    op.create_table(
        "operation_exports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("export_kind", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(240), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operation_requests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
    )
    op.create_index("ix_operation_exports_operation_id", "operation_exports", ["operation_id"])
    op.create_index("ix_operation_exports_export_kind", "operation_exports", ["export_kind"])
    op.create_index("ix_operation_exports_created_at", "operation_exports", ["created_at"])
    op.create_index("ix_operation_exports_expires_at", "operation_exports", ["expires_at"])


def downgrade() -> None:
    op.drop_table("operation_exports")
