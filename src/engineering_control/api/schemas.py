from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engineering_control.engineering.backlog import ISSUE_LABEL_PATTERN, valid_issue_label
from engineering_control.models.enums import AuditLens, Project, RiskClass
from engineering_control.projects import (
    GLOBAL_PROJECTS,
    PROJECTS,
    ProjectName,
    RepositoryFullName,
    RepositorySlug,
)

# NEW-157: e.g. FALCONE|MUSEMATIC|LLMWIKI for the typed confirmation phrases.
_PROJECT_PHRASE = "|".join(project.upper() for project in PROJECTS)

CoverageSlug = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9-]{0,119}$"),
]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignReconciliationRow(APIModel):
    campaign_id: UUID
    snapshot_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    observation_token: str = Field(pattern=r"^[a-f0-9]{64}$")


class CampaignReconciliationApply(APIModel):
    confirmation: Literal["APPLY_EXACT_RECONCILIATION_ROWS"]
    rows: list[CampaignReconciliationRow] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_rows(self) -> CampaignReconciliationApply:
        if len({row.campaign_id for row in self.rows}) != len(self.rows):
            raise ValueError("duplicate reconciliation target")
        return self


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
    project: ProjectName
    source: Literal["github-open"] = "github-open"
    import_filter: Literal["bugs", "bugs-and-docs", "security", "enhancements", "all"]
    maximum_items: int = Field(default=500, ge=1, le=500)
    # NEW-215: import only the open issues carrying this one GitHub label.
    issue_label: str | None = Field(default=None, pattern=ISSUE_LABEL_PATTERN)

    @field_validator("issue_label")
    @classmethod
    def _issue_label_is_listable(cls, value: str | None) -> str | None:
        return valid_issue_label(value)


class BacklogApprovalCandidateRequest(APIModel):
    project: ProjectName
    delivery_mode: Literal["source-pr", "full-delivery"]
    maximum_risk: RiskClass
    maximum_items: int = Field(default=1, ge=1, le=50)
    # NEW-214b: the approved loop concurrency (1, or 2 to pipeline issues).
    maximum_concurrency: Literal[1, 2, 3] = 1


class BacklogApprovalRequest(APIModel):
    project: ProjectName
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    typed_confirmation: Literal["APPROVE_EXISTING_GITHUB_BACKLOG_LOOP"]


class BacklogOwnershipReleaseRequest(APIModel):
    project: ProjectName
    typed_confirmation: Literal["RELEASE_GITHUB_WRITE_POLICY_OWNERSHIP"]


class ExistingBacklogLoopStart(APIModel):
    project: ProjectName
    backlog_campaign_id: UUID
    maximum_risk: RiskClass
    delivery_mode: Literal["source-pr", "full-delivery"]
    maximum_items: int = Field(default=1, ge=1, le=50)
    target_repository: RepositoryFullName | None = None
    target_issue_number: int | None = Field(default=None, ge=1)
    continue_after_blocked_issue: bool = True
    correction_limit: int = Field(default=2, ge=0, le=5)
    global_campaign_id: UUID | None = None
    r3_approval_binding: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    runtime_proof: Literal["runtime", "source-revalidation"] = "runtime"
    issue_operation_id: UUID | None = None
    issue_approval_binding: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    maximum_concurrency: Literal[1, 2, 3] = 1

    @model_validator(mode="after")
    def exact_issue_binding(self) -> ExistingBacklogLoopStart:
        if self.maximum_concurrency != 1 and (
            self.target_repository is not None
            or self.global_campaign_id is not None
            or self.delivery_mode != "full-delivery"
        ):
            # NEW-214b: pipelining is for campaign full-delivery loops only.
            raise ValueError("concurrency above 1 needs a full-delivery campaign loop")
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
        if (self.issue_operation_id is None) != (self.issue_approval_binding is None):
            raise ValueError("issue operation and approval binding must be supplied together")
        if self.issue_operation_id is not None and self.target_repository is None:
            raise ValueError("issue approval must bind one exact issue")
        # NEW-95 (main): an R3 issue operation carries its exact R3 approval binding; it is
        # part of the operation's approved payload, so it does not widen the scope.
        if self.issue_operation_id is not None and (
            self.global_campaign_id is not None or self.continue_after_blocked_issue
        ):
            raise ValueError("direct issue approval cannot widen its execution scope")
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
        if set(self.backlog_campaign_ids) != set(GLOBAL_PROJECTS):
            raise ValueError("global backlog requires exactly Falcone, Musematic, and LLMWiki")
        if self.policy_campaign_id not in set(self.backlog_campaign_ids.values()):
            raise ValueError("policy campaign must be one of the exact imported inventories")
        return self


class R3IssueApprovalRequest(APIModel):
    project: ProjectName
    surface: Literal["source", "deployment"]
    issue_number: int = Field(ge=1)
    binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmation_phrase: str = Field(
        pattern=rf"^APPROVE_R3_({_PROJECT_PHRASE})_[0-9]+_[A-F0-9]{{16}}$"
    )


class CorrectionExtensionRequest(APIModel):
    """NEW-133: grant one extra correction attempt to a validation-blocked ChangeSet."""

    loop_workflow_id: str = Field(pattern=r"^backlog-loop-[A-Za-z0-9._:-]{1,227}$")
    project: ProjectName
    issue_number: int = Field(ge=1, le=2_147_483_647)
    change_set_id: UUID
    confirmation_phrase: str = Field(pattern=r"^EXTEND_CORRECTION_ONCE_[A-F0-9]{8}$")


class IssueReapprovalRequest(APIModel):
    """NEW-143: re-create a voided per-issue approval for the loop's existing operation."""

    loop_workflow_id: str = Field(pattern=r"^backlog-loop-[A-Za-z0-9._:-]{1,227}$")
    project: ProjectName
    repository: RepositorySlug
    issue_number: int = Field(ge=1, le=2_147_483_647)
    confirmation_phrase: str = Field(pattern=r"^REAPPROVE_ISSUE_[A-F0-9]{8}$")


class ChangeSetResetRequest(APIModel):
    """NEW-168: reset a never-published ChangeSet of a paused issue-start loop."""

    loop_workflow_id: str = Field(pattern=r"^backlog-loop-[A-Za-z0-9._:-]{1,227}$")
    project: ProjectName
    repository: RepositorySlug
    issue_number: int = Field(ge=1, le=2_147_483_647)
    change_set_id: UUID
    confirmation_phrase: str = Field(pattern=r"^RESET_CHANGESET_[A-F0-9]{8}$")


class PublicationReconcileRequest(APIModel):
    """NEW-145: reconcile one uncertain publication effect against the live PR heads."""

    effect_id: UUID
    confirmation_phrase: str = Field(pattern=r"^RECONCILE_PUBLICATION_[A-F0-9]{8}$")


class IssueAmendRequest(APIModel):
    """NEW-115: preview (no confirm) or confirm one backlog issue body amendment."""

    project: ProjectName
    repository: RepositorySlug
    issue_number: int = Field(ge=1)
    confirm: str | None = Field(default=None, pattern=r"^[a-f0-9]{12,64}$")
    retry: bool = False

    @model_validator(mode="after")
    def retry_requires_confirmation(self) -> IssueAmendRequest:
        if self.retry and self.confirm is None:
            raise ValueError("retry is sent only after a confirmed amendment")
        return self


class IssueTouchRefreshRequest(APIModel):
    """NEW-150: preview (no confirm) or confirm one issue revision-time refresh."""

    project: ProjectName
    repository: RepositorySlug
    issue_number: int = Field(ge=1, le=2_147_483_647)
    confirm: str | None = Field(default=None, pattern=r"^[a-f0-9]{12,64}$")


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

    project: ProjectName
    description: str = Field(min_length=1, max_length=8192)
    target_repository: (
        Literal[
            "gntik-ai/falcone",
            "gntik-ai/falcone-charts",
            "gntik-ai/musematic",
            "gntik-ai/musematic-deploy",
            "gntik-ai/llmwiki",
            "gntik-ai/gntik-ui",
            "gntik-ai/falcone-serverless",
        ]
        | None
    ) = None


class ChangeRequestApproval(APIModel):
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    typed_confirmation: str = Field(pattern=rf"^APPROVE_CHANGE_({_PROJECT_PHRASE})_[A-F0-9]{{16}}$")


class UiPlanCreate(APIModel):
    """NEW-123: start flow A (the UI migration blueprint) for one configured project."""

    project: Literal["falcone"]
    # Optional expected legacy main head; the snapshot refuses any other commit.
    source_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")


class UiBlueprintApproval(APIModel):
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    typed_confirmation: Literal["APPROVE_UI_BLUEPRINT"]
    # Fan-out cap for this run; bounded by the configured maximum (default 80).
    max_items: int | None = Field(default=None, ge=1, le=600)


class DecompositionCreate(APIModel):
    """NEW-234 PR5: start a backlog decomposition plan for one configured project."""

    project: Literal["falcone-serverless"]
    # Optional expected project main head; the snapshot refuses any other commit.
    source_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    # Optional epic scope (ids such as EP-API or numbers); default every epic.
    epics: list[Annotated[str, Field(pattern=r"^(?:EP-[A-Z]{2,4}|[1-9][0-9]?)$")]] | None = Field(
        default=None, min_length=1, max_length=20
    )


class DecompositionApproval(APIModel):
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    typed_confirmation: Literal["APPROVE_DECOMPOSITION"]


class UiFanoutRetry(APIModel):
    """NEW-146: reset an approved blueprint's FAILED fan-out items and fan out again."""

    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    typed_confirmation: Literal["RETRY_UI_FANOUT"]
    kind: Literal["epic", "page", "cutover", "gap", "backend-gap", "kit-gap"] | None = None
    # Items reset and the fan-out cap of this run; bounded by the configured maximum.
    max_items: int | None = Field(default=None, ge=1, le=600)


class UiGapConsolidationRequest(APIModel):
    """NEW-154: preview (plan) a consolidation of a blueprint's duplicate gap requests."""

    kind: Literal["kit-gap", "backend-gap"] | None = None
    # Plan again although a plan for the same inputs exists (it is superseded).
    replan: bool = False


class UiGapConsolidationApply(APIModel):
    """NEW-154: apply a stored consolidation plan, bound to its sha."""

    plan_sha12: str = Field(pattern=r"^[a-f0-9]{12}$")
    typed_confirmation: Literal["CONSOLIDATE_GAPS"]


class ChangeRequestRevision(APIModel):
    note: str = Field(min_length=3, max_length=2000)


class ChangeRequestRejection(APIModel):
    reason: str = Field(min_length=3, max_length=1000)


class ChangeRequestPublication(APIModel):
    """NEW-108: the issue the operator published with their own gh login."""

    issue_url: str = Field(
        pattern=(
            r"^https://github\.com/gntik-ai/"
            r"(falcone|falcone-charts|musematic|musematic-deploy|llmwiki|gntik-ui"
            r"|falcone-serverless)"
            r"/issues/[1-9][0-9]*$"
        )
    )
    approval_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision_sequence: int = Field(ge=1)


class DeliveryStart(APIModel):
    finding_id: UUID
    risk_class: RiskClass
    dry_run: bool = True
    approved_issue: ExistingBacklogLoopStart | None = None

    @model_validator(mode="after")
    def real_delivery_has_exact_origin(self) -> DeliveryStart:
        if self.dry_run:
            if self.approved_issue is not None:
                raise ValueError("dry-run delivery must not carry a mutating issue request")
            return self
        request = self.approved_issue
        if request is None or request.issue_operation_id is None:
            raise ValueError("real delivery requires its approved typed issue operation")
        if request.delivery_mode != "source-pr":
            raise ValueError("delivery publishes source PRs; full delivery uses solve-issue")
        return self


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


class UiPlanAccepted(OperationAccepted):
    blueprint_id: UUID


class DecompositionAccepted(OperationAccepted):
    decomposition_id: UUID


class RecordList(APIModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class AcceptanceLifecycleStart(APIModel):
    hold_seconds: int = Field(default=5, ge=1, le=30)


class ObservabilityAcceptanceStart(APIModel):
    confirm_read_only: Literal[True] = True
