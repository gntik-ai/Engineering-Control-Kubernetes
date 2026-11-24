"""Keep the versions of the page put together for each Epic."""

import sqlalchemy as sa
from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "epic_pages" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "epic_pages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("epic_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.UniqueConstraint("project_id", "epic_id", "version", name="uq_epic_pages_version"),
    )
    op.create_index("ix_epic_pages_project_id", "epic_pages", ["project_id"])


def downgrade():
    if "epic_pages" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("epic_pages")
