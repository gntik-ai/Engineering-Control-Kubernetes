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


def upgrade() -> None:
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
