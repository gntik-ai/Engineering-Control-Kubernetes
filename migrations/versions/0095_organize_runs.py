"""Keep the runs of Organize and of Coverage, and what each found, in three lists."""

import sqlalchemy as sa
from alembic import op

revision = "0095"
down_revision = "0094"
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


def upgrade():
    # Fresh installations may already have the current ORM schema.
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "organize_runs" not in tables:
        op.create_table(
            "organize_runs",
            *_stamps(),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("stage", sa.String(8), nullable=False),
            sa.Column("counts", sa.JSON(), nullable=False),
            sa.Column("estimate", sa.JSON(), nullable=False),
            sa.Column("dollar_cap", sa.Float()),
            sa.Column("spent", sa.BigInteger(), nullable=False),
            sa.Column("cost", sa.Float(), nullable=False),
            sa.Column("detail", sa.Text(), nullable=False),
            sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("ended_at", sa.DateTime(timezone=True)),
            sa.Column("undone_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_organize_runs_project_id", "organize_runs", ["project_id"])
        op.create_index("ix_organize_runs_state", "organize_runs", ["state"])
    if "organize_lines" not in tables:
        op.create_table(
            "organize_lines",
            *_stamps(),
            sa.Column(
                "run_id",
                sa.Uuid(),
                sa.ForeignKey("organize_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("listed", sa.String(12), nullable=False),
            sa.Column("case", sa.String(24), nullable=False),
            sa.Column("subject_kind", sa.String(12), nullable=False),
            sa.Column("subject_id", sa.Uuid()),
            sa.Column("subject_ref", sa.String(900)),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("candidates", sa.JSON(), nullable=False),
            sa.Column("said", sa.JSON(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("doubt", sa.Float(), nullable=False),
            sa.Column("action", sa.JSON()),
            sa.Column("before", sa.JSON()),
            sa.Column("made_kind", sa.String(12)),
            sa.Column("made_id", sa.Uuid()),
            sa.Column("sources", sa.JSON()),
            sa.Column("state", sa.String(12), nullable=False),
            sa.Column("answer", sa.String(24)),
            sa.Column("answered_at", sa.DateTime(timezone=True)),
            sa.Column("undone_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_organize_lines_project_id", "organize_lines", ["project_id"])
        op.create_index("ix_organize_lines_run_id", "organize_lines", ["run_id"])
        op.create_index("ix_organize_lines_listed", "organize_lines", ["listed"])
        op.create_index("ix_organize_lines_state", "organize_lines", ["state"])


def downgrade():
    tables = sa.inspect(op.get_bind()).get_table_names()
    for name in ("organize_lines", "organize_runs"):
        if name in tables:
            op.drop_table(name)
