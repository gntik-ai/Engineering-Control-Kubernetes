"""Project connections, evidence and administrative decisions.

Revision ID: 0055
Revises: 0054
"""

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def uid(name, target=None, *, nullable=False):
    args = [sa.ForeignKey(target)] if target else []
    return sa.Column(name, sa.Uuid(), *args, nullable=nullable)


def times():
    return [
        sa.Column(name, sa.DateTime(timezone=True), nullable=False)
        for name in ("created_at", "updated_at")
    ]


def upgrade():
    db = sa.inspect(op.get_bind())
    if "origin" not in {c["name"] for c in db.get_columns("workspace_candidates")}:
        op.add_column(
            "workspace_candidates",
            sa.Column("origin", sa.String(16), nullable=False, server_default="import"),
        )
    if not db.has_table("project_connections"):
        op.create_table(
            "project_connections",
            sa.Column("id", sa.Uuid(), primary_key=True),
            uid("first_id", "projects.id"),
            uid("second_id", "projects.id"),
            uid("owner_id", "projects.id"),
            uid("dependent_id", "projects.id", nullable=True),
            sa.Column("purpose", sa.Text, nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("provider", sa.String(16)),
            sa.Column("model", sa.String(200)),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
            uid("lease", nullable=True),
            sa.Column("last_checked", sa.DateTime(timezone=True)),
            sa.Column("detail", sa.String(400)),
            sa.Column("progress", sa.JSON, nullable=False),
            *times(),
            sa.UniqueConstraint("first_id", "second_id", name="uq_connection_pair"),
            sa.CheckConstraint("first_id < second_id", name="connection_order"),
            sa.CheckConstraint("owner_id IN (first_id, second_id)", name="connection_owner"),
            sa.CheckConstraint(
                "dependent_id IN (first_id, second_id)", name="connection_direction"
            ),
        )
        for field in ("first_id", "second_id"):
            op.create_index("ix_project_connections_" + field, "project_connections", [field])
        op.create_index("ix_connection_due", "project_connections", ["archived_at", "due_at"])
    if not db.has_table("connection_findings"):
        op.create_table(
            "connection_findings",
            sa.Column("id", sa.Uuid(), primary_key=True),
            uid("connection_id", "project_connections.id"),
            sa.Column("identity", sa.String(64), nullable=False),
            uid("source_project", "projects.id"),
            sa.Column("source_kind", sa.String(16), nullable=False),
            uid("source_id", nullable=True),
            sa.Column("source_number", sa.Integer),
            sa.Column("target_kind", sa.String(16)),
            uid("target_id", nullable=True),
            sa.Column("target_number", sa.Integer),
            sa.Column("action", sa.String(12), nullable=False),
            sa.Column("origin", sa.String(12), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("evidence", sa.JSON, nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            uid("proposal_id", "workspace_candidates.id", nullable=True),
            *times(),
            sa.UniqueConstraint("connection_id", "identity", name="uq_connection_finding"),
        )
        op.create_index(
            "ix_connection_findings_connection_id", "connection_findings", ["connection_id"]
        )
        op.create_index(
            "ix_connection_source",
            "connection_findings",
            ["source_project", "source_kind", "source_id"],
        )
        op.create_index("ix_connection_target", "connection_findings", ["target_kind", "target_id"])
    if not db.has_table("connection_events"):
        op.create_table(
            "connection_events",
            sa.Column("id", sa.Uuid(), primary_key=True),
            uid("connection_id", "project_connections.id"),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("message", sa.Text, nullable=False),
        )
        op.create_index(
            "ix_connection_events_connection_id", "connection_events", ["connection_id"]
        )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT 1 FROM project_connections LIMIT 1")):
        raise RuntimeError("revision 0055 would discard project connections and review history")
    for name in ("connection_events", "connection_findings", "project_connections"):
        op.drop_table(name)
    op.drop_column("workspace_candidates", "origin")
