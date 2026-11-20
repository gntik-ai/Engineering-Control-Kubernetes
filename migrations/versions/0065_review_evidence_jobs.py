"""Store review evidence snapshots, task checkpoints and isolated model comparisons."""

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade():
    schema = sa.inspect(op.get_bind())
    if "evidence_trace" not in {c["name"] for c in schema.get_columns("review_flow_runs")}:
        op.add_column("review_flow_runs", sa.Column("evidence_trace", sa.JSON(), nullable=True))
    if not schema.has_table("review_wiki_pages"):
        op.create_table(
            "review_wiki_pages",
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("fingerprint", sa.String(64), primary_key=True),
            sa.Column("source_key", sa.String(120), nullable=False, index=True),
            sa.Column("page", sa.JSON(), nullable=False),
        )
    if schema.has_table("review_evidence_jobs"):
        return
    op.create_table(
        "review_evidence_jobs",
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
            index=True,
        ),
        sa.Column("state", sa.String(20), nullable=False, index=True),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("token_cap", sa.Integer(), nullable=False),
        sa.Column("comparison_signature", sa.String(64), index=True),
        sa.Column("spent", sa.Integer(), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("lease", sa.Uuid()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT 1 FROM review_evidence_jobs LIMIT 1")):
        raise RuntimeError("Downgrade would discard review evidence and pilot history")
    if op.get_bind().scalar(sa.text("SELECT 1 FROM review_wiki_pages LIMIT 1")):
        raise RuntimeError("Downgrade would discard review wiki history")
    op.drop_table("review_evidence_jobs")
    op.drop_table("review_wiki_pages")
    op.drop_column("review_flow_runs", "evidence_trace")
