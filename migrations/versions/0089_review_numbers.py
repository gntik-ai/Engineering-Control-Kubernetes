"""Keep the numbers a project set for its reviews, and whether a cheaper model goes first."""

import sqlalchemy as sa
from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("review_settings")}
    if "cheaper_first" not in have:
        op.add_column(
            "review_settings",
            sa.Column("cheaper_first", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    if "numbers" not in have:
        op.add_column("review_settings", sa.Column("numbers", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("review_settings") as table:
        table.drop_column("numbers")
        table.drop_column("cheaper_first")
