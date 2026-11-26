"""Keep what a judge said to each proposed planning record."""

import sqlalchemy as sa
from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("workspace_candidates")}
    if "judged" not in have:
        op.add_column("workspace_candidates", sa.Column("judged", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("workspace_candidates") as batch:
        batch.drop_column("judged")
