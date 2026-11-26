"""Keep what each project's reviews are set to, with a limit in dollars from now on."""

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # Fresh installations may already have the current ORM schema.
    if "review_settings" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "review_settings",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("dollar_limit", sa.Float(), nullable=False),
            sa.Column("dollars_from", sa.DateTime(timezone=True)),
            sa.Column("answer_provider", sa.String(32)),
            sa.Column("answer_model", sa.String(200)),
            sa.Column("judge_provider", sa.String(32)),
            sa.Column("judge_model", sa.String(200)),
            sa.Column("makers", sa.JSON()),
            sa.Column("rounds", sa.Integer(), nullable=False),
        )
    # Every project there is gets the limit decided on 26 November 2026, counting from
    # now: what it spent before there was a limit is not held against it.
    table = sa.table(
        "review_settings",
        sa.column("id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("project_id", sa.Uuid()),
        sa.column("dollar_limit", sa.Float()),
        sa.column("dollars_from", sa.DateTime(timezone=True)),
        sa.column("rounds", sa.Integer()),
    )
    now = datetime.now(UTC)
    projects = sa.table("projects", sa.column("id", sa.Uuid()))
    have = set(bind.execute(sa.select(table.c.project_id)).scalars())
    for project in bind.execute(sa.select(projects.c.id)).scalars().all():
        if project not in have:
            bind.execute(
                table.insert().values(
                    id=uuid4(),
                    created_at=now,
                    updated_at=now,
                    project_id=project,
                    dollar_limit=3.0,
                    dollars_from=now,
                    rounds=0,
                )
            )


def downgrade():
    if "review_settings" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("review_settings")
