"""Remember model capabilities and the last successful catalog read."""

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("provider_accounts")}
    for column in (
        sa.Column("catalog_error", sa.String(300), nullable=True),
        sa.Column("model_catalog", sa.JSON(), nullable=True),
        sa.Column("catalog_updated_at", sa.DateTime(timezone=True), nullable=True),
    ):
        if column.name not in columns:
            op.add_column("provider_accounts", column)


def downgrade():
    op.drop_column("provider_accounts", "catalog_updated_at")
    op.drop_column("provider_accounts", "model_catalog")
    op.drop_column("provider_accounts", "catalog_error")
