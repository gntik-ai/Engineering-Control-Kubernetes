from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from engineering_control.api.schemas import (
    ApprovalRecord,
    CampaignStart,
    DeliveryStart,
    DeploymentPrepare,
    RuntimeVerificationStart,
)
from engineering_control.db.models import (
    Approval,
    ApprovalCandidate,
    Artifact,
    Campaign,
    ChangeSet,
    Deployment,
    Finding,
    QuotaSnapshot,
    Run,
)
from engineering_control.models.core import ArtifactMetadata
from engineering_control.models.enums import Project


def serialize_row(row: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, UUID):
            result[column.name] = str(value)
        elif isinstance(value, datetime):
            result[column.name] = value.astimezone(UTC).isoformat()
        else:
            result[column.name] = value
    return result


class MetadataRepository(Protocol):
    async def create_campaign(
        self, campaign_id: UUID, workflow_id: str, request: CampaignStart
    ) -> None: ...

    async def create_delivery_run(self, workflow_id: str, request: DeliveryStart) -> None: ...
    async def create_deployment_run(
        self, workflow_id: str, request: DeploymentPrepare
    ) -> Project: ...
    async def create_runtime_verification_run(
        self, workflow_id: str, request: RuntimeVerificationStart
    ) -> Project: ...

    async def list_campaigns(self, limit: int) -> list[dict[str, Any]]: ...
    async def get_finding(self, finding_id: UUID) -> dict[str, Any] | None: ...
    async def list_findings(self, limit: int) -> list[dict[str, Any]]: ...
    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None: ...
    async def get_deployment(self, deployment_id: UUID) -> dict[str, Any] | None: ...
    async def latest_quota_snapshots(self) -> dict[str, dict[str, Any]]: ...
    async def workflow_for_change_set(self, change_set_id: UUID) -> str | None: ...
    async def current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None: ...
    async def persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None: ...
    async def resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]: ...

    async def persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]: ...


class SQLMetadataRepository:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    async def create_campaign(
        self, campaign_id: UUID, workflow_id: str, request: CampaignStart
    ) -> None:
        await asyncio.to_thread(self._create_campaign, campaign_id, workflow_id, request)

    def _create_campaign(self, campaign_id: UUID, workflow_id: str, request: CampaignStart) -> None:
        with self.sessions.begin() as session:
            session.add(
                Campaign(
                    id=campaign_id,
                    project=request.project.value,
                    name=request.name,
                    status="QUEUED",
                    source_sha=request.source_sha,
                    deployment_sha=request.deployment_sha,
                    required_cells=(
                        len(request.domains)
                        * len(request.personas)
                        * len(request.lenses)
                        * len(request.environments)
                    ),
                    metadata_json={"dry_run": request.dry_run},
                )
            )
            session.add(
                Run(
                    campaign_id=campaign_id,
                    workflow_id=workflow_id,
                    run_type="audit-campaign",
                    status="QUEUED",
                    project=request.project.value,
                )
            )

    async def create_delivery_run(self, workflow_id: str, request: DeliveryStart) -> None:
        await asyncio.to_thread(self._create_delivery_run, workflow_id, request)

    def _create_delivery_run(self, workflow_id: str, request: DeliveryStart) -> None:
        with self.sessions.begin() as session:
            finding = session.get(Finding, request.finding_id)
            if finding is None or finding.disposition != "CONFIRMED":
                raise ValueError("delivery requires a CONFIRMED finding")
            session.add(
                Run(
                    workflow_id=workflow_id,
                    run_type="issue-delivery",
                    status="QUEUED",
                    project=finding.project,
                )
            )

    async def create_deployment_run(
        self, workflow_id: str, request: DeploymentPrepare
    ) -> Project:
        return await asyncio.to_thread(self._create_deployment_run, workflow_id, request)

    def _create_deployment_run(self, workflow_id: str, request: DeploymentPrepare) -> Project:
        deployable_states = {
            "preview": {
                "CANDIDATE_BUILT",
                "DEPLOY_PR",
                "PREVIEW_VERIFIED",
            },
            "staging": {
                "DEPLOY_PR",
                "PREVIEW_VERIFIED",
                "SOURCE_MERGED",
                "FINAL_ARTIFACT_BUILT",
            },
        }
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, request.change_set_id)
            if change_set is None:
                raise ValueError("deployment requires an existing ChangeSet")
            if change_set.state not in deployable_states[request.environment]:
                raise ValueError(
                    f"ChangeSet has not reached a deployable {request.environment} state"
                )
            active = session.scalar(
                select(Run).where(
                    Run.change_set_id == change_set.id,
                    Run.run_type == f"{request.environment}-deployment",
                    Run.status.in_({"QUEUED", "RUNNING", "AWAITING_APPROVAL"}),
                )
            )
            if active is not None:
                raise ValueError("an active deployment workflow already owns this ChangeSet target")
            project = Project(change_set.project)
            session.add(
                Run(
                    change_set_id=change_set.id,
                    workflow_id=workflow_id,
                    run_type=f"{request.environment}-deployment",
                    status="QUEUED",
                    project=project.value,
                )
            )
            return project

    async def create_runtime_verification_run(
        self, workflow_id: str, request: RuntimeVerificationStart
    ) -> Project:
        return await asyncio.to_thread(
            self._create_runtime_verification_run, workflow_id, request
        )

    def _create_runtime_verification_run(
        self, workflow_id: str, request: RuntimeVerificationStart
    ) -> Project:
        with self.sessions.begin() as session:
            deployment = session.get(Deployment, request.deployment_id)
            if deployment is None:
                raise ValueError("runtime verification requires an existing deployment")
            if deployment.status not in {"STAGING_DEPLOYED", "VERIFICATION_FAILED"}:
                raise ValueError("deployment has not reached runtime-verifiable state")
            active = session.scalar(
                select(Run).where(
                    Run.change_set_id == deployment.change_set_id,
                    Run.run_type == "runtime-verification",
                    Run.status.in_({"QUEUED", "RUNNING"}),
                )
            )
            if active is not None:
                raise ValueError("an active runtime verification already owns this deployment")
            project = Project(deployment.project)
            session.add(
                Run(
                    change_set_id=deployment.change_set_id,
                    workflow_id=workflow_id,
                    run_type="runtime-verification",
                    status="QUEUED",
                    project=project.value,
                    summary=str(deployment.id),
                )
            )
            return project

    async def list_campaigns(self, limit: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_campaigns, limit)

    def _list_campaigns(self, limit: int) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(Campaign).order_by(Campaign.created_at.desc()).limit(limit)
            ).all()
            return [serialize_row(row) for row in rows]

    async def get_finding(self, finding_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_finding, finding_id)

    def _get_finding(self, finding_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(Finding, finding_id)
            return serialize_row(row) if row else None

    async def list_findings(self, limit: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_findings, limit)

    def _list_findings(self, limit: int) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(Finding).order_by(Finding.created_at.desc()).limit(limit)
            ).all()
            return [serialize_row(row) for row in rows]

    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_artifact, artifact_id)

    def _get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(Artifact, artifact_id)
            return serialize_row(row) if row else None

    async def resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]:
        return await asyncio.to_thread(
            self._resolve_evidence_binding,
            workflow_id,
            project,
            campaign_id,
            change_set_id,
        )

    def _resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]:
        with self.sessions() as session:
            run = session.scalar(
                select(Run)
                .where(Run.workflow_id == workflow_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if run is None or run.project != project.value:
                raise ValueError("evidence workflow binding does not exist for this project")
            if campaign_id is not None and run.campaign_id != campaign_id:
                raise ValueError("evidence campaign does not match its authoritative run")
            if change_set_id is not None and run.change_set_id != change_set_id:
                raise ValueError("evidence ChangeSet does not match its authoritative run")
            return {
                "run_id": run.id,
                "campaign_id": run.campaign_id,
                "change_set_id": run.change_set_id,
            }

    async def persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]:
        return await asyncio.to_thread(self._persist_artifact, metadata)

    def _persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]:
        with self.sessions.begin() as session:
            existing = session.get(Artifact, metadata.artifact_id)
            expected = metadata.model_dump(mode="json")
            if existing is not None:
                serialized = serialize_row(existing)
                comparable = {
                    "id": expected["artifact_id"],
                    "project": expected["project"],
                    "campaign_id": expected["campaign_id"],
                    "change_set_id": expected["change_set_id"],
                    "run_id": expected["run_id"],
                    "relative_path": expected["relative_path"],
                    "sha256": expected["sha256"],
                    "content_type": expected["content_type"],
                    "producer": expected["producer"],
                    "redaction_status": expected["redaction_status"],
                    "size_bytes": expected["size_bytes"],
                }
                if any(serialized.get(key) != value for key, value in comparable.items()):
                    raise ValueError("artifact idempotency key conflicts with stored evidence")
                return serialized
            row = Artifact(
                id=metadata.artifact_id,
                project=metadata.project.value,
                campaign_id=metadata.campaign_id,
                change_set_id=metadata.change_set_id,
                run_id=metadata.run_id,
                relative_path=metadata.relative_path,
                sha256=metadata.sha256,
                content_type=metadata.content_type,
                producer=metadata.producer,
                redaction_status=metadata.redaction_status.value,
                size_bytes=metadata.size_bytes,
                retention_class="standard",
            )
            session.add(row)
            session.flush()
            return serialize_row(row)

    async def get_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_deployment, deployment_id)

    def _get_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            row = session.get(Deployment, deployment_id)
            return serialize_row(row) if row else None

    async def latest_quota_snapshots(self) -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(self._latest_quota_snapshots)

    def _latest_quota_snapshots(self) -> dict[str, dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(QuotaSnapshot).order_by(QuotaSnapshot.captured_at.desc())
            ).all()
            latest: dict[str, dict[str, Any]] = {}
            for row in rows:
                if row.provider not in latest:
                    latest[row.provider] = serialize_row(row)
            return latest

    async def workflow_for_change_set(self, change_set_id: UUID) -> str | None:
        return await asyncio.to_thread(self._workflow_for_change_set, change_set_id)

    def _workflow_for_change_set(self, change_set_id: UUID) -> str | None:
        with self.sessions() as session:
            run = session.scalar(
                select(Run)
                .join(ChangeSet, ChangeSet.id == Run.change_set_id)
                .where(ChangeSet.id == change_set_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            return run.workflow_id if run else None

    async def current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._current_approval_candidate, change_set_id)

    def _current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            candidate = session.scalar(
                select(ApprovalCandidate)
                .where(
                    ApprovalCandidate.change_set_id == change_set_id,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
                .order_by(ApprovalCandidate.created_at.desc())
                .limit(1)
            )
            return serialize_row(candidate) if candidate else None

    async def persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None:
        await asyncio.to_thread(self._persist_approval, request, approver_subject)

    def _persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None:
        with self.sessions.begin() as session:
            candidate = session.scalar(
                select(ApprovalCandidate)
                .where(
                    ApprovalCandidate.change_set_id == request.change_set_id,
                    ApprovalCandidate.invalidated_at.is_(None),
                )
                .with_for_update()
            )
            if candidate is None:
                raise ValueError("approval candidate is missing or invalidated")
            if candidate.artifact_binding_sha256 != request.artifact_binding_sha256:
                raise ValueError("approval artifact binding changed")
            session.add(
                Approval(
                    change_set_id=request.change_set_id,
                    approval_type="staging",
                    decision=request.decision,
                    artifact_binding_sha256=request.artifact_binding_sha256,
                    approver_subject=approver_subject,
                    approved_at=datetime.now(UTC),
                    metadata_json={
                        "candidate_id": str(candidate.id),
                        "typed_confirmation_present": request.typed_confirmation is not None,
                    },
                )
            )


class EmptyMetadataRepository:
    async def create_campaign(
        self, campaign_id: UUID, workflow_id: str, request: CampaignStart
    ) -> None:
        return None

    async def create_delivery_run(self, workflow_id: str, request: DeliveryStart) -> None:
        return None

    async def create_deployment_run(
        self, workflow_id: str, request: DeploymentPrepare
    ) -> Project:
        raise ValueError("deployment metadata store is unavailable")

    async def create_runtime_verification_run(
        self, workflow_id: str, request: RuntimeVerificationStart
    ) -> Project:
        raise ValueError("runtime verification metadata store is unavailable")

    async def list_campaigns(self, limit: int) -> list[dict[str, Any]]:
        return []

    async def get_finding(self, finding_id: UUID) -> dict[str, Any] | None:
        return None

    async def list_findings(self, limit: int) -> list[dict[str, Any]]:
        return []

    async def get_artifact(self, artifact_id: UUID) -> dict[str, Any] | None:
        return None

    async def get_deployment(self, deployment_id: UUID) -> dict[str, Any] | None:
        return None

    async def latest_quota_snapshots(self) -> dict[str, dict[str, Any]]:
        return {}

    async def workflow_for_change_set(self, change_set_id: UUID) -> str | None:
        return None

    async def current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None:
        return None

    async def persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None:
        raise ValueError("approval metadata store is unavailable")

    async def resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]:
        raise ValueError("evidence metadata store is unavailable")

    async def persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]:
        raise ValueError("evidence metadata store is unavailable")
