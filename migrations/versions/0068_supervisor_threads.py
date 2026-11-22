"""Keep project conversations separate from the original shared conversation."""

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    schema = sa.inspect(op.get_bind())
    columns = {c["name"] for c in schema.get_columns("supervisor_messages")}
    if "thread" not in columns:
        op.add_column(
            "supervisor_messages",
            sa.Column("thread", sa.String(80), server_default="", nullable=False),
        )
        op.add_column("supervisor_messages", sa.Column("scope", sa.JSON()))
        op.add_column("supervisor_messages", sa.Column("request_id", sa.Uuid()))
        op.create_index(
            "ix_supervisor_thread_time", "supervisor_messages", ["thread", "created_at"]
        )
        op.create_index(
            "uq_supervisor_message_request",
            "supervisor_messages",
            ["thread", "request_id"],
            unique=True,
        )

    if "review_run_id" not in {c["name"] for c in schema.get_columns("workspace_candidates")}:
        op.add_column("workspace_candidates", sa.Column("review_run_id", sa.Uuid()))
        op.create_index(
            "ix_workspace_candidates_review_run_id", "workspace_candidates", ["review_run_id"]
        )
    if "findings" not in {c["name"] for c in schema.get_columns("review_flow_runs")}:
        op.add_column("review_flow_runs", sa.Column("findings", sa.JSON()))
    if "result" not in {c["name"] for c in schema.get_columns("supervisor_actions")}:
        op.add_column("supervisor_actions", sa.Column("result", sa.JSON()))
    if not schema.has_table("supervisor_advice"):
        op.create_table(
            "supervisor_advice",
            sa.Column(
                "run_id",
                sa.Uuid(),
                sa.ForeignKey("review_flow_runs.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("request", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON()),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_supervisor_advice_project_id", "supervisor_advice", ["project_id"])
        op.create_index("ix_supervisor_advice_state", "supervisor_advice", ["state"])


def downgrade():
    op.drop_index("ix_workspace_candidates_review_run_id", table_name="workspace_candidates")
    op.drop_column("workspace_candidates", "review_run_id")
    op.drop_column("review_flow_runs", "findings")
    op.drop_table("supervisor_advice")
    op.drop_column("supervisor_actions", "result")
    op.drop_index("uq_supervisor_message_request", table_name="supervisor_messages")
    op.drop_index("ix_supervisor_thread_time", table_name="supervisor_messages")
    for name in ("request_id", "scope", "thread"):
        op.drop_column("supervisor_messages", name)
