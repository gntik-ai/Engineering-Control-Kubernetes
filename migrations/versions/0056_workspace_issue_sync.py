"""Persist the last completed read of open and closed GitHub issues."""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade():
    if "issues_synced_at" not in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("workspace_imports")
    }:
        op.add_column(
            "workspace_imports", sa.Column("issues_synced_at", sa.DateTime(timezone=True))
        )


def downgrade():
    op.drop_column("workspace_imports", "issues_synced_at")
