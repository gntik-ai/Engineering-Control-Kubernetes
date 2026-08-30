from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engineering_control.engineering.backlog import DocumentationImpact
from engineering_control.models.enums import Project
from engineering_control.models.reconciliation import DeliveryControlPolicy


class FinalClosureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProtectedPullRequestBinding(FinalClosureModel):
    repository: str = Field(
        pattern=r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$"
    )
    number: int = Field(ge=1)
    url: str = Field(
        pattern=(
            r"^https://github\.com/gntik-ai/"
            r"(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)/pull/[1-9][0-9]*$"
        )
    )
    head_branch: str = Field(
        pattern=r"^agent/(falcone|musematic|llmwiki)/[A-Za-z0-9._-]+/[a-f0-9-]{36}$"
    )
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,120}$")

    @model_validator(mode="after")
    def url_matches_repository_and_number(self) -> ProtectedPullRequestBinding:
        if self.url != f"https://github.com/{self.repository}/pull/{self.number}":
            raise ValueError("protected PR URL does not match its repository and number")
        return self


class FinalClosurePlan(FinalClosureModel):
    version: Literal[1] = 1
    change_set_id: UUID
    run_id: UUID
    workflow_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")
    backlog_campaign_id: UUID
    approval_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    project: Project
    issue_repository: str = Field(pattern=r"^gntik-ai/(falcone|musematic|llmwiki)$")
    issue_number: int = Field(ge=1)
    source: ProtectedPullRequestBinding
    deployment: ProtectedPullRequestBinding
    maker_provider: Literal["codex", "claude"]
    checker_provider: Literal["codex", "claude"]
    documentation_impact: DocumentationImpact
    policy: DeliveryControlPolicy
    production: Literal[False] = False

    @model_validator(mode="after")
    def exact_project_pair_and_independent_review(self) -> FinalClosurePlan:
        pairs = {
            Project.FALCONE: ("gntik-ai/falcone", "gntik-ai/falcone-charts"),
            Project.MUSEMATIC: ("gntik-ai/musematic", "gntik-ai/musematic-deploy"),
            Project.LLMWIKI: ("gntik-ai/llmwiki", "gntik-ai/llmwiki"),
        }
        source, deployment = pairs[self.project]
        if (
            self.issue_repository != source
            or self.source.repository != source
            or self.deployment.repository != deployment
        ):
            raise ValueError("final closure repositories do not match the project pair")
        if self.maker_provider == self.checker_provider:
            raise ValueError("final closure requires opposite native providers")
        if (
            self.documentation_impact
            is DocumentationImpact.BLOCKED_BY_UNRESOLVED_DOCUMENTATION_IMPACT
        ):
            raise ValueError("final closure documentation impact remains unresolved")
        return self


class ProtectedMergeResult(FinalClosureModel):
    repository: str = Field(
        pattern=r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$"
    )
    pull_request_number: int = Field(ge=1)
    pull_request_url: str
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    merge_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    merge_tree_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    state: Literal["MERGED"]

    @model_validator(mode="after")
    def exact_pull_request_url(self) -> ProtectedMergeResult:
        expected = f"https://github.com/{self.repository}/pull/{self.pull_request_number}"
        if self.pull_request_url != expected:
            raise ValueError("protected merge result URL changed")
        return self


class FinalArtifactPlan(FinalClosureModel):
    closure: FinalClosurePlan
    final_source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    policy: DeliveryControlPolicy

    @model_validator(mode="after")
    def unchanged_policy(self) -> FinalArtifactPlan:
        if self.policy != self.closure.policy:
            raise ValueError("final artifact policy changed after source merge")
        return self


class FinalIssueMutationPlan(FinalClosureModel):
    closure: FinalClosurePlan
    final_source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    final_deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_id: UUID
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    comment_body: str = Field(min_length=1, max_length=8_000)

    @model_validator(mode="after")
    def bounded_evidence_comment(self) -> FinalIssueMutationPlan:
        marker = f"<!-- engineering-control-final-closure:{self.closure.change_set_id} -->"
        if marker not in self.comment_body:
            raise ValueError("final evidence comment lacks its idempotency marker")
        return self
