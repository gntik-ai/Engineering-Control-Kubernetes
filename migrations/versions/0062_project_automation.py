"""Scope automation libraries and pin operation assignments without changing existing runs."""

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade():
    db = op.get_bind()
    schema = sa.inspect(db)
    tables = set(schema.get_table_names())

    def project():
        return sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        )

    if "project_flows" not in tables:
        op.create_table(
            "project_flows",
            sa.Column("flow_key", sa.String(64), primary_key=True),
            project(),
            sa.Column("local_key", sa.String(64), nullable=False),
            sa.UniqueConstraint("project_id", "local_key"),
        )
    if "operation_definitions" not in tables:
        op.create_table(
            "operation_definitions",
            sa.Column("key", sa.String(40), primary_key=True),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("guide", sa.JSON(), nullable=False),
        )
    if "command_profiles" not in tables:
        op.create_table(
            "command_profiles",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE")),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("commands", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "name"),
        )
        op.create_index(
            "uq_global_command_profile",
            "command_profiles",
            ["name"],
            unique=True,
            sqlite_where=sa.text("project_id IS NULL"),
            postgresql_where=sa.text("project_id IS NULL"),
        )
    if "project_command_selections" not in tables:
        op.create_table(
            "project_command_selections",
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "profile_id",
                sa.Uuid(),
                sa.ForeignKey("command_profiles.id", ondelete="RESTRICT"),
                nullable=False,
            ),
        )
    if "flow_version" not in {c["name"] for c in schema.get_columns("project_sections")}:
        op.add_column("project_sections", sa.Column("flow_version", sa.Integer()))
        op.add_column("project_sections", sa.Column("operation_key", sa.String(40)))
    op.execute(
        "UPDATE project_sections SET operation_key = key "
        "WHERE kind = 'core' AND operation_key IS NULL"
    )
    if "project_id" not in {c["name"] for c in schema.get_columns("agents")}:
        old = next(
            c for c in schema.get_unique_constraints("agents") if c["column_names"] == ["name"]
        )
        with op.batch_alter_table(
            "agents", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}
        ) as batch:
            batch.drop_constraint(old["name"] or "uq_agents_name", type_="unique")
            batch.add_column(sa.Column("project_id", sa.Uuid()))
            batch.create_foreign_key(
                "fk_agents_owner", "projects", ["project_id"], ["id"], ondelete="CASCADE"
            )
            batch.create_unique_constraint("uq_agents_owner_name", ["project_id", "name"])
        op.create_index(
            "uq_global_agent_name",
            "agents",
            ["name"],
            unique=True,
            sqlite_where=sa.text("project_id IS NULL"),
            postgresql_where=sa.text("project_id IS NULL"),
        )


def downgrade():
    db = op.get_bind()
    for table, column in (
        ("project_flows", "flow_key"),
        ("command_profiles", "id"),
        ("operation_definitions", "key"),
        ("agents", "project_id"),
        ("project_sections", "flow_version"),
    ):
        records = sa.table(table, sa.column(column))
        if db.scalar(
            sa.select(sa.literal(1))
            .select_from(records)
            .where(records.c[column].is_not(None))
            .limit(1)
        ):
            raise RuntimeError("Export or reconcile scoped automation before downgrading.")
    unique = next(
        c
        for c in sa.inspect(db).get_unique_constraints("agents")
        if c["column_names"] == ["project_id", "name"]
    )
    fk = next(
        c
        for c in sa.inspect(db).get_foreign_keys("agents")
        if c["constrained_columns"] == ["project_id"]
    )
    op.drop_index("uq_global_agent_name", table_name="agents")
    with op.batch_alter_table(
        "agents",
        naming_convention={
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s",
        },
    ) as batch:
        batch.drop_constraint(unique["name"] or "uq_agents_project_id", type_="unique")
        batch.drop_constraint(fk["name"] or "fk_agents_project_id", type_="foreignkey")
        batch.drop_column("project_id")
        batch.create_unique_constraint("uq_agents_name", ["name"])
    op.drop_column("project_sections", "flow_version")
    op.drop_column("project_sections", "operation_key")
    for table in (
        "project_command_selections",
        "command_profiles",
        "operation_definitions",
        "project_flows",
    ):
        op.drop_table(table)
