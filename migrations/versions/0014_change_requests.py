"""Add operator change requests.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = {
    "ix_change_requests_request_key": (["request_key"], True),
    "ix_change_requests_project": (["project"], False),
    "ix_change_requests_status": (["status"], False),
    "ix_change_requests_fingerprint": (["fingerprint"], False),
    "ix_change_requests_approval_binding_sha256": (["approval_binding_sha256"], False),
}


def _current_bootstrap_schema_present() -> bool:
    """Recognize revision 0001's historical create-all bootstrap safely."""
    inspector = sa.inspect(op.get_bind())
    if "change_requests" not in set(inspector.get_table_names()):
        return False
    required_columns = {
        "id",
        "request_key",
        "project",
        "status",
        "revision",
        "description_redacted",
        "description_sha256",
        "redacted",
        "revision_note",
        "proposal_json",
        "review_json",
        "dedupe_json",
        "fingerprint",
        "issue_title",
        "issue_body",
        "approval_binding_sha256",
        "target_repository",
        "issue_url",
        "issue_number",
        "error_code",
        "requested_by",
        "decided_by",
        "decision_reason",
        "decided_at",
        "published_at",
        "publish_attempted_at",
        "decision_sequence",
        "approval_snapshot_json",
        "created_at",
        "updated_at",
    }
    actual_columns = {str(column["name"]) for column in inspector.get_columns("change_requests")}
    actual_indexes = {
        str(index["name"])
        for index in inspector.get_indexes("change_requests")
        if index.get("name")
    }
    if not required_columns.issubset(actual_columns) or not set(_INDEXES).issubset(actual_indexes):
        raise RuntimeError("revision 0014 found a partial bootstrap schema")
    return True


def upgrade() -> None:
    if _current_bootstrap_schema_present():
        return
    op.create_table(
        "change_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_key", sa.String(40), nullable=False),
        sa.Column("project", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("description_redacted", sa.Text(), nullable=False),
        sa.Column("description_sha256", sa.String(64), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.Column("revision_note", sa.Text(), nullable=True),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("review_json", sa.JSON(), nullable=False),
        sa.Column("dedupe_json", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(80), nullable=True),
        sa.Column("issue_title", sa.String(240), nullable=True),
        sa.Column("issue_body", sa.Text(), nullable=True),
        sa.Column("approval_binding_sha256", sa.String(64), nullable=True),
        sa.Column("target_repository", sa.String(200), nullable=False),
        sa.Column("issue_url", sa.Text(), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("approval_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_change_requests_revision_positive"),
        sa.CheckConstraint(
            "status IN ('DRAFTING', 'CANDIDATE', 'APPROVED', 'PUBLISHED', 'REJECTED', 'FAILED')",
            name="ck_change_requests_status_known",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, (columns, unique) in _INDEXES.items():
        op.create_index(name, "change_requests", columns, unique=unique)


def downgrade() -> None:
    op.drop_table("change_requests")
