"""Keep an inventory of each project's documentation and what changed between readings."""

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def _stamps():
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
    )


def _scan(name: str):
    return sa.Column(name, sa.Uuid(), sa.ForeignKey("doc_scans.id", ondelete="SET NULL"))


def upgrade():
    # Fresh installations may already have the current ORM schema.
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "doc_folders" not in tables:
        op.create_table(
            "doc_folders",
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("include", sa.JSON(), nullable=False),
            sa.Column("exclude", sa.JSON(), nullable=False),
        )
    if "doc_scans" not in tables:
        op.create_table(
            "doc_scans",
            *_stamps(),
            sa.Column("repository", sa.String(200), nullable=False),
            sa.Column("branch", sa.String(120), nullable=False),
            sa.Column("commit_sha", sa.String(40)),
            sa.Column("committed_at", sa.DateTime(timezone=True)),
            sa.Column("previous_sha", sa.String(40)),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("reason", sa.String(16), nullable=False),
            sa.Column("files", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("added", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("modified", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("removed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("ended_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_doc_scans_project_id", "doc_scans", ["project_id"])
        op.create_index("ix_doc_scans_state", "doc_scans", ["state"])
    if "doc_files" not in tables:
        op.create_table(
            "doc_files",
            *_stamps(),
            sa.Column("path", sa.String(700), nullable=False),
            sa.Column("blob_sha", sa.String(40), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("lines", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("title", sa.String(300)),
            sa.Column("sections", sa.JSON(), nullable=False),
            sa.Column("text", sa.Text()),
            _scan("first_scan_id"),
            _scan("changed_scan_id"),
            _scan("removed_scan_id"),
            sa.UniqueConstraint("project_id", "path", name="uq_doc_files_path"),
        )
        op.create_index("ix_doc_files_project_id", "doc_files", ["project_id"])
    if "doc_changes" not in tables:
        op.create_table(
            "doc_changes",
            *_stamps(),
            sa.Column(
                "scan_id",
                sa.Uuid(),
                sa.ForeignKey("doc_scans.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("path", sa.String(700), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("old_blob", sa.String(40)),
            sa.Column("new_blob", sa.String(40)),
            sa.Column("added_lines", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("removed_lines", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sections", sa.JSON(), nullable=False),
            sa.Column("diff", sa.Text()),
        )
        op.create_index("ix_doc_changes_project_id", "doc_changes", ["project_id"])
        op.create_index("ix_doc_changes_scan_id", "doc_changes", ["scan_id"])


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    for name in ("doc_changes", "doc_files", "doc_scans", "doc_folders"):
        if name in tables:
            op.drop_table(name)
