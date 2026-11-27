"""Keep what caused a bug, when it is known: what makes it a regression."""

import sqlalchemy as sa
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("bug_records")}
    if "caused_by" not in have:
        op.add_column("bug_records", sa.Column("caused_by", sa.String(200), nullable=True))


def downgrade():
    with op.batch_alter_table("bug_records") as batch:
        batch.drop_column("caused_by")
