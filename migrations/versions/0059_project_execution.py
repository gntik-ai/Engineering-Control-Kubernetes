"""Epic execution plans and confirmed technical Issue types."""

import sqlalchemy as sa
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("backlog_items")}
    for column in (
        sa.Column("execution_type", sa.String(16)),
        sa.Column("classification_reason", sa.Text()),
        sa.Column("type_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
    ):
        if column.name not in columns:
            op.add_column("backlog_items", column)
    if not inspector.has_table("epic_execution_plans"):
        op.create_table(
            "epic_execution_plans",
            sa.Column(
                "queue_id",
                sa.Uuid(),
                sa.ForeignKey("work_queues.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "epic_id", sa.Uuid(), sa.ForeignKey("project_epics.id"), nullable=False, index=True
            ),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease", sa.Uuid()),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("detail", sa.String(400)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_epic_execution_due", "epic_execution_plans", ["status", "due_at"])
    # A waiting decision still occupies the original entry and run. Old versions
    # could finish a whole queue while its last change was waiting for a person.
    op.execute("""UPDATE work_queue_entries SET state='running', detail='Awaiting approval'
                  WHERE state='done' AND run_id IN
                  (SELECT id FROM sandbox_runs WHERE state='waiting')""")
    op.execute("""UPDATE work_queues SET state='paused', finished_at=NULL,
                  pause_reason='A workflow is awaiting approval.'
                  WHERE state='finished' AND id IN
                  (SELECT queue_id FROM work_queue_entries WHERE state='running')""")


def downgrade():
    op.drop_table("epic_execution_plans")
    for column in ("execution_type", "classification_reason", "type_confirmed"):
        op.drop_column("backlog_items", column)
