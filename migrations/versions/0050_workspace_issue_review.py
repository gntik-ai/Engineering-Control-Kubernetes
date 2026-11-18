"""Track Superadmin approval independently of backlog delivery.

Revision ID: 0050
Revises: 0049
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "workspace_review" not in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("backlog_items")
    }:
        op.add_column("backlog_items", sa.Column("workspace_review", sa.String(16), nullable=True))


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM backlog_items WHERE workspace_review IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("revision 0050 would discard workspace approvals")
    op.drop_column("backlog_items", "workspace_review")
