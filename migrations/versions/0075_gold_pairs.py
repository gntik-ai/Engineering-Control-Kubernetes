"""Keep the pairs of planning records the administrator judges once, as known truth."""

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "gold_pairs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "gold_pairs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("child_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=False),
        sa.Column("bucket", sa.String(24), nullable=False),
        sa.Column("held_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("label", sa.String(16)),
        sa.Column("judged_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id", "kind", "child_id", "parent_id", name="uq_gold_pairs_pair"
        ),
    )
    op.create_index("ix_gold_pairs_project_id", "gold_pairs", ["project_id"])


def downgrade():
    if "gold_pairs" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("gold_pairs")
