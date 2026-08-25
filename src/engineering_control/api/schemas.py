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


class RecordList(APIModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class AcceptanceLifecycleStart(APIModel):
    hold_seconds: int = Field(default=5, ge=1, le=30)
