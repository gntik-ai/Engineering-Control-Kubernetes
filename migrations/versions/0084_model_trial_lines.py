"""Keep what a model said in a trial, beside the answer on record."""

import sqlalchemy as sa
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade():
    # Fresh installations may already have the current ORM schema.
    if "model_trial_lines" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "model_trial_lines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("choice_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("child_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("answer", sa.String(16), nullable=False),
        sa.Column("parent", sa.String(500)),
        sa.Column("reason", sa.Text()),
        sa.Column("checked", sa.Boolean(), nullable=False),
        sa.Column("tokens", sa.Integer()),
        sa.Column("cost", sa.Float()),
        sa.Column("ref_model", sa.String(200)),
        sa.Column("ref_answer", sa.String(16), nullable=False),
        sa.Column("ref_parent", sa.String(500)),
        sa.Column("ref_reason", sa.Text()),
        sa.Column("ref_checked", sa.Boolean(), nullable=False),
        sa.Column("ref_tokens", sa.Integer()),
        sa.Column("ref_decision", sa.String(16), nullable=False),
        sa.Column("agrees", sa.Boolean(), nullable=False),
        sa.Column("links", sa.Integer(), nullable=False),
        sa.Column("links_same", sa.Integer(), nullable=False),
        sa.Column("differs", sa.JSON()),
        sa.UniqueConstraint("run_id", "child_id", name="uq_model_trial_lines_run_child"),
    )
    op.create_index("ix_model_trial_lines_project_id", "model_trial_lines", ["project_id"])
    op.create_index("ix_model_trial_lines_run_id", "model_trial_lines", ["run_id"])


def downgrade():
    if "model_trial_lines" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("model_trial_lines")
