"""Record confirmed planning changes and their reviewed impact."""

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade():
    schema = sa.inspect(op.get_bind())
    if "confirmation_token" not in {c["name"] for c in schema.get_columns("workspace_actions")}:
        op.add_column("workspace_actions", sa.Column("confirmation_token", sa.String(64)))
    if not schema.has_table("planning_lifecycle_receipts"):
        op.create_table(
            "planning_lifecycle_receipts",
            sa.Column("token", sa.String(64), primary_key=True),
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("record_id", sa.Uuid(), nullable=False),
            sa.Column("operation", sa.String(12), nullable=False),
            sa.Column("impact", sa.JSON(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade():
    with op.batch_alter_table("workspace_actions") as batch:
        batch.drop_column("confirmation_token")
    op.drop_table("planning_lifecycle_receipts")
