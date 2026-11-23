"""Record who made each planning link and why, and which parent is the primary one."""

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "planning_links" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "planning_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("child_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text()),
        sa.Column("evidence", sa.JSON()),
        sa.Column("strength", sa.String(16)),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("primary_by", sa.String(16)),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_planning_links_project_id", "planning_links", ["project_id"])
    op.create_index("ix_planning_links_child", "planning_links", ["project_id", "kind", "child_id"])
    op.create_index(
        "uq_planning_links_active",
        "planning_links",
        ["project_id", "kind", "child_id", "parent_id"],
        unique=True,
        sqlite_where=sa.text("removed_at IS NULL"),
        postgresql_where=sa.text("removed_at IS NULL"),
    )


def downgrade():
    if "planning_links" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("planning_links")
