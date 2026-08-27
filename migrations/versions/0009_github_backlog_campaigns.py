"""Add durable paired GitHub backlog campaigns and issue records.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_backlog_campaigns",
        sa.Column("project", sa.String(length=32), nullable=False),
        sa.Column("source_repository", sa.String(length=200), nullable=False),
        sa.Column("deployment_repository", sa.String(length=200), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issue_query", sa.String(length=240), nullable=False),
        sa.Column("import_filter", sa.String(length=32), nullable=False),
        sa.Column("source_default_branch", sa.String(length=120), nullable=False),
        sa.Column("deployment_default_branch", sa.String(length=120), nullable=False),
        sa.Column("source_head_at_import", sa.String(length=40), nullable=False),
        sa.Column("deployment_head_at_import", sa.String(length=40), nullable=False),
        sa.Column("issue_count", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("deferred_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("stale_count", sa.Integer(), nullable=False),
        sa.Column("already_fixed_count", sa.Integer(), nullable=False),
        sa.Column("existing_pr_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("dependency_graph_hash", sa.String(length=64), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("approval_binding", sa.String(length=64)),
        sa.Column("approval_json", sa.JSON(), nullable=False),
        sa.Column("maximum_risk", sa.String(length=8)),
        sa.Column("maximum_items", sa.Integer(), nullable=False),
        sa.Column("maximum_concurrency", sa.Integer(), nullable=False),
        sa.Column("delivery_mode", sa.String(length=32)),
        sa.Column("include_enhancements", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=240), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    for column in (
        "project",
        "imported_at",
        "import_filter",
        "plan_hash",
        "approval_binding",
        "status",
        "workflow_id",
    ):
        op.create_index(
            f"ix_github_backlog_campaigns_{column}", "github_backlog_campaigns", [column]
        )
    op.create_table(
        "backlog_issue_records",
        sa.Column("backlog_campaign_id", sa.Uuid(), nullable=False),
        sa.Column("repository", sa.String(length=200), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("issue_url_reference", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("labels_json", sa.JSON(), nullable=False),
        sa.Column("milestone", sa.String(length=200)),
        sa.Column("issue_type", sa.String(length=64), nullable=False),
        sa.Column("openspec_kind", sa.String(length=80)),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("calculated_risk", sa.String(length=8), nullable=False),
        sa.Column("component", sa.String(length=200), nullable=False),
        sa.Column("source_or_deployment_owner", sa.String(length=32), nullable=False),
        sa.Column("documentation_levels_json", sa.JSON(), nullable=False),
        sa.Column("dependencies_json", sa.JSON(), nullable=False),
        sa.Column("blocking_issues_json", sa.JSON(), nullable=False),
        sa.Column("issues_blocked_json", sa.JSON(), nullable=False),
        sa.Column("linked_prs_json", sa.JSON(), nullable=False),
        sa.Column("linked_branches_json", sa.JSON(), nullable=False),
        sa.Column("linked_commits_json", sa.JSON(), nullable=False),
        sa.Column("duplicate_candidates_json", sa.JSON(), nullable=False),
        sa.Column("current_source_reproduction", sa.String(length=80), nullable=False),
        sa.Column("current_runtime_reproduction", sa.String(length=80), nullable=False),
        sa.Column("current_runtime_required", sa.Boolean(), nullable=False),
        sa.Column("reproduction_mode", sa.String(length=64), nullable=False),
        sa.Column("candidate_status", sa.String(length=80), nullable=False),
        sa.Column("eligibility_status", sa.String(length=80), nullable=False),
        sa.Column("eligibility_reason", sa.String(length=120), nullable=False),
        sa.Column("planned_wave", sa.String(length=8), nullable=False),
        sa.Column("planned_order", sa.Integer(), nullable=False),
        sa.Column("finding_id", sa.Uuid()),
        sa.Column("change_set_id", sa.Uuid()),
        sa.Column("delivery_status", sa.String(length=80), nullable=False),
        sa.Column("final_closure_status", sa.String(length=80), nullable=False),
        sa.Column("documentation_impact", sa.String(length=80), nullable=False),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("github_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("github_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["backlog_campaign_id"], ["github_backlog_campaigns.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"]),
        sa.ForeignKeyConstraint(["change_set_id"], ["change_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "backlog_campaign_id",
            "repository",
            "issue_number",
            name="uq_backlog_campaign_repository_issue",
        ),
    )
    for column in (
        "backlog_campaign_id",
        "repository",
        "state",
        "issue_type",
        "severity",
        "calculated_risk",
        "component",
        "candidate_status",
        "eligibility_status",
        "planned_wave",
        "planned_order",
        "delivery_status",
        "final_closure_status",
    ):
        op.create_index(f"ix_backlog_issue_records_{column}", "backlog_issue_records", [column])
    op.create_index(
        "ix_backlog_issue_campaign_wave_order",
        "backlog_issue_records",
        ["backlog_campaign_id", "planned_wave", "planned_order"],
    )


def downgrade() -> None:
    op.drop_table("backlog_issue_records")
    op.drop_table("github_backlog_campaigns")
