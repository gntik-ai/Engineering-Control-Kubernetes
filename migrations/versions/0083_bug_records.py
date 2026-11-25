"""Keep what was decided about each bug: severity, area, where it was seen, its duplicate."""

import sqlalchemy as sa
from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "bug_records" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "bug_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("severity", sa.String(16)),
        sa.Column("area", sa.String(60)),
        sa.Column("seen_in", sa.String(200)),
        sa.Column("duplicate_of", sa.Uuid()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "item_id", name="uq_bug_records_item"),
    )
    op.create_index("ix_bug_records_project_id", "bug_records", ["project_id"])


def downgrade():
    if "bug_records" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("bug_records")
