"""Persist native provider presence and authentication status.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("quota_snapshots")}
    if "authenticated" not in columns:
        op.add_column("quota_snapshots", sa.Column("authenticated", sa.Boolean()))
    if "cli_present" not in columns:
        op.add_column("quota_snapshots", sa.Column("cli_present", sa.Boolean()))
    if "status" not in columns:
        op.add_column("quota_snapshots", sa.Column("status", sa.String(length=80)))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("quota_snapshots")}
    if "status" in columns:
        op.drop_column("quota_snapshots", "status")
    if "cli_present" in columns:
        op.drop_column("quota_snapshots", "cli_present")
    if "authenticated" in columns:
        op.drop_column("quota_snapshots", "authenticated")
