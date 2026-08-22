from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from temporalio.exceptions import ApplicationError

from engineering_control.config import Settings
from engineering_control.db.models import Campaign, CoverageCell, Finding, QuotaSnapshot
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import AuditLens, ModelClass, Project, Provider, RiskClass
from engineering_control.quota.broker import QuotaBroker
from engineering_control.quota.broker import QuotaSnapshot as BrokerQuotaSnapshot


class QuotaSnapshotRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    provider: Provider
    captured_at: datetime
    remaining_capacity: float | None = Field(default=None, ge=0)
    total_capacity: float | None = Field(default=None, ge=0)
    reset_at: datetime | None = None
    running_jobs: int = Field(default=0, ge=0)
    rate_limited: bool = False
    source: str = Field(min_length=1, max_length=80)


class CampaignCellRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    persona: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    lens: AuditLens
    environment: str = Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")


class FindingCandidateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: str = Field(pattern=r"^(low|medium|high|critical)$")
    confidence: float = Field(ge=0, le=1)
    expected_behavior: str = Field(min_length=1, max_length=20_000)
    actual_behavior: str = Field(min_length=1, max_length=20_000)
    minimal_reproduction: list[str] = Field(min_length=1, max_length=50)
    affected_component: str = Field(min_length=1, max_length=200)
    security_impact: str | None = Field(default=None, max_length=20_000)
    data_impact: str | None = Field(default=None, max_length=20_000)
    recommended_action: str = Field(min_length=1, max_length=20_000)


class MetadataActivityHandler:
    """Controller-side authoritative metadata writes for idempotent activity results."""

    supported_steps = frozenset(
        {
            "audit.prepare_agent_cell",
            "campaign.persist_coverage",
            "campaign.persist_cell_result",
            "campaign.finalize",
            "quota.persist",
        }
    )

    def __init__(self, settings: Settings) -> None:
        database_url = settings.database_url.get_secret_value()
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=2,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if step == "audit.prepare_agent_cell":
                return await asyncio.to_thread(self._prepare_agent_cell, payload)
            if step == "campaign.persist_coverage":
                return await asyncio.to_thread(self._persist_coverage, payload)
            if step == "campaign.persist_cell_result":
                return await asyncio.to_thread(self._persist_cell_result, payload)
            if step == "campaign.finalize":
                return await asyncio.to_thread(self._finalize_campaign, payload)
            if step == "quota.persist":
                return await self._quota(payload)
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise ApplicationError(
                f"invalid metadata activity input for {step}",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        raise ApplicationError("unsupported metadata activity", non_retryable=True)

    async def _quota(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_snapshots = payload.get("snapshots")
        if not isinstance(raw_snapshots, list):
            raise ApplicationError(
                "quota persistence requires snapshot records",
                type="PolicyViolation",
                non_retryable=True,
            )
        try:
            snapshots = [QuotaSnapshotRecord.model_validate(item) for item in raw_snapshots]
        except ValidationError as error:
            raise ApplicationError(
                "quota snapshot failed strict validation",
                type="PolicyViolation",
                non_retryable=True,
            ) from error
        await asyncio.to_thread(self._persist_quota, snapshots)
        return {**payload, "status": "PERSISTED", "persisted_snapshots": len(snapshots)}

    def _persist_coverage(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        raw_cells = payload.get("cells")
        total = int(payload["total"])
        if not isinstance(raw_cells, list) or total < len(raw_cells) or total < 1:
            raise ValueError("coverage batch and total are inconsistent")
        cells = [CampaignCellRecord.model_validate(item) for item in raw_cells]
        with self.sessions.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise ValueError("campaign does not exist")
            campaign.required_cells = total
            campaign.status = "IN_PROGRESS"
            for cell in cells:
                identity = self._cell_id(campaign_id, cell)
                if session.get(CoverageCell, identity) is not None:
                    continue
                session.add(
                    CoverageCell(
                        id=identity,
                        campaign_id=campaign_id,
                        domain=cell.domain,
                        persona=cell.persona,
                        lens=cell.lens.value,
                        environment=cell.environment,
                        status="NOT_STARTED",
                        blocker=None,
                        exclusion_approval_id=None,
                        evidence_json=[],
                        result_json={},
                    )
                )
        return {**payload, "status": "COVERAGE_PERSISTED", "persisted_cells": len(cells)}

    def _prepare_agent_cell(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        cell = CampaignCellRecord.model_validate(payload["cell"])
        snapshot = self._object(payload.get("snapshot"), "snapshot")
        deterministic = self._object(payload.get("deterministic"), "deterministic")
        project = Project(str(snapshot["project"]))
        risk = (
            RiskClass.R3
            if cell.lens
            in {AuditLens.SECURITY, AuditLens.AUTHORIZATION, AuditLens.TENANT_ISOLATION}
            else RiskClass.R1
        )
        role = "security-auditor" if risk is RiskClass.R3 else "domain-auditor"
        model_class = ModelClass.JUDGE if risk is RiskClass.R3 else ModelClass.ROUTINE
        broker = self._quota_broker()
        decision = broker.route(role=role, model_class=model_class, risk_class=risk)
        if decision.primary is None:
            return {
                **payload,
                "status": decision.status,
                "routing_reason": decision.reason,
            }
        request_id = uuid5(
            NAMESPACE_URL,
            "audit-agent:"
            f"{campaign_id}:{cell.domain}:{cell.persona}:{cell.lens.value}:{cell.environment}",
        )
        prompt = (
            "Audit exactly one immutable coverage cell. Do not modify files. "
            "Use the deterministic evidence supplied below and inspect only the bounded source "
            "and deployment worktrees. Report PASS only with evidence. Report FAIL only with a "
            "minimal reproducible finding.\n\n"
            f"Project: {project.value}\nDomain: {cell.domain}\nPersona: {cell.persona}\n"
            f"Lens: {cell.lens.value}\nEnvironment: {cell.environment}\n"
            f"Source SHA: {snapshot['source_sha']}\n"
            f"Deployment SHA: {snapshot['deployment_sha']}\n"
            f"Deployment worktree: {snapshot['deployment_worktree']}\n"
            f"Deterministic evidence SHA256: {deterministic['evidence_sha256']}\n"
        )
        request = AgentRequest(
            run_id=request_id,
            project=project,
            role=role,
            provider=decision.primary,
            model_class=model_class,
            prompt=prompt,
            working_directory=str(snapshot["source_worktree"]),
            output_schema=self._audit_output_schema(),
            timeout_seconds=3600,
            effort="high" if risk is RiskClass.R3 else "medium",
            writable=False,
            campaign_id=campaign_id,
        )
        return {
            **payload,
            "provider": decision.primary.value,
            "routing_reason": decision.reason,
            "agent_request": request.model_dump(mode="json"),
            "status": "READY",
        }

    def _persist_cell_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        cell = CampaignCellRecord.model_validate(payload["cell"])
        agent_result = self._object(payload.get("agent_result"), "agent_result")
        structured = self._object(agent_result.get("structured_result"), "structured_result")
        decision = str(structured.get("decision", ""))
        evidence = structured.get("evidence")
        if decision not in {"PASS", "FAIL", "BLOCKED", "REQUEST_CHANGES"}:
            raise ValueError("audit result has an unknown decision")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("audit result requires explicit evidence")
        deterministic_sha = str(payload.get("deterministic_evidence_sha256", ""))
        if len(deterministic_sha) != 64 or not any(
            isinstance(item, dict) and item.get("sha256") == deterministic_sha for item in evidence
        ):
            raise ValueError("audit result does not bind to deterministic evidence")
        status = {
            "PASS": "NO_FINDING",
            "FAIL": "CANDIDATE_FOUND",
            "BLOCKED": "BLOCKED",
            "REQUEST_CHANGES": "RETEST_REQUIRED",
        }[decision]
        blocker = str(structured.get("summary", "")) if decision == "BLOCKED" else None
        finding_id: UUID | None = None
        fingerprint: str | None = None
        with self.sessions.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            coverage = session.get(CoverageCell, self._cell_id(campaign_id, cell))
            if campaign is None or coverage is None:
                raise ValueError("campaign coverage cell does not exist")
            coverage.status = status
            coverage.blocker = blocker
            coverage.evidence_json = evidence
            coverage.result_json = {
                "provider": agent_result.get("provider"),
                "run_id": agent_result.get("run_id"),
                "summary": structured.get("summary"),
                "decision": decision,
                "open_questions": structured.get("open_questions", []),
            }
            if decision == "FAIL":
                candidate = FindingCandidateRecord.model_validate(structured.get("finding"))
                fingerprint = self._fingerprint(
                    campaign,
                    cell,
                    candidate.affected_component,
                    candidate.minimal_reproduction,
                )
                existing = session.scalar(select(Finding).where(Finding.fingerprint == fingerprint))
                finding_id = existing.id if existing else uuid5(NAMESPACE_URL, fingerprint)
                if existing is None:
                    risk = (
                        "R3"
                        if cell.lens
                        in {
                            AuditLens.SECURITY,
                            AuditLens.AUTHORIZATION,
                            AuditLens.TENANT_ISOLATION,
                        }
                        else "R2"
                    )
                    session.add(
                        Finding(
                            id=finding_id,
                            campaign_id=campaign_id,
                            coverage_cell_id=coverage.id,
                            fingerprint=fingerprint,
                            project=campaign.project,
                            domain=cell.domain,
                            persona=cell.persona,
                            severity=candidate.severity,
                            confidence=candidate.confidence,
                            risk_class=risk,
                            disposition="CANDIDATE",
                            expected_behavior=candidate.expected_behavior,
                            actual_behavior=candidate.actual_behavior,
                            evidence_json=evidence,
                            minimal_reproduction=candidate.minimal_reproduction,
                            affected_source_sha=campaign.source_sha,
                            affected_deployment_sha=campaign.deployment_sha,
                            affected_image_digest=None,
                            affected_component=candidate.affected_component,
                            impact_json={
                                "security": candidate.security_impact,
                                "data": candidate.data_impact,
                            },
                            recommended_action=candidate.recommended_action,
                        )
                    )
        return {
            **payload,
            "status": status,
            "finding_id": str(finding_id) if finding_id else None,
            "fingerprint": fingerprint,
        }

    def _finalize_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        expected = int(payload["expected_cells"])
        with self.sessions.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise ValueError("campaign does not exist")
            cells = session.scalars(
                select(CoverageCell).where(CoverageCell.campaign_id == campaign_id)
            ).all()
            if len(cells) != expected or campaign.required_cells != expected:
                campaign.status = "INCOMPLETE"
            elif any(cell.status in {"CANDIDATE_FOUND", "RETEST_REQUIRED"} for cell in cells):
                campaign.status = "VERIFICATION_REQUIRED"
            elif all(
                cell.exclusion_approval_id is not None
                or (cell.status == "BLOCKED" and bool(cell.blocker))
                or (
                    cell.status in {"NO_FINDING", "CONFIRMED_FINDING", "VERIFIED_FIXED"}
                    and bool(cell.evidence_json)
                )
                for cell in cells
            ):
                campaign.status = "COMPLETE"
            else:
                campaign.status = "INCOMPLETE"
            status = campaign.status
        return {**payload, "status": status, "completed_cells": len(cells)}

    def _persist_quota(self, snapshots: list[QuotaSnapshotRecord]) -> None:
        for snapshot in snapshots:
            try:
                with self.sessions.begin() as session:
                    captured = snapshot.captured_at.astimezone(UTC)
                    identity = uuid5(
                        NAMESPACE_URL,
                        f"engineering-quota:{snapshot.provider.value}:{captured.isoformat()}",
                    )
                    if session.get(QuotaSnapshot, identity) is not None:
                        continue
                    session.add(
                        QuotaSnapshot(
                            id=identity,
                            provider=snapshot.provider.value,
                            captured_at=captured,
                            remaining_capacity=snapshot.remaining_capacity,
                            total_capacity=snapshot.total_capacity,
                            reset_at=(
                                snapshot.reset_at.astimezone(UTC) if snapshot.reset_at else None
                            ),
                            running_jobs=snapshot.running_jobs,
                            rate_limited=snapshot.rate_limited,
                            source=snapshot.source,
                        )
                    )
            except IntegrityError:
                # A concurrent retry may have committed the deterministic primary key first.
                continue

    def _quota_broker(self) -> QuotaBroker:
        broker = QuotaBroker()
        with self.sessions() as session:
            rows = session.scalars(
                select(QuotaSnapshot).order_by(QuotaSnapshot.captured_at.desc())
            ).all()
        seen: set[str] = set()
        for row in rows:
            if row.provider in seen:
                continue
            seen.add(row.provider)
            broker.record(
                BrokerQuotaSnapshot(
                    provider=Provider(row.provider),
                    captured_at=row.captured_at,
                    remaining_capacity=row.remaining_capacity,
                    total_capacity=row.total_capacity,
                    reset_at=row.reset_at,
                    running_jobs=row.running_jobs,
                    rate_limited=row.rate_limited,
                    source=row.source,
                )
            )
        return broker

    @staticmethod
    def _cell_id(campaign_id: UUID, cell: CampaignCellRecord) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            "coverage-cell:"
            f"{campaign_id}:{cell.domain}:{cell.persona}:{cell.lens.value}:{cell.environment}",
        )

    @staticmethod
    def _object(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        return value

    @staticmethod
    def _fingerprint(
        campaign: Campaign,
        cell: CampaignCellRecord,
        component: str,
        reproduction: list[str],
    ) -> str:
        canonical = json.dumps(
            {
                "project": campaign.project,
                "domain": cell.domain,
                "persona": cell.persona,
                "lens": cell.lens.value,
                "component": component,
                "reproduction": reproduction,
                "source_sha": campaign.source_sha,
                "deployment_sha": campaign.deployment_sha,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _audit_output_schema() -> dict[str, Any]:
        finding = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "severity",
                "confidence",
                "expected_behavior",
                "actual_behavior",
                "minimal_reproduction",
                "affected_component",
                "recommended_action",
            ],
            "properties": {
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "expected_behavior": {"type": "string", "minLength": 1, "maxLength": 20000},
                "actual_behavior": {"type": "string", "minLength": 1, "maxLength": 20000},
                "minimal_reproduction": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
                "affected_component": {"type": "string", "minLength": 1, "maxLength": 200},
                "security_impact": {"type": ["string", "null"], "maxLength": 20000},
                "data_impact": {"type": ["string", "null"], "maxLength": 20000},
                "recommended_action": {"type": "string", "minLength": 1, "maxLength": 20000},
            },
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "decision", "evidence", "open_questions", "finding"],
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 20000},
                "decision": {
                    "type": "string",
                    "enum": ["PASS", "FAIL", "BLOCKED", "REQUEST_CHANGES"],
                },
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "reference", "sha256"],
                        "properties": {
                            "kind": {"type": "string", "minLength": 1, "maxLength": 100},
                            "reference": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                        },
                    },
                },
                "open_questions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4000},
                },
                "finding": {"anyOf": [{"type": "null"}, finding]},
            },
            "allOf": [
                {
                    "if": {"properties": {"decision": {"const": "FAIL"}}},
                    "then": {"properties": {"finding": finding}},
                }
            ],
        }
