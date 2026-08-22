from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from engineering_control.models.enums import Project, RiskClass


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CampaignStart(APIModel):
    project: Project
    name: str = Field(min_length=1, max_length=200)
    source_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    domains: list[str] = Field(min_length=1, max_length=100)
    personas: list[str] = Field(min_length=1, max_length=100)
    lenses: list[str] = Field(min_length=1, max_length=20)
    environments: list[str] = Field(min_length=1, max_length=10)
    dry_run: bool = True


class DeliveryStart(APIModel):
    finding_id: UUID
    risk_class: RiskClass
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


class OperationAccepted(APIModel):
    workflow_id: str
    run_id: str
    status: str = "ACCEPTED"


class RecordList(APIModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class AcceptanceLifecycleStart(APIModel):
    hold_seconds: int = Field(default=5, ge=1, le=30)
