"""Keep how far each kind of change goes without the administrator, and which batches did."""

import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def _columns(table):
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "alone" not in _columns("reorg_batches"):
        op.add_column(
            "reorg_batches",
            sa.Column("alone", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "alone" not in _columns("review_settings"):
        op.add_column("review_settings", sa.Column("alone", sa.JSON(), nullable=True))


def downgrade():
    for table in ("review_settings", "reorg_batches"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("alone")
