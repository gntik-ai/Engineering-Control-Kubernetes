from __future__ import annotations

from typing import Annotated, Any
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
    name: str = Field(min_length=1, max_length=200)
    source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    domains: list[CoverageSlug] = Field(min_length=1, max_length=100)
    personas: list[CoverageSlug] = Field(min_length=1, max_length=100)
    lenses: list[AuditLens] = Field(min_length=1, max_length=20)
    environments: list[CoverageSlug] = Field(min_length=1, max_length=10)
    dry_run: bool = True

    @model_validator(mode="after")
    def bounded_coverage_matrix(self) -> CampaignStart:
        cells = len(self.domains) * len(self.personas) * len(self.lenses) * len(self.environments)
        if cells > 100_000:
            raise ValueError("coverage matrix exceeds the 100000-cell safety bound")
        return self


class DeliveryStart(APIModel):
    finding_id: UUID
    risk_class: RiskClass
    dry_run: bool = True


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
