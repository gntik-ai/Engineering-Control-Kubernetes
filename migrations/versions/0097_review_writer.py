"""Let a project name the model that writes a record that is missing."""

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("review_settings")}
    if "writer_provider" not in have:
        op.add_column("review_settings", sa.Column("writer_provider", sa.String(32), nullable=True))
    if "writer_model" not in have:
        op.add_column("review_settings", sa.Column("writer_model", sa.String(200), nullable=True))


def downgrade():
    with op.batch_alter_table("review_settings") as batch:
        batch.drop_column("writer_model")
        batch.drop_column("writer_provider")
