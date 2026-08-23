from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from engineering_control.models.enums import (
    AuditLens,
    ChangeSetState,
    CoverageStatus,
    FindingDisposition,
    ModelClass,
    Project,
    Provider,
    RedactionStatus,
    RiskClass,
    RunStatus,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, validate_assignment=True)


class AgentArtifact(StrictModel):
    path: str
    media_type: str = "application/octet-stream"
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    redaction_status: RedactionStatus


class AgentEvent(StrictModel):
    run_id: UUID
    provider: Provider
    sequence: int = Field(ge=0)
    event_type: str
    occurred_at: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)


class ProviderUsage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    remaining_capacity: float | None = Field(default=None, ge=0)
    reset_at: datetime | None = None
    source: str = "provider_event"


class AgentRequest(StrictModel):
    run_id: UUID = Field(default_factory=uuid4)
    project: Project
    role: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9-]*$")
    provider: Provider
    model_class: ModelClass
    prompt: str = Field(min_length=1, max_length=200_000, repr=False)
    working_directory: str
    output_schema: dict[str, Any]
    timeout_seconds: int = Field(default=1800, ge=30, le=21_600)
    effort: str | None = Field(default=None, pattern=r"^(low|medium|high|xhigh|max)$")
    model: str | None = None
    writable: bool = False
    allowed_tools: tuple[str, ...] = ()
    change_set_id: UUID | None = None
    campaign_id: UUID | None = None


class AgentResult(StrictModel):
    run_id: UUID
    provider: Provider
    role: str
    status: RunStatus
    summary: str = ""
    artifacts: list[AgentArtifact] = Field(default_factory=list)
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
    structured_result: dict[str, Any] = Field(default_factory=dict)
    provider_session_id: str | None = None
    started_at: datetime
    finished_at: datetime
    exit_code: int | None = None
    error_code: str | None = None


class ArtifactMetadata(StrictModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    project: Project
    campaign_id: UUID | None = None
    change_set_id: UUID | None = None
    run_id: UUID
    relative_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_type: str
    producer: str
    created_at: datetime = Field(default_factory=utcnow)
    redaction_status: RedactionStatus
    size_bytes: int = Field(ge=0)
    retention_class: Literal["standard", "ephemeral", "security", "release", "incident"] = (
        "standard"
    )

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("artifact paths must be relative and cannot traverse")
        return value


class CoverageCell(StrictModel):
    coverage_cell_id: UUID = Field(default_factory=uuid4)
    campaign_id: UUID
    project: Project
    domain: str
    persona: str
    lens: AuditLens
    environment: str
    status: CoverageStatus = CoverageStatus.NOT_STARTED
    evidence_artifact_ids: list[UUID] = Field(default_factory=list)
    blocker: str | None = None
    exclusion_approval_id: UUID | None = None

    @model_validator(mode="after")
    def require_terminal_evidence(self) -> CoverageCell:
        terminal = {
            CoverageStatus.NO_FINDING,
            CoverageStatus.CONFIRMED_FINDING,
            CoverageStatus.VERIFIED_FIXED,
        }
        if self.status in terminal and not self.evidence_artifact_ids:
            raise ValueError("terminal coverage status requires evidence")
        if self.status == CoverageStatus.BLOCKED and not self.blocker:
            raise ValueError("blocked coverage status requires a blocker")
        return self


class Finding(StrictModel):
    finding_id: UUID = Field(default_factory=uuid4)
    fingerprint: str = Field(min_length=16, max_length=128)
    project: Project
    domain: str
    persona: str
    severity: str
    confidence: float = Field(ge=0, le=1)
    risk_class: RiskClass
    expected_behavior: str
    actual_behavior: str
    evidence_artifact_ids: list[UUID]
    minimal_reproduction: list[str]
    affected_source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    affected_deployment_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    affected_running_image_digest: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    affected_endpoint_component: str
    security_impact: str | None = None
    data_impact: str | None = None
    recommended_action: str
    duplicate_issue: HttpUrl | None = None
    disposition: FindingDisposition = FindingDisposition.CANDIDATE


class ChangeSet(StrictModel):
    change_set_id: UUID = Field(default_factory=uuid4)
    project: Project
    finding_id: UUID
    github_issue: HttpUrl | None = None
    openspec_change: str | None = None
    risk_class: RiskClass
    state: ChangeSetState = ChangeSetState.CANDIDATE
    source_repository: str
    source_branch: str | None = None
    source_base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_head_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    source_pr: HttpUrl | None = None
    deployment_repository: str
    deployment_branch: str | None = None
    deployment_base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_head_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    deployment_pr: HttpUrl | None = None
    image_digests: dict[str, str] = Field(default_factory=dict)
    chart_version: str | None = None
    ci_result_artifact_ids: list[UUID] = Field(default_factory=list)
    preview_deployment_id: UUID | None = None
    staging_deployment_id: UUID | None = None
    approval_ids: list[UUID] = Field(default_factory=list)
    verification_result_ids: list[UUID] = Field(default_factory=list)
    rollback_plan_artifact_id: UUID | None = None
    recovery_plan_artifact_id: UUID | None = None
    version: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("image_digests")
    @classmethod
    def immutable_digests(cls, values: dict[str, str]) -> dict[str, str]:
        bad = [name for name, value in values.items() if not value.startswith("sha256:")]
        if bad:
            raise ValueError(f"images must use immutable sha256 digests: {sorted(bad)}")
        return values


class ApprovalBinding(StrictModel):
    approval_id: UUID = Field(default_factory=uuid4)
    change_set_id: UUID
    project: Project
    risk_class: RiskClass
    source_pr: HttpUrl | None
    source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_pr: HttpUrl | None
    deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    image_digests: dict[str, str]
    target_context: str
    target_namespace: str
    migration: bool
    backup_requirement: str
    rollback_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ci_result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    independent_review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    release_preflight_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approver_subject: str
    typed_confirmation: str | None = None
    approved_at: datetime = Field(default_factory=utcnow)
