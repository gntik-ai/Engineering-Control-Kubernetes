"""Keep where each User Story stands, with the evidence for it."""

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "story_delivery" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "story_delivery",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("story_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.UniqueConstraint("project_id", "story_id", name="uq_story_delivery_story"),
    )
    op.create_index("ix_story_delivery_project_id", "story_delivery", ["project_id"])
    op.create_index("ix_story_delivery_state", "story_delivery", ["state"])


def downgrade():
    if "story_delivery" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("story_delivery")
