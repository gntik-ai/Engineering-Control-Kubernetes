"""Add explicit evidence retention classes and purge tombstones.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("artifacts")}
    if "purged_at" not in columns:
        op.add_column("artifacts", sa.Column("purged_at", sa.DateTime(timezone=True)))
    if "purge_reason" not in columns:
        op.add_column("artifacts", sa.Column("purge_reason", sa.String(length=120)))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("artifacts")}
    if "purge_reason" in columns:
        op.drop_column("artifacts", "purge_reason")
    if "purged_at" in columns:
        op.drop_column("artifacts", "purged_at")
