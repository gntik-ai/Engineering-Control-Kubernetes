"""Keep both positive and negative Story/issue comparisons across inventory changes."""

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade():
    if "planning_reviews_paused_at" not in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("projects")
    }:
        op.add_column(
            "projects",
            sa.Column("planning_reviews_paused_at", sa.DateTime(timezone=True), nullable=True),
        )
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("story_issue_coverage")}
    for name, kind in (("story_revision", sa.String(64)), ("issue_versions", sa.JSON())):
        if name not in columns:
            op.add_column("story_issue_coverage", sa.Column(name, kind, nullable=True))


def downgrade():
    op.drop_column("projects", "planning_reviews_paused_at")
    op.drop_column("story_issue_coverage", "issue_versions")
    op.drop_column("story_issue_coverage", "story_revision")
