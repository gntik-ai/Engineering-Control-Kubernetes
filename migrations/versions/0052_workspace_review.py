"""Human-reviewed planning candidates, issue mapping and durable GitHub publication.

Revision ID: 0052
Revises: 0051
"""

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def common() -> list:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "github_closed_at" not in {c["name"] for c in inspector.get_columns("backlog_items")}:
        op.add_column("backlog_items", sa.Column("github_closed_at", sa.DateTime(timezone=True)))
    if "workspace_candidates" not in tables:
        op.create_table(
            "workspace_candidates",
            *common(),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("identity", sa.String(96), nullable=False),
            sa.Column("number", sa.Integer()),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("parents", sa.JSON(), nullable=False),
            sa.Column("decision", sa.String(16), nullable=False),
            sa.Column("local_id", sa.Uuid()),
            sa.UniqueConstraint(
                "project_id", "kind", "identity", name="uq_workspace_candidate_identity"
            ),
            sa.UniqueConstraint("project_id", "number", name="uq_workspace_candidate_issue"),
        )
    if "workspace_actions" not in tables:
        op.create_table(
            "workspace_actions",
            *common(),
            sa.Column("operation", sa.String(16), nullable=False),
            sa.Column("local_id", sa.Uuid(), nullable=False),
            sa.Column("repository", sa.String(140), nullable=False),
            sa.Column("number", sa.Integer()),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("lease", sa.Uuid()),
            sa.Column("lease_until", sa.DateTime(timezone=True)),
            sa.Column("detail", sa.String(350)),
            sa.UniqueConstraint(
                "project_id", "operation", "local_id", name="uq_workspace_action_record"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("workspace_candidates", "workspace_actions"):
        if bind.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")):
            raise RuntimeError("revision 0052 would discard human reviews or GitHub writes")
    if bind.scalar(
        sa.text("SELECT 1 FROM backlog_items WHERE github_closed_at IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("revision 0052 would discard an original closure date")
    op.drop_table("workspace_actions")
    op.drop_table("workspace_candidates")
    op.drop_column("backlog_items", "github_closed_at")
