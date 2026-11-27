"""Keep the parts of a project's documentation, and what Coverage found of each."""

import sqlalchemy as sa
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "doc_parts" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "doc_parts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(700), nullable=False),
        sa.Column("heading", sa.String(300), nullable=False),
        sa.Column("under", sa.JSON(), nullable=False),
        sa.Column("first_line", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_line", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("words", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("about", sa.String(8), nullable=False),
        sa.Column("said", sa.String(8), nullable=True),
        sa.Column("state", sa.String(12), nullable=False, server_default="unchecked"),
        sa.Column("covered_by", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("vector", sa.LargeBinary(), nullable=True),
        sa.Column("gone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_doc_parts_project_id", "doc_parts", ["project_id"])
    op.create_index("ix_doc_parts_state", "doc_parts", ["project_id", "state"])


def downgrade():
    op.drop_index("ix_doc_parts_state", table_name="doc_parts")
    op.drop_index("ix_doc_parts_project_id", table_name="doc_parts")
    op.drop_table("doc_parts")
