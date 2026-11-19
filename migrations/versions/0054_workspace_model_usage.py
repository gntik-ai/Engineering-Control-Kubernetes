"""Keep metered model responses used by repository analysis and planning.

Revision ID: 0054
Revises: 0053
"""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("workspace_model_usage"):
        op.create_table(
            "workspace_model_usage",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("usage", sa.JSON(), nullable=False),
        )
        op.create_index(
            "ix_workspace_model_usage_project_time",
            "workspace_model_usage",
            ["project_id", "recorded_at"],
        )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM workspace_model_usage LIMIT 1")):
        raise RuntimeError("revision 0054 would discard project model usage")
    op.drop_table("workspace_model_usage")
