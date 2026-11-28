"""Keep since when each stage of a project's reviews is as it is, and what it was before."""

import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("review_settings")}
    if "stages" not in have:
        op.add_column("review_settings", sa.Column("stages", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("review_settings") as batch:
        batch.drop_column("stages")
