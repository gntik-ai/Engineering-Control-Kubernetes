"""Keep one kind of run and answer for every choice of a parent, not only for issues."""

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

RUN = (
    "id, created_at, updated_at, project_id, state, provider, model, token_cap, spent, total, "
    "done, failed, uncounted, detail, claimed_at, reserved, ended_at"
)
REVIEW = (
    "id, created_at, updated_at, project_id, run_id, state, revision, shortlist, answer, "
    "confidence, reason, evidence, checked, missing, provider, model, tokens, detail, decision, "
    "decided_at"
)


def _stamps():
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
    )


def _run_columns():
    return (
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("token_cap", sa.BigInteger(), nullable=False),
        sa.Column("spent", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uncounted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("reserved", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )


def _review_columns(runs: str):
    return (
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey(f"{runs}.id", ondelete="SET NULL")),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("shortlist", sa.JSON(), nullable=False),
        sa.Column("answer", sa.String(16)),
        sa.Column("confidence", sa.String(16)),
        sa.Column("reason", sa.Text()),
        sa.Column("evidence", sa.JSON()),
        sa.Column("checked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("missing", sa.JSON()),
        sa.Column("provider", sa.String(32)),
        sa.Column("model", sa.String(200)),
        sa.Column("tokens", sa.Integer()),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )


def upgrade():
    # Fresh installations may already have the current ORM schema.
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "choice_runs" not in tables:
        op.create_table(
            "choice_runs",
            *_stamps(),
            sa.Column("kind", sa.String(16), nullable=False),
            *_run_columns(),
            sa.Column("current_child", sa.Uuid()),
        )
        op.create_index("ix_choice_runs_project_id", "choice_runs", ["project_id"])
        op.create_index("ix_choice_runs_state", "choice_runs", ["state"])
    if "choice_reviews" not in tables:
        op.create_table(
            "choice_reviews",
            *_stamps(),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("child_id", sa.Uuid(), nullable=False),
            *_review_columns("choice_runs"),
            sa.Column("parent_id", sa.Uuid()),
            sa.UniqueConstraint("project_id", "kind", "child_id", name="uq_choice_reviews_child"),
        )
        op.create_index("ix_choice_reviews_project_id", "choice_reviews", ["project_id"])
    # What was asked about issues so far is kept.
    if "orphan_runs" in tables:
        op.execute(
            f"INSERT INTO choice_runs ({RUN}, kind, current_child) "
            f"SELECT {RUN}, 'issue_story', current_item FROM orphan_runs"
        )
    if "orphan_reviews" in tables:
        op.execute(
            f"INSERT INTO choice_reviews ({REVIEW}, kind, child_id, parent_id) "
            f"SELECT {REVIEW}, 'issue_story', item_id, story_id FROM orphan_reviews"
        )
        op.drop_table("orphan_reviews")
    if "orphan_runs" in tables:
        op.drop_table("orphan_runs")


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "orphan_runs" not in tables:
        op.create_table(
            "orphan_runs", *_stamps(), *_run_columns(), sa.Column("current_item", sa.Uuid())
        )
        op.create_index("ix_orphan_runs_project_id", "orphan_runs", ["project_id"])
        op.create_index("ix_orphan_runs_state", "orphan_runs", ["state"])
    if "orphan_reviews" not in tables:
        op.create_table(
            "orphan_reviews",
            *_stamps(),
            sa.Column(
                "item_id",
                sa.Uuid(),
                sa.ForeignKey("backlog_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            *_review_columns("orphan_runs"),
            sa.Column("story_id", sa.Uuid()),
            sa.UniqueConstraint("project_id", "item_id", name="uq_orphan_reviews_item"),
        )
        op.create_index("ix_orphan_reviews_project_id", "orphan_reviews", ["project_id"])
    if "choice_runs" in tables:
        op.execute(
            f"INSERT INTO orphan_runs ({RUN}, current_item) "
            f"SELECT {RUN}, current_child FROM choice_runs WHERE kind = 'issue_story'"
        )
    if "choice_reviews" in tables:
        # An answer about an issue that is gone since cannot be kept: it names no issue.
        op.execute(
            f"INSERT INTO orphan_reviews ({REVIEW}, item_id, story_id) "
            f"SELECT {REVIEW}, child_id, parent_id FROM choice_reviews "
            "WHERE kind = 'issue_story' AND child_id IN (SELECT id FROM backlog_items)"
        )
        op.drop_table("choice_reviews")
    if "choice_runs" in tables:
        op.drop_table("choice_runs")
