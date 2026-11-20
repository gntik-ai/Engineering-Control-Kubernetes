"""Add opt-in, project-wide allowances for model reviews."""

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("project_review_budgets"):
        op.create_table(
            "project_review_budgets",
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("token_limit", sa.BigInteger(), nullable=False),
            sa.Column("used", sa.BigInteger(), nullable=False),
            sa.Column("reserved", sa.BigInteger(), nullable=False),
            sa.Column("required", sa.BigInteger()),
        )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT 1 FROM project_review_budgets LIMIT 1")):
        raise RuntimeError("Downgrade would discard configured review token limits")
    op.drop_table("project_review_budgets")
