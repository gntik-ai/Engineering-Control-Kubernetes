from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engineering_control.models.enums import AuditLens, Project, RiskClass

CoverageSlug = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9-]{0,119}$"),
]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignStart(APIModel):
    project: Project
    name: str = Field(default="audit", min_length=1, max_length=200)
    preset: (
        Literal[
            "snapshot",
            "static-full",
            "e2e-smoke",
            "full-e2e",
            "security",
            "ux",
            "deployment",
            "falcone-intensive",
        ]
        | None
    ) = None
    environment: CoverageSlug | None = None
    source_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    deployment_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    domains: list[CoverageSlug] = Field(default_factory=list, max_length=100)
    personas: list[CoverageSlug] = Field(default_factory=list, max_length=100)
    lenses: list[AuditLens] = Field(default_factory=list, max_length=20)
    environments: list[CoverageSlug] = Field(default_factory=list, max_length=10)
    dry_run: bool = True
    publish_findings: bool = False
    application_data_mutation_approved: bool = False

    @model_validator(mode="after")
    def bounded_coverage_matrix(self) -> CampaignStart:
        if self.publish_findings and self.dry_run:
            raise ValueError("finding publication requires explicit execution mode")
        if self.application_data_mutation_approved and (self.dry_run or self.preset != "full-e2e"):
            raise ValueError("application-data mutation requires an executing full-e2e campaign")
        if self.preset is not None:
            if self.environment is None:
                raise ValueError("preset campaigns require one environment")
            if any((self.domains, self.personas, self.lenses, self.environments)):
                raise ValueError("preset campaigns cannot include a free-form coverage matrix")
            if self.project is Project.LLMWIKI and self.environment != "staging":
                raise ValueError("LLMWiki rejects every non-staging audit target")
            if self.preset == "falcone-intensive" and self.project is not Project.FALCONE:
                raise ValueError("falcone-intensive is bound to Falcone")
            return self
        if not all((self.source_sha, self.deployment_sha)):
            raise ValueError("advanced campaigns require both immutable repository SHAs")
        if not all((self.domains, self.personas, self.lenses, self.environments)):
            raise ValueError("advanced campaigns require a complete bounded coverage matrix")
        cells = len(self.domains) * len(self.personas) * len(self.lenses) * len(self.environments)
        if cells > 100_000:
            raise ValueError("coverage matrix exceeds the 100000-cell safety bound")
        return self


class AuditPlanRequest(APIModel):
    project: Project
    preset: Literal[
        "snapshot",
        "static-full",
        "e2e-smoke",
        "full-e2e",
        "security",
        "ux",
        "deployment",
        "falcone-intensive",
    ]
    environment: CoverageSlug

    @model_validator(mode="after")
    def staging_only_projects(self) -> AuditPlanRequest:
        if self.project is Project.LLMWIKI and self.environment != "staging":
            raise ValueError("LLMWiki rejects every non-staging audit target")
        if self.preset == "falcone-intensive" and self.project is not Project.FALCONE:
            raise ValueError("falcone-intensive is bound to Falcone")
        return self


class MultiProjectAuditStart(APIModel):
    preset: Literal[
        "snapshot",
        "static-full",
        "e2e-smoke",
        "full-e2e",
        "security",
        "ux",
        "deployment",
    ]
    environment: Literal["staging"] = "staging"
    publish_findings: bool = False
    production: Literal[False] = False


class LLMWikiBootstrapPlanRequest(APIModel):
    project: Literal["llmwiki"] = "llmwiki"
    environment: Literal["staging"] = "staging"


class LLMWikiBootstrapExecuteRequest(APIModel):
    project: Literal["llmwiki"] = "llmwiki"
    environment: Literal["staging"] = "staging"
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    execute: Literal[True] = True


class IssueLoopStart(APIModel):
    project: Project
    campaign_id: UUID
    maximum_risk: RiskClass = RiskClass.R1
    maximum_items: int = Field(default=1, ge=1, le=50)
    execution_mode: Literal["plan-only", "source-pr", "preview", "staging"] = "plan-only"
    component_concurrency: int = Field(default=1, ge=1, le=8)
    stop_policy: Literal["pause-on-blocker", "continue-eligible", "stop-on-failure"] = (
        "pause-on-blocker"
    )
    dry_run: bool = True

    @model_validator(mode="after")
    def execute_is_explicit(self) -> IssueLoopStart:
        if self.execution_mode != "plan-only" and self.dry_run:
            raise ValueError("non-planning loop modes require explicit execution")
        if self.project is Project.LLMWIKI and self.execution_mode == "preview":
            raise ValueError("LLMWiki is staging-only and rejects preview deployment")
        return self


class GitHubBacklogImportRequest(APIModel):
    project: Literal["falcone", "musematic", "llmwiki"]
    source: Literal["github-open"] = "github-open"
    import_filter: Literal["bugs", "bugs-and-docs", "security", "enhancements", "all"]
    maximum_items: int = Field(default=500, ge=1, le=500)


class BacklogApprovalCandidateRequest(APIModel):
    project: Literal["falcone", "musematic", "llmwiki"]
    delivery_mode: Literal["source-pr", "full-delivery"]
    maximum_risk: RiskClass
    maximum_items: int = Field(default=1, ge=1, le=50)


class BacklogApprovalRequest(APIModel):
    project: Literal["falcone", "musematic", "llmwiki"]
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    typed_confirmation: Literal["APPROVE_EXISTING_GITHUB_BACKLOG_LOOP"]


class BacklogOwnershipReleaseRequest(APIModel):
    project: Literal["falcone", "musematic", "llmwiki"]
    typed_confirmation: Literal["RELEASE_GITHUB_WRITE_POLICY_OWNERSHIP"]


class ExistingBacklogLoopStart(APIModel):
    project: Literal["falcone", "musematic", "llmwiki"]
    backlog_campaign_id: UUID
    maximum_risk: RiskClass
    delivery_mode: Literal["source-pr", "full-delivery"]
    maximum_items: int = Field(default=1, ge=1, le=50)
    target_repository: (
        Literal[
            "gntik-ai/falcone",
            "gntik-ai/falcone-charts",
            "gntik-ai/musematic",
            "gntik-ai/musematic-deploy",
            "gntik-ai/llmwiki",
        ]
        | None
    ) = None
    target_issue_number: int | None = Field(default=None, ge=1)
    continue_after_blocked_issue: bool = True
    correction_limit: int = Field(default=2, ge=0, le=5)
    global_campaign_id: UUID | None = None
    r3_approval_binding: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    runtime_proof: Literal["runtime", "source-revalidation"] = "runtime"

    @model_validator(mode="after")
    def exact_issue_binding(self) -> ExistingBacklogLoopStart:
        if (self.target_repository is None) != (self.target_issue_number is None):
            raise ValueError("target repository and issue number must be supplied together")
        if self.target_repository is not None and self.maximum_items != 1:
            raise ValueError("a direct issue operation must remain bound to one item")
        if self.runtime_proof == "source-revalidation" and (
            self.delivery_mode != "source-pr" or self.target_issue_number is None
        ):
            # NEW-88: the waiver is per issue and per source-pr operation, never campaign-wide.
            raise ValueError(
                "runtime proof by source revalidation requires one source-pr issue operation"
            )
        return self


class AutonomousBacklogStart(APIModel):
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
    def exact_project_set(self) -> AutonomousBacklogStart:
        if set(self.backlog_campaign_ids) != {"falcone", "musematic", "llmwiki"}:
            raise ValueError("global backlog requires exactly Falcone, Musematic, and LLMWiki")
        if self.policy_campaign_id not in set(self.backlog_campaign_ids.values()):
            raise ValueError("policy campaign must be one of the exact imported inventories")
        return self


class R3IssueApprovalRequest(APIModel):
    project: Literal["falcone", "musematic", "llmwiki"]
    surface: Literal["source", "deployment"]
    issue_number: int = Field(ge=1)
    binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmation_phrase: str = Field(
        pattern=r"^APPROVE_R3_(FALCONE|MUSEMATIC|LLMWIKI)_[0-9]+_[A-F0-9]{16}$"
    )


class LoopPlanDispatch(APIModel):
    project: Project
    campaign_id: UUID


class LoopStartDispatch(APIModel):
    project: Project
    campaign_id: UUID
    maximum_risk: RiskClass
    execution_mode: Literal["plan-only", "source-pr", "preview", "staging", "full-delivery"]
    maximum_items: int = Field(default=1, ge=1, le=50)


class BacklogIssueAction(APIModel):
    repository: Literal["falcone", "falcone-charts", "musematic", "musematic-deploy"]
    issue_number: int = Field(ge=1)
    reason_code: (
        Literal[
            "DUPLICATE",
            "ALREADY_FIXED",
            "SUPERSEDED",
            "NEEDS_PRODUCT_DECISION",
            "BLOCKED_BY_DEPENDENCY",
            "BLOCKED_BY_RUNTIME_BASELINE",
            "R3_REQUIRES_OPERATOR",
            "NOT_REPRODUCIBLE",
            "ENHANCEMENT_DEFERRED",
        ]
        | None
    ) = None


class EnhancementDiscover(APIModel):
    project: Project
    campaign_id: UUID
    dry_run: bool = True


class EnhancementApproval(APIModel):
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")


class EnhancementRejection(APIModel):
    reason: str = Field(min_length=3, max_length=1000)


class EnhancementRun(APIModel):
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    risk: RiskClass
    execution_mode: Literal["plan-only", "source-pr", "preview", "staging"] = "plan-only"
    dry_run: bool = True

    @model_validator(mode="after")
    def approval_still_requires_execute(self) -> EnhancementRun:
        if self.execution_mode != "plan-only" and self.dry_run:
            raise ValueError("enhancement execution requires --execute")
        return self


class ChangeRequestCreate(APIModel):
    """NEW-108: one operator change request; the byte bound is enforced at intake."""

    project: Literal["falcone", "musematic", "llmwiki"]
    description: str = Field(min_length=1, max_length=8192)
    target_repository: (
        Literal[
            "gntik-ai/falcone",
            "gntik-ai/falcone-charts",
            "gntik-ai/musematic",
            "gntik-ai/musematic-deploy",
            "gntik-ai/llmwiki",
        ]
        | None
    ) = None


class ChangeRequestApproval(APIModel):
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    typed_confirmation: str = Field(
        pattern=r"^APPROVE_CHANGE_(FALCONE|MUSEMATIC|LLMWIKI)_[A-F0-9]{16}$"
    )


class ChangeRequestRevision(APIModel):
    note: str = Field(min_length=3, max_length=2000)


class ChangeRequestRejection(APIModel):
    reason: str = Field(min_length=3, max_length=1000)


class ChangeRequestPublication(APIModel):
    """NEW-108: the issue the operator published with their own gh login."""

    issue_url: str = Field(
        pattern=(
            r"^https://github\.com/gntik-ai/"
            r"(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)/issues/[1-9][0-9]*$"
        )
    )
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision_sequence: int = Field(ge=1)


class DeliveryStart(APIModel):
    finding_id: UUID
    risk_class: RiskClass
    dry_run: bool = True


class FindingVerificationStart(APIModel):
    finding_id: UUID


class DeliveryReconcile(APIModel):
    change_set_id: UUID
    candidate_build_run_id: int | None = Field(default=None, ge=1)
    dry_run: bool = True


class ApprovalRecord(APIModel):
    change_set_id: UUID
    artifact_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: str = Field(pattern=r"^(approve|reject)$")
    typed_confirmation: str | None = Field(default=None, max_length=200)


class DeploymentPrepare(APIModel):
    change_set_id: UUID
    environment: str = Field(pattern=r"^(preview|staging)$")
    dry_run: bool = True


class RollbackRequest(APIModel):
    deployment_id: UUID
    expected_artifact_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dry_run: bool = True


class RuntimeVerificationStart(APIModel):
    deployment_id: UUID


class OperationAccepted(APIModel):
    workflow_id: str
    run_id: str
    status: str = "ACCEPTED"


class BacklogImportAccepted(OperationAccepted):
    backlog_campaign_id: UUID


class ChangeRequestAccepted(OperationAccepted):
    change_request_id: UUID
    request_key: str
    revision: int = Field(ge=1)
    redacted: bool = False


class RecordList(APIModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class AcceptanceLifecycleStart(APIModel):
    hold_seconds: int = Field(default=5, ge=1, le=30)


class ObservabilityAcceptanceStart(APIModel):
    confirm_read_only: Literal[True] = True
