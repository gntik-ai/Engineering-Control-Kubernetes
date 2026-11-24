"""Keep what was decided about each proposal for the plan's structure."""

import sqlalchemy as sa
from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "structure_decisions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "structure_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("proposal", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("made", sa.JSON()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "proposal", name="uq_structure_decisions_proposal"),
    )
    op.create_index("ix_structure_decisions_project_id", "structure_decisions", ["project_id"])


def downgrade():
    if "structure_decisions" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("structure_decisions")
