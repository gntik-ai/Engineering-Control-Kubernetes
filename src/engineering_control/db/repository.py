from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from engineering_control.api.schemas import ApprovalRecord, CampaignStart, DeliveryStart
from engineering_control.db.models import (
    Approval,
    ApprovalCandidate,
    Campaign,
    ChangeSet,
    Finding,
    Run,
)


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

    async def list_campaigns(self, limit: int) -> list[dict[str, Any]]: ...
    async def get_finding(self, finding_id: UUID) -> dict[str, Any] | None: ...
    async def list_findings(self, limit: int) -> list[dict[str, Any]]: ...
    async def workflow_for_change_set(self, change_set_id: UUID) -> str | None: ...
    async def current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None: ...
    async def persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None: ...


class SQLMetadataRepository:
    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 5},
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

    async def list_campaigns(self, limit: int) -> list[dict[str, Any]]:
        return []

    async def get_finding(self, finding_id: UUID) -> dict[str, Any] | None:
        return None

    async def list_findings(self, limit: int) -> list[dict[str, Any]]:
        return []

    async def workflow_for_change_set(self, change_set_id: UUID) -> str | None:
        return None

    async def current_approval_candidate(self, change_set_id: UUID) -> dict[str, Any] | None:
        return None

    async def persist_approval(self, request: ApprovalRecord, approver_subject: str) -> None:
        raise ValueError("approval metadata store is unavailable")
