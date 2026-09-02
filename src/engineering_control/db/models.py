from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from engineering_control.db.base import Base, Timestamps, UUIDPrimaryKey


class Campaign(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "campaigns"
    project: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(64), index=True)
    source_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    deployment_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    required_cells: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CoverageCell(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "coverage_cells"
    campaign_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    repository_surface: Mapped[str] = mapped_column(String(32), default="source")
    domain: Mapped[str] = mapped_column(String(120))
    persona: Mapped[str] = mapped_column(String(120))
    flow: Mapped[str] = mapped_column(String(120), default="repository-static")
    lens: Mapped[str] = mapped_column(String(64))
    environment: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(64), index=True)
    blocker: Mapped[str | None] = mapped_column(Text)
    exclusion_approval_id: Mapped[UUID | None] = mapped_column(Uuid)
    evidence_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "repository_surface",
            "domain",
            "persona",
            "flow",
            "lens",
            "environment",
            name="uq_coverage_cell",
        ),
    )


class Finding(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "findings"
    campaign_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("campaigns.id"), index=True)
    coverage_cell_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("coverage_cells.id"))
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    project: Mapped[str] = mapped_column(String(32), index=True)
    domain: Mapped[str] = mapped_column(String(120))
    persona: Mapped[str] = mapped_column(String(120))
    severity: Mapped[str] = mapped_column(String(32), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    risk_class: Mapped[str] = mapped_column(String(8), index=True)
    disposition: Mapped[str] = mapped_column(String(32), index=True)
    expected_behavior: Mapped[str] = mapped_column(Text)
    actual_behavior: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    minimal_reproduction: Mapped[list[Any]] = mapped_column(JSON)
    affected_source_sha: Mapped[str] = mapped_column(String(40))
    affected_deployment_sha: Mapped[str | None] = mapped_column(String(40))
    affected_image_digest: Mapped[str | None] = mapped_column(String(80))
    affected_component: Mapped[str] = mapped_column(String(200))
    impact_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recommended_action: Mapped[str] = mapped_column(Text)


class FindingVerification(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "finding_verifications"
    finding_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("findings.id"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("runs.id", use_alter=True))
    verifier_provider: Mapped[str] = mapped_column(String(32))
    disposition: Mapped[str] = mapped_column(String(32), index=True)
    reproduction_count: Mapped[int] = mapped_column(Integer)
    positive_control: Mapped[bool] = mapped_column(Boolean)
    negative_control: Mapped[bool] = mapped_column(Boolean)
    duplicate_issue_url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)


class Issue(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "issues"
    finding_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("findings.id"), unique=True)
    repository: Mapped[str] = mapped_column(String(200))
    number: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), index=True)
    openspec_change: Mapped[str | None] = mapped_column(String(240))


class ChangeSet(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "change_sets"
    finding_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("findings.id"), index=True)
    issue_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("issues.id"))
    project: Mapped[str] = mapped_column(String(32), index=True)
    risk_class: Mapped[str] = mapped_column(String(8), index=True)
    state: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_repository: Mapped[str] = mapped_column(String(200))
    source_branch: Mapped[str | None] = mapped_column(String(240))
    source_base_sha: Mapped[str] = mapped_column(String(40))
    source_head_sha: Mapped[str | None] = mapped_column(String(40))
    source_pr_url: Mapped[str | None] = mapped_column(Text)
    deployment_repository: Mapped[str] = mapped_column(String(200))
    deployment_branch: Mapped[str | None] = mapped_column(String(240))
    deployment_base_sha: Mapped[str] = mapped_column(String(40))
    deployment_head_sha: Mapped[str | None] = mapped_column(String(40))
    deployment_pr_url: Mapped[str | None] = mapped_column(Text)
    image_digests: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    chart_version: Mapped[str | None] = mapped_column(String(80))
    recovery_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Run(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "runs"
    campaign_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("campaigns.id"), index=True)
    change_set_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("change_sets.id"), index=True
    )
    workflow_id: Mapped[str] = mapped_column(String(240), index=True)
    run_type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(64), index=True)
    project: Mapped[str] = mapped_column(String(32), index=True)
    worker: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(120))
    summary: Mapped[str | None] = mapped_column(Text)


class Step(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "steps"
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    ordinal: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(240), unique=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentInvocation(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "agent_invocations"
    run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("runs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(80), index=True)
    model_class: Mapped[str] = mapped_column(String(32))
    agent_pack_revision: Mapped[str | None] = mapped_column(String(40))
    instruction_bundle_sha256: Mapped[str | None] = mapped_column(String(64))
    instruction_sources_json: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    allowed_tools_json: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    provider_session_id: Mapped[str | None] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    structured_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentExecution(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "agent_executions"
    workflow_id: Mapped[str] = mapped_column(String(240), index=True)
    temporal_run_id: Mapped[str | None] = mapped_column(String(240))
    activity_id: Mapped[str | None] = mapped_column(String(240))
    project: Mapped[str] = mapped_column(String(32), index=True)
    repository_full_name: Mapped[str | None] = mapped_column(String(200))
    issue_number: Mapped[int | None] = mapped_column(Integer)
    campaign_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("campaigns.id"))
    change_set_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("change_sets.id"))
    provider: Mapped[str] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(80), index=True)
    host: Mapped[str | None] = mapped_column(String(253))
    worker_kind: Mapped[str | None] = mapped_column(String(32), index=True)
    task_queue: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(64), index=True)
    phase: Mapped[str | None] = mapped_column(String(120), index=True)
    selection_reason: Mapped[str | None] = mapped_column(String(500))
    cli_path_identifier: Mapped[str | None] = mapped_column(String(80))
    cli_version: Mapped[str | None] = mapped_column(String(120))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    blocker_code: Mapped[str | None] = mapped_column(String(120))
    error_summary: Mapped[str | None] = mapped_column(Text)
    output_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    transcript_incomplete: Mapped[bool] = mapped_column(Boolean, default=False)
    total_log_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    redaction_count: Mapped[int] = mapped_column(Integer, default=0)


class AgentLogChunk(Base):
    __tablename__ = "agent_log_chunks"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    execution_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_executions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    stream: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    redacted_text: Mapped[str] = mapped_column(Text)
    byte_count: Mapped[int] = mapped_column(Integer)
    redaction_count: Mapped[int] = mapped_column(Integer, default=0)
    is_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="uq_agent_log_execution_sequence"),
    )


class WorktreeLease(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "worktree_leases"
    change_set_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("change_sets.id"), index=True)
    repository: Mapped[str] = mapped_column(String(200))
    worktree_path: Mapped[str] = mapped_column(Text, unique=True)
    branch: Mapped[str] = mapped_column(String(240))
    owner_run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("runs.id"))
    base_sha: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("change_set_id", "repository", name="uq_worktree_changeset_repository"),
        UniqueConstraint("repository", "branch", name="uq_worktree_repository_branch"),
    )


class ComponentLease(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "component_leases"
    component_key: Mapped[str] = mapped_column(String(240), index=True)
    owner_run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("runs.id"))
    mode: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderUsage(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "provider_usage"
    provider: Mapped[str] = mapped_column(String(32), index=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(80))
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(80))


class QuotaSnapshot(UUIDPrimaryKey, Base):
    __tablename__ = "quota_snapshots"
    provider: Mapped[str] = mapped_column(String(32), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    remaining_capacity: Mapped[float | None] = mapped_column(Float)
    total_capacity: Mapped[float | None] = mapped_column(Float)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    running_jobs: Mapped[int] = mapped_column(Integer)
    rate_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    authenticated: Mapped[bool | None] = mapped_column(Boolean)
    cli_present: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str | None] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(80))


class Artifact(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "artifacts"
    project: Mapped[str] = mapped_column(String(32), index=True)
    campaign_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("campaigns.id"), index=True)
    change_set_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("change_sets.id"), index=True
    )
    run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("runs.id"), index=True)
    relative_path: Mapped[str] = mapped_column(Text, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    content_type: Mapped[str] = mapped_column(String(160))
    producer: Mapped[str] = mapped_column(String(120))
    redaction_status: Mapped[str] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    retention_class: Mapped[str] = mapped_column(String(64), default="standard")
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_reason: Mapped[str | None] = mapped_column(String(120))


class Approval(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "approvals"
    change_set_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("change_sets.id"), index=True)
    approval_type: Mapped[str] = mapped_column(String(80))
    decision: Mapped[str] = mapped_column(String(16))
    artifact_binding_sha256: Mapped[str] = mapped_column(String(64), index=True)
    approver_subject: Mapped[str] = mapped_column(String(240))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ApprovalCandidate(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "approval_candidates"
    change_set_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("change_sets.id"), index=True)
    workflow_id: Mapped[str] = mapped_column(String(240), index=True)
    risk_class: Mapped[str] = mapped_column(String(8))
    artifact_binding_sha256: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Deployment(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "deployments"
    change_set_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("change_sets.id"), index=True)
    project: Mapped[str] = mapped_column(String(32), index=True)
    environment: Mapped[str] = mapped_column(String(80), index=True)
    strategy: Mapped[str] = mapped_column(String(32))
    target_context: Mapped[str] = mapped_column(String(160))
    target_namespace: Mapped[str] = mapped_column(String(160))
    desired_sha: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(64), index=True)
    transaction_id: Mapped[str | None] = mapped_column(String(240))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class VerificationResult(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "verification_results"
    change_set_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("change_sets.id"), index=True)
    deployment_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("deployments.id"))
    run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("runs.id"))
    verification_type: Mapped[str] = mapped_column(String(80))
    verifier_provider: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(64), index=True)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Incident(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "incidents"
    change_set_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("change_sets.id"), index=True
    )
    deployment_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("deployments.id"))
    project: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text)
    recovery_state: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationOutbox(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "notification_outbox"
    topic: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="PENDING")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(120))


class WorkerHeartbeat(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "worker_heartbeats"
    identity: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(253))
    worker_kind: Mapped[str] = mapped_column(String(32), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ProjectSnapshot(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "project_snapshots"
    campaign_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("campaigns.id"), index=True)
    project: Mapped[str] = mapped_column(String(32), index=True)
    source_sha: Mapped[str] = mapped_column(String(40), index=True)
    deployment_sha: Mapped[str] = mapped_column(String(40), index=True)
    live_revision: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class SolutionLoop(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "solution_loops"
    workflow_id: Mapped[str] = mapped_column(String(240), unique=True, index=True)
    project: Mapped[str] = mapped_column(String(32), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(240), index=True)
    execution_mode: Mapped[str] = mapped_column(String(32))
    maximum_risk: Mapped[str] = mapped_column(String(8))
    maximum_items: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(64), index=True)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Enhancement(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "enhancements"
    enhancement_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    project: Mapped[str] = mapped_column(String(32), index=True)
    campaign_id: Mapped[str] = mapped_column(String(240), index=True)
    persona: Mapped[str] = mapped_column(String(120))
    affected_repository: Mapped[str] = mapped_column(String(200))
    risk_class: Mapped[str] = mapped_column(String(8), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    approval_binding_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class GitHubBacklogCampaign(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "github_backlog_campaigns"
    project: Mapped[str] = mapped_column(String(32), index=True)
    source_repository: Mapped[str] = mapped_column(String(200))
    deployment_repository: Mapped[str] = mapped_column(String(200))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    issue_query: Mapped[str] = mapped_column(String(240))
    import_filter: Mapped[str] = mapped_column(String(32), index=True)
    source_default_branch: Mapped[str] = mapped_column(String(120))
    deployment_default_branch: Mapped[str] = mapped_column(String(120))
    source_head_at_import: Mapped[str] = mapped_column(String(40))
    deployment_head_at_import: Mapped[str] = mapped_column(String(40))
    issue_count: Mapped[int] = mapped_column(Integer)
    eligible_count: Mapped[int] = mapped_column(Integer)
    deferred_count: Mapped[int] = mapped_column(Integer)
    duplicate_count: Mapped[int] = mapped_column(Integer)
    stale_count: Mapped[int] = mapped_column(Integer)
    already_fixed_count: Mapped[int] = mapped_column(Integer)
    existing_pr_count: Mapped[int] = mapped_column(Integer)
    blocked_count: Mapped[int] = mapped_column(Integer)
    dependency_graph_hash: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str] = mapped_column(String(64), index=True)
    approval_binding: Mapped[str | None] = mapped_column(String(64), index=True)
    approval_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    maximum_risk: Mapped[str | None] = mapped_column(String(8))
    maximum_items: Mapped[int] = mapped_column(Integer)
    maximum_concurrency: Mapped[int] = mapped_column(Integer)
    delivery_mode: Mapped[str | None] = mapped_column(String(32))
    include_enhancements: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(64), index=True)
    workflow_id: Mapped[str] = mapped_column(String(240), unique=True, index=True)


class BacklogIssueRecord(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "backlog_issue_records"
    backlog_campaign_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("github_backlog_campaigns.id", ondelete="CASCADE"), index=True
    )
    repository: Mapped[str] = mapped_column(String(200), index=True)
    issue_number: Mapped[int] = mapped_column(Integer)
    issue_url_reference: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300))
    state: Mapped[str] = mapped_column(String(32), index=True)
    labels_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    milestone: Mapped[str | None] = mapped_column(String(200))
    issue_type: Mapped[str] = mapped_column(String(64), index=True)
    openspec_kind: Mapped[str | None] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(32), index=True)
    calculated_risk: Mapped[str] = mapped_column(String(8), index=True)
    component: Mapped[str] = mapped_column(String(200), index=True)
    source_or_deployment_owner: Mapped[str] = mapped_column(String(32))
    documentation_levels_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    dependencies_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    blocking_issues_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    issues_blocked_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    linked_prs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    linked_branches_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    linked_commits_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    duplicate_candidates_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    current_source_reproduction: Mapped[str] = mapped_column(String(80))
    current_runtime_reproduction: Mapped[str] = mapped_column(String(80))
    current_runtime_required: Mapped[bool] = mapped_column(Boolean)
    reproduction_mode: Mapped[str] = mapped_column(String(64))
    candidate_status: Mapped[str] = mapped_column(String(80), index=True)
    eligibility_status: Mapped[str] = mapped_column(String(80), index=True)
    eligibility_reason: Mapped[str] = mapped_column(String(120))
    planned_wave: Mapped[str] = mapped_column(String(8), index=True)
    planned_order: Mapped[int] = mapped_column(Integer, index=True)
    finding_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("findings.id"))
    change_set_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("change_sets.id"))
    delivery_status: Mapped[str] = mapped_column(String(80), index=True)
    final_closure_status: Mapped[str] = mapped_column(String(80), index=True)
    documentation_impact: Mapped[str] = mapped_column(String(80))
    body_sha256: Mapped[str] = mapped_column(String(64))
    github_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    github_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint(
            "backlog_campaign_id",
            "repository",
            "issue_number",
            name="uq_backlog_campaign_repository_issue",
        ),
    )


class DashboardAuthentication(Base):
    __tablename__ = "dashboard_authentication"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    password_hash: Mapped[str] = mapped_column(Text)
    password_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)


class DashboardSessionRevocation(Base):
    __tablename__ = "dashboard_session_revocations"
    session_binding_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_version: Mapped[int] = mapped_column(Integer)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DashboardEvent(Base):
    __tablename__ = "dashboard_events"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    host: Mapped[str | None] = mapped_column(String(253))
    worker_kind: Mapped[str | None] = mapped_column(String(32), index=True)
    project: Mapped[str | None] = mapped_column(String(32), index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(240), index=True)
    temporal_run_id: Mapped[str | None] = mapped_column(String(240))
    issue_number: Mapped[int | None] = mapped_column(Integer)
    campaign_id: Mapped[str | None] = mapped_column(String(240), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(240))
    current_phase: Mapped[str | None] = mapped_column(String(120))
    message: Mapped[str] = mapped_column(String(500))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ActionCandidate(UUIDPrimaryKey, Base):
    __tablename__ = "action_candidates"
    action: Mapped[str] = mapped_column(String(80), index=True)
    project: Mapped[str | None] = mapped_column(String(32), index=True)
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(240), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    capability_status: Mapped[str] = mapped_column(String(32), index=True)
    blockers_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    binding_hash: Mapped[str] = mapped_column(String(64), index=True)
    confirmation_phrase: Mapped[str | None] = mapped_column(String(120))
    session_binding_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "binding_hash",
            "session_binding_hash",
            name="uq_action_candidate_binding_session",
        ),
    )


class ReauthenticationBinding(UUIDPrimaryKey, Base):
    __tablename__ = "reauthentication_bindings"
    candidate_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("action_candidates.id", ondelete="CASCADE"), unique=True, index=True
    )
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    session_binding_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationRequest(UUIDPrimaryKey, Base):
    __tablename__ = "operation_requests"
    action: Mapped[str] = mapped_column(String(80), index=True)
    project: Mapped[str | None] = mapped_column(String(32), index=True)
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(240), index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_channel: Mapped[str] = mapped_column(String(16), index=True)
    operator_class: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    binding_hash: Mapped[str] = mapped_column(String(64), index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(240), index=True)
    temporal_run_id: Mapped[str | None] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(32), index=True)
    safe_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationExport(UUIDPrimaryKey, Base):
    __tablename__ = "operation_exports"
    operation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("operation_requests.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    export_kind: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String(240))
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ConnectionSnapshot(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "connection_snapshots"
    component: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    logical_target: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(32), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    safe_error: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(80))
    next_action: Mapped[str | None] = mapped_column(String(500))
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


Index("ix_runs_active", Run.status, Run.project)
Index("ix_change_sets_project_state", ChangeSet.project, ChangeSet.state)
Index("ix_findings_project_disposition", Finding.project, Finding.disposition)
Index("ix_enhancements_project_status", Enhancement.project, Enhancement.status)
Index(
    "ix_backlog_issue_campaign_wave_order",
    BacklogIssueRecord.backlog_campaign_id,
    BacklogIssueRecord.planned_wave,
    BacklogIssueRecord.planned_order,
)
Index(
    "uq_component_leases_active_key",
    ComponentLease.component_key,
    unique=True,
    postgresql_where=ComponentLease.released_at.is_(None),
    sqlite_where=ComponentLease.released_at.is_(None),
)
Index(
    "uq_approval_candidates_active_change_set",
    ApprovalCandidate.change_set_id,
    unique=True,
    postgresql_where=ApprovalCandidate.invalidated_at.is_(None),
    sqlite_where=ApprovalCandidate.invalidated_at.is_(None),
)
Index("ix_dashboard_events_stream", DashboardEvent.id, DashboardEvent.occurred_at)
Index("ix_agent_executions_project_status", AgentExecution.project, AgentExecution.status)
Index("ix_agent_executions_provider_started", AgentExecution.provider, AgentExecution.started_at)
Index("ix_agent_executions_last_heartbeat", AgentExecution.last_heartbeat_at)
Index("ix_agent_log_chunks_execution_sequence", AgentLogChunk.execution_id, AgentLogChunk.sequence)
Index(
    "ix_operation_requests_target_status",
    OperationRequest.action,
    OperationRequest.target_id,
    OperationRequest.status,
)
Index(
    "uq_operation_requests_active_target",
    OperationRequest.action,
    OperationRequest.target_id,
    unique=True,
    postgresql_where=OperationRequest.status.in_(("PENDING", "DISPATCHING", "STARTED", "RUNNING")),
    sqlite_where=OperationRequest.status.in_(("PENDING", "DISPATCHING", "STARTED", "RUNNING")),
)
