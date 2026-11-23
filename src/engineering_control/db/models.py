from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

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
    # Trusted Temporal start/registration binding only; no historical backfill.
    temporal_first_execution_run_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
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


class CleanupRunItem(UUIDPrimaryKey, Timestamps, Base):
    """NEW-245: one stored chunk of a cleanup run's candidate or outcome list.

    The CleanupWorkflow carries only a reference to these rows, never the lists, so no
    Temporal payload grows with the retention backlog. They are deleted at finalization.
    """

    __tablename__ = "cleanup_run_items"
    run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    list_name: Mapped[str] = mapped_column(String(80))
    ordinal: Mapped[int] = mapped_column(Integer)
    items_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    __table_args__ = (
        UniqueConstraint("run_id", "list_name", "ordinal", name="uq_cleanup_run_items_chunk"),
    )


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


class ProjectRecord(UUIDPrimaryKey, Timestamps, Base):
    """A project the administrator sees and manages (migration 0025).

    ``origin`` says where it comes from: ``registry`` for the projects the workflows know
    by name (``engineering_control.projects``), ``console`` for one created in the console.
    Removing a project archives it; its name stays taken so old records keep their meaning.
    """

    __tablename__ = "projects"
    name: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    architecture: Mapped[str | None] = mapped_column(Text)
    planning_provider: Mapped[str | None] = mapped_column(String(16))
    planning_model: Mapped[str | None] = mapped_column(String(200))
    planning_reviews_paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_repository: Mapped[str] = mapped_column(String(140))
    deployment_repository: Mapped[str] = mapped_column(String(140))
    default_branch: Mapped[str] = mapped_column(String(100), default="main")
    origin: Mapped[str] = mapped_column(String(16))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the project's work moved from the original system to this platform, and what
    # was brought along from there (migration 0034). Not set: the original still works on it.
    cut_over_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    brought_over: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Set while no work may start on the project (migration 0035): since when, why, and
    # whether the administrator or the supervisor said so. What runs, runs to its end.
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(String(300))
    paused_by: Mapped[str | None] = mapped_column(String(16))
    # Where the project's application runs, for flows that look at it in a browser
    # (migration 0037). Not set: such flows cannot run for the project.
    application_url: Mapped[str | None] = mapped_column(String(300))
    # The other hosts the application uses (its API, where it signs in): what a step that
    # looks at it may reach beside the address's own host (migration 0038).
    application_hosts: Mapped[list[str] | None] = mapped_column(JSON)
    # Since when a test identity for that application is stored (migration 0038). The
    # identity itself is a Secret of the sandbox namespace, never a record.
    application_identity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The project's own commands, as lines: ``install`` and ``tests`` (migration 0041).
    # Not set: the commands of the platform's own file for the project.
    toolchain: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("origin IN ('registry', 'console')", name="origin"),)


class ProjectSettings(Base):
    """The global setting shared by all project workspaces."""

    __tablename__ = "project_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    default_project_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL")
    )
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)


class WorkspaceImport(Timestamps, Base):
    """One resumable analysis of the connected repository per project."""

    __tablename__ = "workspace_imports"
    review_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[str] = mapped_column(String(24), default="inventory")
    issues_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=0)
    repository: Mapped[str] = mapped_column(String(140))
    branch: Mapped[str] = mapped_column(String(100))
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    draft: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(200))
    lease: Mapped[UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceSource(Base):
    """Repository evidence retained for subsequent human-reviewed stages."""

    __tablename__ = "workspace_sources"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspace_imports.project_id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)


class WorkspaceCandidate(UUIDPrimaryKey, Timestamps, Base):
    """A proposal from a retained issue or a gap found in the project evidence."""

    __tablename__ = "workspace_candidates"
    review_run_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    identity: Mapped[str] = mapped_column(String(96))
    review_reason: Mapped[str | None] = mapped_column(Text)
    execution_type: Mapped[str | None] = mapped_column(String(16))
    origin: Mapped[str] = mapped_column(String(16), default="import", server_default="import")
    number: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    parents: Mapped[list[str]] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(16), default="pending")
    local_id: Mapped[UUID | None] = mapped_column(Uuid)
    __table_args__ = (
        UniqueConstraint("project_id", "kind", "identity", name="uq_workspace_candidate_identity"),
        UniqueConstraint("project_id", "number", name="uq_workspace_candidate_issue"),
    )


class PlanningReview(Timestamps, Base):
    """A resumable review of one new workspace record, independent of the import wizard."""

    __tablename__ = "planning_reviews"
    review_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    record_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    scope: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    lease: Mapped[UUID | None] = mapped_column(Uuid)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_planning_reviews_due", "status", "due_at"),)


class WorkspaceAction(UUIDPrimaryKey, Timestamps, Base):
    """An approved GitHub change, durably recorded before an external write."""

    __tablename__ = "workspace_actions"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    confirmation_token: Mapped[str | None] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(16))
    local_id: Mapped[UUID] = mapped_column(Uuid)
    repository: Mapped[str] = mapped_column(String(140))
    number: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    lease: Mapped[UUID | None] = mapped_column(Uuid)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(String(350))
    __table_args__ = (
        UniqueConstraint("project_id", "operation", "local_id", name="uq_workspace_action_record"),
    )


class PlanningLifecycleReceipt(Base):
    """The exact planning impact confirmed by an administrator, also used for replay."""

    __tablename__ = "planning_lifecycle_receipts"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    record_id: Mapped[UUID] = mapped_column(Uuid)
    operation: Mapped[str] = mapped_column(String(12))
    impact: Mapped[dict[str, Any]] = mapped_column(JSON)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorkspaceModelUsage(UUIDPrimaryKey, Base):
    """One metered response from repository analysis or planning, including its price snapshot."""

    __tablename__ = "workspace_model_usage"
    review_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("projects.id", ondelete="CASCADE"))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    usage: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (
        Index("ix_workspace_model_usage_project_time", "project_id", "recorded_at"),
        Index("ix_workspace_model_usage_recorded_at", "recorded_at"),
    )


class ProjectConnection(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "project_connections"
    review_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    first_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("projects.id"), index=True)
    second_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("projects.id"), index=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("projects.id"))
    dependent_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("projects.id"))
    purpose: Mapped[str] = mapped_column(Text, default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    lease: Mapped[UUID | None] = mapped_column(Uuid)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(String(400))
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("first_id", "second_id", name="uq_connection_pair"),
        CheckConstraint("first_id < second_id", name="connection_order"),
        CheckConstraint("owner_id IN (first_id, second_id)", name="connection_owner"),
        CheckConstraint("dependent_id IN (first_id, second_id)", name="connection_direction"),
        Index("ix_connection_due", "archived_at", "due_at"),
    )


class ConnectionFinding(UUIDPrimaryKey, Timestamps, Base):
    """A project reference or a review decision, including its original evidence."""

    __tablename__ = "connection_findings"
    connection_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("project_connections.id"), index=True
    )
    identity: Mapped[str] = mapped_column(String(64))
    source_project: Mapped[UUID] = mapped_column(Uuid, ForeignKey("projects.id"))
    source_kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[UUID | None] = mapped_column(Uuid)
    source_number: Mapped[int | None] = mapped_column(Integer)
    target_kind: Mapped[str | None] = mapped_column(String(16))
    target_id: Mapped[UUID | None] = mapped_column(Uuid)
    target_number: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(12))
    origin: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64))
    proposal_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("workspace_candidates.id"))
    __table_args__ = (
        UniqueConstraint("connection_id", "identity", name="uq_connection_finding"),
        Index("ix_connection_source", "source_project", "source_kind", "source_id"),
        Index("ix_connection_target", "target_kind", "target_id"),
    )


class ConnectionEvent(UUIDPrimaryKey, Base):
    __tablename__ = "connection_events"
    connection_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("project_connections.id"), index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    message: Mapped[str] = mapped_column(Text)


class WorkspaceBilledDay(Base):
    """The feed's finalized total for a single project and UTC billing date, in EUR."""

    __tablename__ = "workspace_billed_days"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    source: Mapped[str] = mapped_column(String(100))
    reference: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer)
    euro_micros: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (
        UniqueConstraint("source", "reference", name="uq_workspace_billed_reference"),
    )


class ProjectEpic(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "project_epics"
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("projects.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("project_id", "id", name="uq_project_epics_project_epic"),)


class ProjectUserStory(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "project_user_stories"
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("projects.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("project_id", "id", name="uq_project_user_stories_project_story"),
    )


class ReviewFlowAssignment(Base):
    __tablename__ = "review_flow_assignments"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    action: Mapped[str] = mapped_column(String(24), primary_key=True)
    flow_key: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int | None] = mapped_column(Integer)


class ReviewFlowRun(UUIDPrimaryKey, Base):
    __tablename__ = "review_flow_runs"
    findings: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(24))
    flow_key: Mapped[str] = mapped_column(String(64))
    flow_name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence_trace: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    outcome: Mapped[str | None] = mapped_column(String(16))


class SupervisorAdvice(Base):
    """One bounded, source-versioned specialist comparison. A lease is never replayed implicitly."""

    __tablename__ = "supervisor_advice"
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("review_flow_runs.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(16), index=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewWikiPage(Base):
    __tablename__ = "review_wiki_pages"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(120), index=True)
    page: Mapped[dict[str, Any]] = mapped_column(JSON)


class ReviewEvidenceJob(Base):
    """Immutable source snapshot, task checkpoints and separate pilot admission per run."""

    __tablename__ = "review_evidence_jobs"
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("review_flow_runs.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(20), index=True)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    token_cap: Mapped[int] = mapped_column(Integer)
    comparison_signature: Mapped[str | None] = mapped_column(String(64), index=True)
    spent: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    lease: Mapped[UUID | None] = mapped_column(Uuid)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectReviewBudget(Base):
    """An administrator's cumulative allowance for this project's model reviews."""

    __tablename__ = "project_review_budgets"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    token_limit: Mapped[int] = mapped_column(BigInteger)
    used: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved: Mapped[int] = mapped_column(BigInteger, default=0)
    # The size of the request that put the project on hold. Only an administrator clears it.
    required: Mapped[int | None] = mapped_column(BigInteger)


class StoryIssueCoverage(Base):
    """Checkpoint the imported issue inventory examined for an approved Story."""

    __tablename__ = "story_issue_coverage"
    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    story_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    revision: Mapped[str] = mapped_column(String(64))
    checked: Mapped[int] = mapped_column(Integer)
    story_revision: Mapped[str | None] = mapped_column(String(64))
    issue_versions: Mapped[dict[str, str] | None] = mapped_column(JSON)
    matches: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "story_id"],
            ["project_user_stories.project_id", "project_user_stories.id"],
            ondelete="CASCADE",
        ),
    )


class ProjectStoryEpic(Base):
    __tablename__ = "project_story_epics"
    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    story_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    epic_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "story_id"],
            ["project_user_stories.project_id", "project_user_stories.id"],
            ondelete="CASCADE",
            name="fk_project_story_epics_story",
        ),
        ForeignKeyConstraint(
            ["project_id", "epic_id"],
            ["project_epics.project_id", "project_epics.id"],
            ondelete="CASCADE",
            name="fk_project_story_epics_epic",
        ),
    )


class ProjectIssueStory(Base):
    """A technical issue uses the backlog item that already tracks its GitHub state."""

    __tablename__ = "project_issue_stories"
    project_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    item_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    story_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "item_id"],
            ["backlog_items.project_id", "backlog_items.id"],
            ondelete="CASCADE",
            name="fk_project_issue_stories_item",
        ),
        ForeignKeyConstraint(
            ["project_id", "story_id"],
            ["project_user_stories.project_id", "project_user_stories.id"],
            ondelete="CASCADE",
            name="fk_project_issue_stories_story",
        ),
    )


class ProjectSection(UUIDPrimaryKey, Timestamps, Base):
    """One kind of work on a project (migration 0026).

    Every project has the six core sections; the administrator adds custom ones. A section
    runs the flow assigned to it, or nothing while it has none.

    Five fields are its routing guide, written for whoever decides where work goes (the
    supervisor): what belongs in it, when to use it, when not to and where to send the
    work instead, example requests, and the labels that send an item here on their own.
    """

    __tablename__ = "project_sections"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(500), default="")
    use_when: Mapped[str] = mapped_column(String(600), default="")
    avoid_when: Mapped[str] = mapped_column(String(600), default="")
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    examples: Mapped[list[str]] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(8))
    position: Mapped[int] = mapped_column(Integer)
    flow_key: Mapped[str | None] = mapped_column(String(80))
    flow_version: Mapped[int | None] = mapped_column(Integer)
    operation_key: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_project_sections_project_key"),
        CheckConstraint("kind IN ('core', 'custom')", name="kind"),
    )


class BacklogItem(UUIDPrimaryKey, Timestamps, Base):
    """One piece of work wanted on a project (migration 0027).

    It comes from a GitHub issue of the project's repository, is written by the
    administrator, or is proposed: by the supervisor, by an audit (a finding) or by a
    search for enhancements. It is routed to one section of the project, or to none yet;
    the section's flow is what will run for it. ``routed_by`` says who decided: a label of
    the issue, the supervisor, or the administrator, whose choice nothing else ever changes.
    """

    __tablename__ = "backlog_items"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    section_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("project_sections.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), index=True)
    # Workspace approval is independent of delivery progress and the issue's GitHub state.
    # Older backlog items retain their original execution rules.
    workspace_review: Mapped[str | None] = mapped_column(String(16))
    routed_by: Mapped[str | None] = mapped_column(String(16))
    routing_reason: Mapped[str | None] = mapped_column(String(300))
    # Who proposed an item that did not come from GitHub: nobody named means the
    # administrator wrote it; ``supervisor`` means the supervisor proposed it (migration
    # 0031); ``audit`` and ``discovery`` mean a run of that kind did, and ``original`` that
    # it was brought over from the original system (migration 0034).
    proposed_by: Mapped[str | None] = mapped_column(String(16))
    # What an item that is not a GitHub issue asks for, in its own words (migration 0034).
    description: Mapped[str | None] = mapped_column(Text)
    # Facts about the item worth showing: a finding's severity and files, an enhancement's
    # effort, a pull request that already exists for it.
    facts: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # The run that produced the item, and a name for where it came from that is the same
    # every time the same thing is produced, so that nothing is proposed twice.
    source_run_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    origin_ref: Mapped[str | None] = mapped_column(String(160))
    # For an item that came from GitHub: where it is, and how it looked at the last import.
    repository: Mapped[str | None] = mapped_column(String(140))
    issue_number: Mapped[int | None] = mapped_column(Integer)
    issue_url: Mapped[str | None] = mapped_column(String(300))
    issue_state: Mapped[str | None] = mapped_column(String(16))
    github_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issue_labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    execution_type: Mapped[str | None] = mapped_column(String(16))
    classification_reason: Mapped[str | None] = mapped_column(Text)
    type_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    issue_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the item was done (migration 0047): merged by a run, closed on GitHub or marked
    # by the administrator. Empty for as long as it is not, and again if it is reopened.
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("state")
    def _dated(self, _key: str, value: str) -> str:
        # Whoever changes the state, the day it was done is kept with it.
        if value != "done":
            self.done_at = None
        elif self.done_at is None:
            self.done_at = datetime.now(UTC)
        return value

    __table_args__ = (
        UniqueConstraint(
            "project_id", "repository", "issue_number", name="uq_backlog_items_project_issue"
        ),
        Index("uq_backlog_items_project_item", "project_id", "id", unique=True),
        # An index, not a constraint: a constraint cannot be added to a table that exists
        # on every engine, and empty values count as different either way.
        Index("uq_backlog_items_project_origin", "project_id", "origin_ref", unique=True),
        CheckConstraint("source IN ('github', 'manual')", name="source"),
        CheckConstraint("state IN ('proposed', 'accepted', 'skipped', 'done')", name="state"),
    )


class ProviderAccount(UUIDPrimaryKey, Timestamps, Base):
    """One account the platform works with: a subscription sign-in for the agents in
    sandboxes (migration 0028), or an API key a model is asked with (migration 0042).

    A subscription's record holds no credential: that is a Secret of the sandbox
    namespace, named after the account, written when the administrator signs the account
    in and replaced when they do so again (``credential_generation`` counts how often).
    An API key is kept here, sealed with the platform's own key (``sealed_key``): it is
    never given to a sandbox. ``state`` is what the platform last learned: nothing stored
    yet, stored and not tried, working, refused for now (``limited_until``), or the
    provider wants a new sign-in or key.
    """

    __tablename__ = "provider_accounts"
    name: Mapped[str] = mapped_column(String(60), index=True)
    provider: Mapped[str] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(20))
    state_reason: Mapped[str | None] = mapped_column(String(300))
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=2)
    credential_generation: Mapped[int] = mapped_column(Integer, default=0)
    # What the sign-in says about itself and is safe to show: an address, a plan.
    identity: Mapped[str | None] = mapped_column(String(200))
    signed_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    limited_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # For an account with an API key: the key, sealed; which of the provider's addresses
    # it is used at; the models the provider listed; what each costs per million tokens.
    sealed_key: Mapped[str | None] = mapped_column(Text)
    endpoint: Mapped[str | None] = mapped_column(String(20))
    catalog_error: Mapped[str | None] = mapped_column(String(300))
    model_catalog: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    catalog_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    models: Mapped[list[str] | None] = mapped_column(JSON)
    prices: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    __table_args__ = (
        CheckConstraint(
            "state IN ('signed_out', 'unchecked', 'ready', 'limited', 'sign_in_required')",
            name="state",
        ),
        CheckConstraint("max_concurrent BETWEEN 1 AND 8", name="max_concurrent"),
    )


class SandboxRun(UUIDPrimaryKey, Timestamps, Base):
    """One piece of work done in sandboxes (migration 0029).

    A run is a sequence of steps, each one pod. ``kind`` says what the run is for: trying
    an account, or delivering a backlog item. ``data`` holds what the kind needs and may
    be shown (a repository, an issue number), never a credential.
    """

    __tablename__ = "sandbox_runs"
    kind: Mapped[str] = mapped_column(String(24), index=True)
    state: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(300))
    project_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    item_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("backlog_items.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_accounts.id", ondelete="SET NULL"), index=True
    )
    outcome: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(String(600))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled')",
            name="state",
        ),
    )


class SandboxStep(UUIDPrimaryKey, Timestamps, Base):
    """One step of a run: one pod in the sandbox namespace (migration 0029).

    A step that works with a subscription names its ``provider``; the account leased to it
    is ``account_id`` for as long as the step is running, which is what the limit of
    sandboxes per account counts.
    """

    __tablename__ = "sandbox_steps"
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("sandbox_runs.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20), index=True)
    provider: Mapped[str | None] = mapped_column(String(16))
    account_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_accounts.id", ondelete="SET NULL"), index=True
    )
    pod_name: Mapped[str | None] = mapped_column(String(80))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(String(600))
    # What the step was given (a round, the findings to act on) and what it produced.
    input: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    log: Mapped[str | None] = mapped_column(Text)
    # What the step's agent reported using: tokens in and out, and how long its client
    # says it worked (migration 0036). Not set for a step without an agent.
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_sandbox_steps_run_position"),
        CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'skipped')",
            name="state",
        ),
    )


class ProjectFlow(Base):
    """A private library key, independent of other projects' display keys."""

    __tablename__ = "project_flows"
    flow_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("projects.id", ondelete="CASCADE"))
    local_key: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("project_id", "local_key"),)


class OperationDefinition(Base):
    __tablename__ = "operation_definitions"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    guide: Mapped[dict[str, Any]] = mapped_column(JSON)


class CommandProfile(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "command_profiles"
    project_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(80))
    commands: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        Index(
            "uq_global_command_profile",
            "name",
            unique=True,
            sqlite_where=text("project_id IS NULL"),
            postgresql_where=text("project_id IS NULL"),
        ),
    )


class ProjectCommandSelection(Base):
    __tablename__ = "project_command_selections"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("command_profiles.id", ondelete="RESTRICT")
    )


class FlowVersion(UUIDPrimaryKey, Timestamps, Base):
    """One version of a flow other than the platform's own (migration 0036).

    A flow is a document (``engine/document.py``). The platform's own flows are version 0
    of their key and live in the code; every version made here is a row. A ``draft`` can
    still be changed and runs nothing; ``published`` is what cannot be changed any more,
    and the highest published version of a key is the one in force. A run keeps the
    version it started with.
    """

    __tablename__ = "flow_versions"
    flow_key: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), index=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(300))
    # What validation said when the version was last saved.
    problems: Mapped[list[Any] | None] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("flow_key", "version", name="uq_flow_versions_key_version"),
        CheckConstraint("state IN ('draft', 'published')", name="state"),
    )


class Evaluation(UUIDPrimaryKey, Timestamps, Base):
    """A version of a flow tried on past work before it is put in force (migration 0036).

    Each case replays one piece of past work on one version: the candidate, and the
    version it is compared with. The runs are ordinary runs of kind ``evaluation`` that
    leave nothing behind: nothing is published and nothing is added to the backlog.
    """

    __tablename__ = "evaluations"
    flow_key: Mapped[str] = mapped_column(String(64), index=True)
    candidate_version: Mapped[int] = mapped_column(Integer)
    baseline_version: Mapped[int] = mapped_column(Integer)
    project_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    state: Mapped[str] = mapped_column(String(16), index=True)
    note: Mapped[str | None] = mapped_column(String(300))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The candidate as it was when the evaluation started: a draft may change afterwards.
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    # The flow whose past runs are gone through, when it is not the flow tried
    # (migration 0044): a flow tried on the runs of another of the same sort.
    of_flow: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (CheckConstraint("state IN ('running', 'finished', 'stopped')", name="state"),)


class EvaluationCase(UUIDPrimaryKey, Timestamps, Base):
    """One piece of past work replayed on one version of a flow (migration 0036)."""

    __tablename__ = "evaluation_cases"
    evaluation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("evaluations.id", ondelete="CASCADE"), index=True
    )
    arm: Mapped[str] = mapped_column(String(16))
    # What is replayed: the item's words or the scope, and the commit it was worked on at.
    subject: Mapped[dict[str, Any]] = mapped_column(JSON)
    run_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    __table_args__ = (CheckConstraint("arm IN ('candidate', 'baseline')", name="arm"),)


class SavedTask(UUIDPrimaryKey, Timestamps, Base):
    """A task of the administrator's own, kept under a name to be used in flows
    (migration 0037): a kind of task of the platform's, its settings and the wording of
    its prompt."""

    __tablename__ = "saved_tasks"
    name: Mapped[str] = mapped_column(String(80), unique=True)
    task: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    wording: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # What the task is for, in the administrator's words (migration 0040).
    description: Mapped[str | None] = mapped_column(Text)


class CodeSummary(UUIDPrimaryKey, Timestamps, Base):
    """What a model said a file of a project's repository is for (migration 0045), with
    the name git has for the content it read. One row a file; a file whose content has
    another name by now is summarised again."""

    __tablename__ = "code_summaries"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(1000))
    blob: Mapped[str] = mapped_column(String(64))
    # Empty when the model was asked and gave none: the file is not asked about again
    # for the same content.
    summary: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120))
    run_id: Mapped[UUID | None] = mapped_column(Uuid)
    __table_args__ = (
        UniqueConstraint("project_id", "path", name="uq_code_summaries_project_path"),
    )


class ModelBatch(UUIDPrimaryKey, Timestamps, Base):
    """Questions handed to a provider as one batch (migration 0046): answered within
    hours instead of seconds, at a lower price. The record is what the platform knows
    of it, so that a batch is followed across restarts and never handed in twice.

    ``state``: ``submitting`` (recorded here, not yet known to be at the provider),
    ``waiting`` (at the provider), ``stopping`` (asked to stop: its time was over, or
    its run was stopped), ``ended`` (what it answered is in its requests) or ``failed``
    (it could not be handed in, or the provider gave it up).
    """

    __tablename__ = "model_batches"
    step_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("sandbox_steps.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    account_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_accounts.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    # The batch's name at the provider, once the provider has it.
    remote_id: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), index=True)
    # Why it is being stopped: "late" (not answered in its time) or "stopped".
    stopping: Mapped[str | None] = mapped_column(String(20))
    requests: Mapped[int] = mapped_column(Integer, default=0)
    answered: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str | None] = mapped_column(String(600))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # After this the platform stops waiting, whatever the provider's own limit is.
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "state IN ('submitting', 'waiting', 'stopping', 'ended', 'failed')", name="state"
        ),
    )


class ModelBatchRequest(UUIDPrimaryKey, Timestamps, Base):
    """One question of a batch (migration 0046): what was asked, and what came back.

    ``state``: ``waiting``, ``answered``, ``failed`` (the provider could not answer it,
    or the model declined) or ``late`` (the batch ended before it was answered)."""

    __tablename__ = "model_batch_requests"
    batch_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("model_batches.id", ondelete="CASCADE"), index=True
    )
    # The name the request has in the batch: answers come back by it, in any order.
    name: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    question: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20))
    answer: Mapped[str | None] = mapped_column(Text)
    # The model that answered, and what the answer used.
    model: Mapped[str | None] = mapped_column(String(120))
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Why there is no answer: the kind (as a call that brings none has it) and the words.
    kind: Mapped[str | None] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(String(600))
    __table_args__ = (
        UniqueConstraint("batch_id", "name", name="uq_model_batch_requests_batch_name"),
        CheckConstraint("state IN ('waiting', 'answered', 'failed', 'late')", name="state"),
    )


class Skill(UUIDPrimaryKey, Timestamps, Base):
    """Named instructions an agent can be given (migration 0043): how something is done
    here, written once and handed to every agent that has the skill."""

    __tablename__ = "skills"
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)


class AgentRecord(UUIDPrimaryKey, Timestamps, Base):
    """One who does work, as the administrator defined it (migration 0043): standing
    instructions, how it works in a sandbox and over an API (``settings``), and the
    skills it has, by their ids. A task of a flow names an agent; what the task is, and
    what holds for it, stays the task's."""

    __tablename__ = "agents"
    project_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(80))
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        Index(
            "uq_global_agent_name",
            "name",
            unique=True,
            sqlite_where=text("project_id IS NULL"),
            postgresql_where=text("project_id IS NULL"),
        ),
    )
    description: Mapped[str | None] = mapped_column(Text)
    instructions: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    skills: Mapped[list[str]] = mapped_column(JSON)


class RemovedFlow(UUIDPrimaryKey, Timestamps, Base):
    """A flow the platform knows from the start that the administrator deleted (migration
    0048): one of the platform's own, or a name of the library. The platform's own are in
    its code, so what is kept is that the flow is not wanted: nothing lists it, assigns
    it or starts it until it is put back. A run that followed it still shows what it
    followed."""

    __tablename__ = "removed_flows"
    flow_key: Mapped[str] = mapped_column(String(64), unique=True)


class HiddenTask(UUIDPrimaryKey, Timestamps, Base):
    """A kind of task of the platform's own that is not offered in the designer's list
    (migration 0040). Nothing else changes for it: a flow that has one keeps it."""

    __tablename__ = "hidden_tasks"
    task: Mapped[str] = mapped_column(String(64), unique=True)


class FlowTrigger(UUIDPrimaryKey, Timestamps, Base):
    """What starts a section's flow without anyone asking (migration 0037).

    ``kind`` says when: ``schedule`` at set times, ``push`` when the project's default
    branch has moved, ``item`` when an item arrives in the section. ``spec`` holds the
    details of the kind and ``state`` what the trigger last saw. A trigger starts what the
    administrator could start by hand, within the same budgets and pauses.
    """

    __tablename__ = "flow_triggers"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    section_key: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(String(300))
    fired: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint("kind IN ('schedule', 'push', 'item')", name="kind"),)


class BacklogSync(Base):
    """When a project's backlog was last brought in step with GitHub, and how it went."""

    __tablename__ = "backlog_syncs"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open_issues: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(300))


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


class ChangeRequest(UUIDPrimaryKey, Timestamps, Base):
    """NEW-108: one operator change request, drafted, reviewed and approved as an issue."""

    __tablename__ = "change_requests"
    request_key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    project: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    description_redacted: Mapped[str] = mapped_column(Text)
    description_sha256: Mapped[str] = mapped_column(String(64))
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    revision_note: Mapped[str | None] = mapped_column(Text)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    review_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dedupe_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str | None] = mapped_column(String(80), index=True)
    issue_title: Mapped[str | None] = mapped_column(String(240))
    issue_body: Mapped[str | None] = mapped_column(Text)
    approval_binding_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    target_repository: Mapped[str] = mapped_column(String(200))
    issue_url: Mapped[str | None] = mapped_column(Text)
    issue_number: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    requested_by: Mapped[str] = mapped_column(String(128))
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Incremented by every operator decision; publication is bound to the approved one.
    decision_sequence: Mapped[int] = mapped_column(Integer, default=0)
    # Written only at approval: the decision record and a hash of the approved content.
    approval_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # NEW-123: the blueprint item a fanned-out UI migration request came from (labels,
    # dependencies); rendered into the bound issue body. NULL for operator requests.
    ui_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # NEW-154: a gap request an operator consolidation merged (status MERGED, terminal)
    # names the canonical request that replaces it; blocker resolution follows it.
    merged_into_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    # NEW-154: the consolidation record of a merged member, a canonical request, or a
    # request whose blocked_by was re-pointed (or, once published, still names a member).
    consolidation_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # NEW-234: the backlog-decomposition task a fanned-out technical request came from
    # (rendered into the bound issue body), and its cross-plan dedupe identity
    # project:story:task. Both NULL for every other request.
    decomposition_context_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    decomposition_lineage: Mapped[str | None] = mapped_column(
        String(160), nullable=True, index=True
    )
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "status IN ('DRAFTING', 'CANDIDATE', 'APPROVED', 'PUBLISHED', 'REJECTED', 'FAILED', "
            "'MERGED')",
            name="status_known",
        ),
    )


UI_BLUEPRINT_STATUSES = (
    "DRAFTING",
    "BLOCKED",
    "FAILED",
    "CANDIDATE",
    "APPROVED",
    "SUPERSEDED",
)


class UiBlueprint(UUIDPrimaryKey, Timestamps, Base):
    """NEW-123: one UI migration blueprint run (flow A), its candidate and its approval."""

    __tablename__ = "ui_blueprints"
    project: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    workflow_id: Mapped[str] = mapped_column(String(240))
    requested_by: Mapped[str] = mapped_column(String(128))
    requested_source_sha: Mapped[str | None] = mapped_column(String(40))
    source_sha: Mapped[str | None] = mapped_column(String(40))
    inventory_sha256: Mapped[str | None] = mapped_column(String(64))
    inventory_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    kit_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    gntik_ui_version: Mapped[str | None] = mapped_column(String(100))
    kit_image: Mapped[str | None] = mapped_column(String(300))
    draft_attempts: Mapped[int] = mapped_column(Integer, default=0)
    review_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Every draft version, immutable once recorded; the reviewed one becomes the candidate.
    drafts_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    blueprint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    blueprint_sha256: Mapped[str | None] = mapped_column(String(64))
    # Hermes's hash of the exact draft file the independent reviewer read.
    reviewed_sha256: Mapped[str | None] = mapped_column(String(64))
    markdown_sha256: Mapped[str | None] = mapped_column(String(64))
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    approval_binding_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    error_code: Mapped[str | None] = mapped_column(String(80))
    blockers_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_sequence: Mapped[int] = mapped_column(Integer, default=0)
    fanout_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # At most one active fan-out: set by compare-and-set, cleared when that fan-out ends.
    fanout_sequence: Mapped[int] = mapped_column(Integer, default=0)
    active_fanout_workflow_id: Mapped[str | None] = mapped_column(
        String(240), unique=True, index=True
    )
    # When the active marker was claimed: a marker whose workflow Temporal does not know
    # yet is a start in progress for a grace period, not an ended fan-out.
    fanout_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFTING', 'BLOCKED', 'FAILED', 'CANDIDATE', 'APPROVED', 'SUPERSEDED')",
            name="status_known",
        ),
    )


BACKLOG_DECOMPOSITION_STATUSES = (
    "DRAFTING",
    "BLOCKED",
    "FAILED",
    "CANDIDATE",
    "APPROVED",
    "SUPERSEDED",
)


class BacklogDecomposition(UUIDPrimaryKey, Timestamps, Base):
    """NEW-234: one backlog decomposition run, its bound sources, candidate plan, approval
    and fan-out. Modelled on UiBlueprint, without the kit and inventory columns."""

    __tablename__ = "backlog_decompositions"
    project: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    workflow_id: Mapped[str] = mapped_column(String(240))
    requested_by: Mapped[str] = mapped_column(String(128))
    requested_source_sha: Mapped[str | None] = mapped_column(String(40))
    source_sha: Mapped[str | None] = mapped_column(String(40))
    # The bound sources: the design document, docs/backlog.json and the issue snapshot.
    design_sha256: Mapped[str | None] = mapped_column(String(64))
    backlog_sha256: Mapped[str | None] = mapped_column(String(64))
    issues_sha256: Mapped[str | None] = mapped_column(String(64))
    # Per-part (frame, epic batches) drafts and reviews, and the provider-call budget used.
    parts_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider_calls: Mapped[int] = mapped_column(Integer, default=0)
    review_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    plan_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    markdown_sha256: Mapped[str | None] = mapped_column(String(64))
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    approval_binding_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    error_code: Mapped[str | None] = mapped_column(String(80))
    blockers_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_sequence: Mapped[int] = mapped_column(Integer, default=0)
    fanout_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # At most one active fan-out: set by compare-and-set, cleared when that fan-out ends.
    fanout_sequence: Mapped[int] = mapped_column(Integer, default=0)
    active_fanout_workflow_id: Mapped[str | None] = mapped_column(
        String(240), unique=True, index=True
    )
    fanout_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFTING', 'BLOCKED', 'FAILED', 'CANDIDATE', 'APPROVED', 'SUPERSEDED')",
            name="status_known",
        ),
    )


UI_GAP_CONSOLIDATION_STATUSES = (
    "PLANNING",
    "PROPOSED",
    "INVALID",
    "FAILED",
    "APPLIED",
    "SUPERSEDED",
)


class UiGapConsolidation(UUIDPrimaryKey, Timestamps, Base):
    """NEW-154: one operator-driven consolidation of a blueprint's duplicate gap requests:
    the model-proposed plan (stored with its sha256) and, once applied, its audit record."""

    __tablename__ = "ui_gap_consolidations"
    blueprint_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    project: Mapped[str] = mapped_column(String(32))
    # kit-gap, backend-gap, or NULL for both.
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    workflow_id: Mapped[str] = mapped_column(String(240), unique=True)
    requested_by: Mapped[str] = mapped_column(String(128))
    # The eligible gap requests the plan was asked about (ids, keys, statuses, revisions).
    input_sha256: Mapped[str] = mapped_column(String(64), index=True)
    inputs_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    plan_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    problems_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    applied_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint(
            "status IN ('PLANNING', 'PROPOSED', 'INVALID', 'FAILED', 'APPLIED', 'SUPERSEDED')",
            name="status_known",
        ),
    )


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
    # Only correlated gateway-ingest events use this key. Historical rows are
    # left NULL; no old event or supposed acknowledgement is reconstructed.
    gateway_ingestion_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        Index("uq_dashboard_events_gateway_ingestion_key", "gateway_ingestion_key", unique=True),
    )


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
    accepted_operation_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "operation_requests.id",
            name="fk_action_candidates_accepted_operation",
            ondelete="SET NULL",
        ),
        index=True,
    )
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


ACTIVE_OPERATION_STATUSES = ("PENDING", "DISPATCHING", "STARTED", "RUNNING")


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
# NEW-234 PR6 review (migration 0023): at most one active change request per backlog
# decomposition lineage, so two decompositions can never both file the same task.
ACTIVE_DECOMPOSITION_STATUSES = ("DRAFTING", "CANDIDATE", "APPROVED", "PUBLISHED")
Index(
    "uq_change_requests_active_decomposition_lineage",
    ChangeRequest.decomposition_lineage,
    unique=True,
    postgresql_where=ChangeRequest.decomposition_lineage.is_not(None)
    & ChangeRequest.status.in_(ACTIVE_DECOMPOSITION_STATUSES),
    sqlite_where=ChangeRequest.decomposition_lineage.is_not(None)
    & ChangeRequest.status.in_(ACTIVE_DECOMPOSITION_STATUSES),
)
Index(
    "uq_operation_requests_active_target",
    OperationRequest.action,
    OperationRequest.target_id,
    unique=True,
    postgresql_where=OperationRequest.status.in_(ACTIVE_OPERATION_STATUSES),
    sqlite_where=OperationRequest.status.in_(ACTIVE_OPERATION_STATUSES),
)


class SupervisorDefinition(UUIDPrimaryKey, Timestamps, Base):
    """One version of the supervisor's definition (migration 0031).

    The supervisor is an agent like any other: a prompt, skills, the tools it may call and
    how each is confirmed, and which subscription it works on. Every save is a new version;
    the highest one is in force. Nothing here is a credential.
    """

    __tablename__ = "supervisor_definitions"
    version: Mapped[int] = mapped_column(Integer, unique=True)
    prompt: Mapped[str] = mapped_column(Text)
    skills: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tools: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # The providers it may work on, most wanted first, and optionally one account of its own.
    providers: Mapped[list[str]] = mapped_column(JSON, default=list)
    account_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_accounts.id", ondelete="SET NULL")
    )
    # When set, the account above is the supervisor's alone: no other work is given it.
    exclusive: Mapped[bool] = mapped_column(Boolean, default=False)
    models: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # How hard the model reasons, by provider (migration 0039). Not set: the client's own.
    efforts: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(String(300))
    # Whether it looks at failed and stuck work by itself, and how often within an hour
    # (migration 0034). Not set: the platform's defaults.
    operations: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class SupervisorMessage(UUIDPrimaryKey, Timestamps, Base):
    """One message of the conversation between the administrator and the supervisor
    (migration 0031). There is one conversation; the console and the chat channel are two
    ways into it. ``platform`` messages are what the platform itself adds: a decision the
    administrator took on a proposal, a turn that could not be answered."""

    __tablename__ = "supervisor_messages"
    thread: Mapped[str] = mapped_column(String(80), default="", server_default="")
    scope: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    request_id: Mapped[UUID | None] = mapped_column(Uuid)
    role: Mapped[str] = mapped_column(String(16), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    # The turn that answers an administrator's message, or produced a supervisor's.
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("sandbox_runs.id", ondelete="SET NULL"), index=True
    )
    __table_args__ = (
        Index("ix_supervisor_thread_time", "thread", "created_at"),
        Index("uq_supervisor_message_request", "thread", "request_id", unique=True),
        CheckConstraint("role IN ('administrator', 'supervisor', 'platform')", name="role"),
        CheckConstraint("channel IN ('console', 'chat', 'platform')", name="channel"),
    )


class SupervisorAction(UUIDPrimaryKey, Timestamps, Base):
    """One call the supervisor made to a tool, and what became of it (migration 0031).

    ``outcome`` is what the platform did with the call: ``read`` (answered), ``applied``
    (carried out), ``pending`` (waits for the administrator), ``accepted`` or ``rejected``
    (their answer), ``refused`` (against a rule) or ``failed``. This table is the record of
    the supervisor's decisions.
    """

    __tablename__ = "supervisor_actions"
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("sandbox_runs.id", ondelete="SET NULL"), index=True
    )
    tool: Mapped[str] = mapped_column(String(40), index=True)
    # What kind of tool it is: read, plan, operate or ask.
    kind: Mapped[str] = mapped_column(String(16))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    # What the action is, in a sentence the administrator can decide on.
    summary: Mapped[str | None] = mapped_column(String(600))
    reason: Mapped[str | None] = mapped_column(String(600))
    detail: Mapped[str | None] = mapped_column(String(600))
    project_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_via: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('read', 'applied', 'pending', 'accepted', 'rejected', 'refused', "
            "'failed')",
            name="outcome",
        ),
        CheckConstraint("kind IN ('read', 'plan', 'operate', 'ask')", name="kind"),
    )


class SupervisorIncident(UUIDPrimaryKey, Timestamps, Base):
    """Something that went wrong with the work and that the platform noticed by itself
    (migration 0034): a run that failed, a run that makes no progress, a queue that paused
    itself, an account that can no longer be used.

    Noticing is the platform's and needs no model. What to do about it is the
    supervisor's: ``open`` until it has looked, then ``handled`` with its diagnosis and what
    it did, or ``waiting`` when what it wants to do waits for the administrator.
    ``dismissed`` is the administrator's. ``kind`` and ``ref`` together name the thing, so
    that the same trouble is one incident however often it is seen.
    """

    __tablename__ = "supervisor_incidents"
    kind: Mapped[str] = mapped_column(String(24), index=True)
    ref: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(16), index=True)
    summary: Mapped[str] = mapped_column(String(600))
    # What the platform knows about it, to show and to tell the supervisor.
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    project_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    item_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    # The supervisor's turn that looked at it, and what it concluded.
    turn_run_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    diagnosis: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(String(8))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("kind", "ref", name="uq_supervisor_incidents_kind_ref"),
        CheckConstraint(
            "state IN ('open', 'looking', 'handled', 'waiting', 'dismissed')", name="state"
        ),
    )


class WorkQueue(UUIDPrimaryKey, Timestamps, Base):
    """Items of one project to be worked on in order (migration 0032).

    A queue is made by the administrator from items of the backlog, or proposed by the
    supervisor with its reasons. It is a ``draft`` until the administrator starts it; then
    its items run in order, as many at the same time as ``at_once`` allows, until it is
    ``finished``. It can be paused, reordered and extended while it runs.
    """

    __tablename__ = "work_queues"
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(16), index=True)
    at_once: Mapped[int] = mapped_column(Integer, default=1)
    # Who made it (nobody named: the administrator) and, for the supervisor, why this order.
    proposed_by: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(600))
    # Why it is paused, when the platform paused it itself.
    pause_reason: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("state IN ('draft', 'running', 'paused', 'finished')", name="state"),
        CheckConstraint("at_once BETWEEN 1 AND 4", name="at_once"),
    )


class EpicExecutionPlan(Timestamps, Base):
    __tablename__ = "epic_execution_plans"
    review_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    queue_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("work_queues.id", ondelete="CASCADE"), primary_key=True
    )
    epic_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("project_epics.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    version: Mapped[int] = mapped_column(Integer, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    lease: Mapped[UUID | None] = mapped_column(Uuid)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    detail: Mapped[str | None] = mapped_column(String(400))
    __table_args__ = (Index("ix_epic_execution_due", "status", "due_at"),)


class WorkQueueEntry(UUIDPrimaryKey, Timestamps, Base):
    """One item's place in a queue (migration 0032). ``state`` follows the item's run:
    ``waiting`` for its turn, ``running``, then ``done`` (its run ended well, or its change
    waits for the administrator), ``failed`` or ``skipped`` (it could not be started)."""

    __tablename__ = "work_queue_entries"
    queue_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("work_queues.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("backlog_items.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), index=True)
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("sandbox_runs.id", ondelete="SET NULL"), index=True
    )
    # Why the item is at this place (the supervisor's), and what became of it.
    reason: Mapped[str | None] = mapped_column(String(300))
    detail: Mapped[str | None] = mapped_column(String(300))
    __table_args__ = (
        UniqueConstraint("queue_id", "item_id", name="uq_work_queue_entries_queue_item"),
        CheckConstraint(
            "state IN ('waiting', 'running', 'done', 'failed', 'skipped')", name="state"
        ),
    )


class Budget(UUIDPrimaryKey, Timestamps, Base):
    """How much may run for a scope (migration 0032): the whole platform, one project or
    one subscription account. A limit left empty does not limit. For the platform and a
    project the limits count runs of work; for an account they count steps, since an
    account is leased to a step."""

    __tablename__ = "budgets"
    scope: Mapped[str] = mapped_column(String(16))
    # The project or the account; empty for the platform.
    scope_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    at_once: Mapped[int | None] = mapped_column(Integer)
    per_day: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("scope IN ('platform', 'project', 'account')", name="scope"),
        CheckConstraint("at_once IS NULL OR at_once BETWEEN 1 AND 100", name="at_once"),
        CheckConstraint("per_day IS NULL OR per_day BETWEEN 1 AND 10000", name="per_day"),
    )


class ChatState(Timestamps, Base):
    """What the chat channel remembers between restarts (migration 0033): how far it has
    read the transport's updates, and since when it tells the administrator about things."""

    __tablename__ = "chat_state"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class ChatDelivery(UUIDPrimaryKey, Timestamps, Base):
    """Something the chat channel told the administrator (migration 0033), so that it is
    told once. ``ref`` names the thing: a message, a run in a state, a day's digest."""

    __tablename__ = "chat_deliveries"
    kind: Mapped[str] = mapped_column(String(24), index=True)
    ref: Mapped[str] = mapped_column(String(120))
    __table_args__ = (UniqueConstraint("kind", "ref", name="uq_chat_deliveries_kind_ref"),)
