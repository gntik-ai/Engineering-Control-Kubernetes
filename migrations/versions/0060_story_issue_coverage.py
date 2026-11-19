"""Persist evidence and resumable comparisons of imported issues to User Stories."""

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("story_issue_coverage"):
        op.create_table(
            "story_issue_coverage",
            sa.Column("project_id", sa.Uuid(), primary_key=True),
            sa.Column("story_id", sa.Uuid(), primary_key=True),
            sa.Column("revision", sa.String(64), nullable=False),
            sa.Column("checked", sa.Integer(), nullable=False),
            sa.Column("matches", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(
                ["project_id", "story_id"],
                ["project_user_stories.project_id", "project_user_stories.id"],
                ondelete="CASCADE",
            ),
        )


def downgrade():
    op.drop_table("story_issue_coverage")
