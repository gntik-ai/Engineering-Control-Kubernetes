from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from engineering_control.models.enums import AuditLens, Project, RiskClass

CoverageSlug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")]

SourceChannel = Literal["dashboard", "telegram", "cli"]
CapabilityStatus = Literal[
    "AVAILABLE", "RUNNING", "BLOCKED", "DISABLED", "NOT_CONFIGURED", "NOT_INSTALLED"
]
ConnectionStatus = Literal[
    "CONNECTED",
    "DEGRADED",
    "DISCONNECTED",
    "STALE",
    "BLOCKED_BY_POLICY",
    "NOT_CONFIGURED",
    "NOT_INSTALLED",
]


class OperationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SolveIssueCommand(OperationModel):
    action: Literal["solve_issue"] = "solve_issue"
    project: Project
    backlog_campaign_id: UUID
    repository: Literal[
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
        "gntik-ai/musematic",
        "gntik-ai/musematic-deploy",
        "gntik-ai/llmwiki",
    ]
    issue_number: int = Field(ge=1)
    maximum_risk: RiskClass = RiskClass.R2
    delivery_mode: Literal["source-pr", "full-delivery"] = "full-delivery"
    # NEW-88: operator opt-in, for this one issue in source-pr mode, to accept the independent
    # current-source revalidation as the proof of a RUNTIME/DEPLOYMENT_REQUIRED issue (no step
    # can record a runtime reproduction, and source-pr deploys nothing to verify afterwards).
    runtime_proof: Literal["runtime", "source-revalidation"] = "runtime"
    correction_limit: int = Field(default=2, ge=0, le=5)
    production: Literal[False] = False

    @model_validator(mode="after")
    def source_revalidation_is_source_pr_only(self) -> SolveIssueCommand:
        if self.runtime_proof == "source-revalidation" and self.delivery_mode != "source-pr":
            raise ValueError("runtime proof by source revalidation requires source-pr delivery")
        return self


class SolveBacklogCommand(OperationModel):
    action: Literal["solve_backlog"] = "solve_backlog"
    project: Literal["falcone", "musematic", "llmwiki"]
    backlog_campaign_id: UUID
    issue_filter: Literal["bugs", "bugs-and-docs", "security", "enhancements", "all"] | None = None
    maximum_risk: RiskClass = RiskClass.R2
    delivery_mode: Literal["source-pr", "full-delivery"] = "full-delivery"
    maximum_items: int = Field(default=10, ge=1, le=50)
    continue_after_blocked_issue: bool = True
    include_enhancements: bool = False
    correction_limit: int = Field(default=2, ge=0, le=5)
    concurrency: Literal[1] = 1
    deployment_concurrency: Literal[1] = 1
    production: Literal[False] = False


class AutonomousBacklogCommand(OperationModel):
    action: Literal["start_autonomous_backlog"] = "start_autonomous_backlog"
    campaign_id: UUID
    backlog_campaign_ids: dict[str, UUID]
    policy_campaign_id: UUID
    environment: Literal["staging"] = "staging"
    maximum_risk: Literal[RiskClass.R2] = RiskClass.R2
    global_active_issues: Literal[1] = 1
    global_deployments: Literal[1] = 1
    implementation_correction_limit: Literal[5] = 5
    deployment_correction_limit: Literal[3] = 3
    production: Literal[False] = False
    typed_confirmation: Literal["START_GLOBAL_STAGING_R2_BACKLOG"]

    @model_validator(mode="after")
    def exact_project_set(self) -> AutonomousBacklogCommand:
        if set(self.backlog_campaign_ids) != {"falcone", "musematic", "llmwiki"}:
            raise ValueError("global backlog requires exactly Falcone, Musematic, and LLMWiki")
        if self.policy_campaign_id not in set(self.backlog_campaign_ids.values()):
            raise ValueError("policy campaign must be one of the exact imported inventories")
        return self


AuditPreset = Literal[
    "snapshot",
    "static-full",
    "e2e-smoke",
    "full-e2e",
    "security",
    "ux",
    "deployment",
    "documentation",
    "falcone-intensive",
]


class AuditPlanCommand(OperationModel):
    action: Literal["audit_plan"] = "audit_plan"
    project: Project
    preset: AuditPreset
    environment: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    personas: list[str] = Field(default_factory=list, max_length=100)
    lenses: list[str] = Field(default_factory=list, max_length=20)
    documentation_level: Literal["none", "technical", "devops", "end-user", "all"] = "all"
    publish_findings: Literal[False] = False


class AuditStartCommand(OperationModel):
    action: Literal["audit_start"] = "audit_start"
    project: Project
    preset: AuditPreset
    environment: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    personas: list[str] = Field(default_factory=list, max_length=100)
    lenses: list[str] = Field(default_factory=list, max_length=20)
    documentation_level: Literal["none", "technical", "devops", "end-user", "all"] = "all"
    publish_findings: bool = False
    production: Literal[False] = False


class AuditAllProjectsCommand(OperationModel):
    action: Literal["audit_all_projects"] = "audit_all_projects"
    mode: Literal["plan", "start"] = "plan"
    preset: Literal[
        "snapshot",
        "static-full",
        "e2e-smoke",
        "full-e2e",
        "security",
        "ux",
        "deployment",
        "documentation",
    ]
    environment: Literal["staging"] = "staging"
    publish_findings: bool = False
    production: Literal[False] = False

    @model_validator(mode="after")
    def planning_never_publishes(self) -> AuditAllProjectsCommand:
        if self.mode == "plan" and self.publish_findings:
            raise ValueError("all-project audit planning cannot publish findings")
        return self


class AdvancedAuditCommand(OperationModel):
    """Compatibility command for the existing immutable CLI coverage matrix."""

    action: Literal["audit_campaign"] = "audit_campaign"
    project: Project
    name: str = Field(min_length=1, max_length=200)
    source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    domains: list[CoverageSlug] = Field(min_length=1, max_length=100)
    personas: list[CoverageSlug] = Field(min_length=1, max_length=100)
    lenses: list[AuditLens] = Field(min_length=1, max_length=20)
    environments: list[CoverageSlug] = Field(min_length=1, max_length=10)
    execute: bool = False
    production: Literal[False] = False

    @model_validator(mode="after")
    def bounded_matrix(self) -> AdvancedAuditCommand:
        cells = len(self.domains) * len(self.personas) * len(self.lenses) * len(self.environments)
        if cells > 100_000:
            raise ValueError("coverage matrix exceeds the 100000-cell safety bound")
        return self


class WorkflowControlCommand(OperationModel):
    action: Literal[
        "pause",
        "pause_after_current",
        "resume",
        "nudge",
        "retry",
        "stop_after_current",
        "cancel",
        "skip",
    ]
    workflow_id: str = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9._:-]+$")
    repository: (
        Literal["falcone", "falcone-charts", "musematic", "musematic-deploy", "llmwiki"] | None
    ) = None
    issue_number: int | None = Field(default=None, ge=1)
    reason_code: str | None = Field(default=None, min_length=3, max_length=120)

    @model_validator(mode="after")
    def bound_issue_retry(self) -> WorkflowControlCommand:
        if (self.repository is None) != (self.issue_number is None):
            raise ValueError("repository and issue number must be supplied together")
        if self.action == "skip" and (self.repository is None or self.reason_code is None):
            raise ValueError("skip requires one issue and an allowlisted reason")
        return self


class RefreshConnectionsCommand(OperationModel):
    action: Literal["refresh_connections"] = "refresh_connections"
    scope: Literal["all", "public", "hermes", "control", "workers", "providers", "projects"] = "all"


class EnhancementDiscoverCommand(OperationModel):
    action: Literal["enhancement_discover"] = "enhancement_discover"
    project: Project
    campaign_id: UUID
    dry_run: bool = True


class EnhancementApproveCommand(OperationModel):
    action: Literal["enhancement_approve"] = "enhancement_approve"
    project: Project
    enhancement_id: str = Field(pattern=r"^enh-[a-f0-9]{32}$")
    approval_binding: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    production: Literal[False] = False


class EnhancementRejectCommand(OperationModel):
    action: Literal["enhancement_reject"] = "enhancement_reject"
    project: Project
    enhancement_id: str = Field(pattern=r"^enh-[a-f0-9]{32}$")
    approval_binding: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reason_code: Literal[
        "DUPLICATE",
        "NOT_ACTIONABLE",
        "OUT_OF_SCOPE",
        "POLICY_BLOCKED",
    ]
    production: Literal[False] = False


class EnhancementRunCommand(OperationModel):
    action: Literal["enhancement_run"] = "enhancement_run"
    project: Project
    enhancement_id: str = Field(pattern=r"^enh-[a-f0-9]{32}$")
    approval_binding: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    risk: RiskClass
    execution_mode: Literal["plan-only", "source-pr", "preview", "staging"] = "plan-only"
    dry_run: bool = True
    production: Literal[False] = False

    @model_validator(mode="after")
    def execution_requires_confirmation(self) -> EnhancementRunCommand:
        if self.execution_mode != "plan-only" and self.dry_run:
            raise ValueError("enhancement execution requires dry_run=false")
        return self


class FindingVerifyCommand(OperationModel):
    action: Literal["finding_verify"] = "finding_verify"
    finding_id: UUID


class LLMWikiBootstrapPlanCommand(OperationModel):
    action: Literal["llmwiki_bootstrap_plan"] = "llmwiki_bootstrap_plan"
    project: Literal["llmwiki"] = "llmwiki"
    environment: Literal["staging"] = "staging"


class EvidenceExportCampaignCommand(OperationModel):
    action: Literal["evidence_export_campaign"] = "evidence_export_campaign"
    campaign_id: UUID
    format: Literal["json"] = "json"


class EvidenceExportWorkflowCommand(OperationModel):
    action: Literal["evidence_export_workflow"] = "evidence_export_workflow"
    workflow_id: str = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9._:-]+$")
    format: Literal["json"] = "json"


type Command = Annotated[
    SolveIssueCommand
    | SolveBacklogCommand
    | AutonomousBacklogCommand
    | AuditPlanCommand
    | AuditStartCommand
    | AuditAllProjectsCommand
    | AdvancedAuditCommand
    | WorkflowControlCommand
    | RefreshConnectionsCommand
    | EnhancementDiscoverCommand
    | EnhancementApproveCommand
    | EnhancementRejectCommand
    | EnhancementRunCommand
    | FindingVerifyCommand
    | LLMWikiBootstrapPlanCommand
    | EvidenceExportCampaignCommand
    | EvidenceExportWorkflowCommand,
    Field(discriminator="action"),
]

_COMMAND_ADAPTER: TypeAdapter[Command] = TypeAdapter(Command)


def parse_command(value: Any) -> Command:
    return _COMMAND_ADAPTER.validate_python(value)


class CapabilityResult(OperationModel):
    capability: str = Field(min_length=1, max_length=80)
    status: CapabilityStatus
    project: str | None = Field(default=None, max_length=32)
    mode: str | None = Field(default=None, max_length=80)
    blockers: list[str] = Field(default_factory=list, max_length=50)
    required_approval: str | None = Field(default=None, max_length=120)
    required_credentials: list[str] = Field(default_factory=list, max_length=20)
    checked_at: datetime
    next_action: str | None = Field(default=None, max_length=500)


class CandidateView(OperationModel):
    candidate_id: UUID
    action: str
    target_type: str
    target_id: str
    project: str | None
    binding_hash: str
    confirmation_phrase: str | None
    capability: CapabilityResult
    operation_binding: dict[str, Any]
    expires_at: datetime


class OperationView(OperationModel):
    operation_id: UUID
    action: str
    target_type: str
    target_id: str
    source_channel: SourceChannel
    status: str
    workflow_id: str | None
    temporal_run_id: str | None
    binding_hash: str
    safe_error: str | None
    created_at: datetime
    result_available: bool = False


class ConnectionView(OperationModel):
    component: str
    logical_target: str
    status: ConnectionStatus
    checked_at: datetime
    last_success_at: datetime | None
    last_failure_at: datetime | None
    latency_ms: int | None
    freshness_seconds: int
    safe_error: str | None
    source: str
    next_action: str | None
    details: dict[str, Any]
