"""Track the local lifecycle of Epics and User Stories."""

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("project_epics", "project_user_stories"):
        if "closed_at" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}:
            op.add_column(table, sa.Column("closed_at", sa.DateTime(timezone=True)))


def downgrade():
    for table in ("project_epics", "project_user_stories"):
        op.drop_column(table, "closed_at")
