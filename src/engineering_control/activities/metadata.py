from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import CommandFailure, SafeCommandRunner
from engineering_control.config import Settings
from engineering_control.db.models import (
    Campaign,
    ChangeSet,
    CoverageCell,
    Finding,
    FindingVerification,
    Issue,
    QuotaSnapshot,
    Run,
    WorktreeLease,
)
from engineering_control.leases.component import ComponentLeaseConflict, ComponentLeaseManager
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
            "finding.load_candidate",
            "finding.prepare_verifier",
            "finding.controls",
            "finding.dedupe",
            "finding.finalize",
            "delivery.load_confirmed_finding",
            "delivery.ensure_issue",
            "delivery.acquire_leases",
            "delivery.route_providers",
            "delivery.persist_worktree_leases",
            "delivery.prepare_spec",
            "delivery.create_openspec",
            "delivery.prepare_maker",
            "delivery.prepare_deployment_maker",
            "delivery.prepare_checker",
            "delivery.persist_handoff",
            "quota.persist",
        }
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
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
            if step == "finding.load_candidate":
                return await asyncio.to_thread(self._load_candidate, payload)
            if step == "finding.prepare_verifier":
                return await asyncio.to_thread(self._prepare_verifier, payload)
            if step == "finding.controls":
                return await asyncio.to_thread(self._evaluate_controls, payload)
            if step == "finding.dedupe":
                return await asyncio.to_thread(self._dedupe_finding, payload)
            if step == "finding.finalize":
                return await asyncio.to_thread(self._finalize_finding, payload)
            if step == "delivery.load_confirmed_finding":
                return await asyncio.to_thread(self._load_confirmed_finding, payload)
            if step == "delivery.ensure_issue":
                return await asyncio.to_thread(self._ensure_issue, payload)
            if step == "delivery.acquire_leases":
                try:
                    return await asyncio.to_thread(self._acquire_delivery_leases, payload)
                except ComponentLeaseConflict:
                    return {
                        **payload,
                        "status": "BLOCKED",
                        "blocker": "COMPONENT_LEASE_CONFLICT",
                    }
            if step == "delivery.route_providers":
                return await asyncio.to_thread(self._route_delivery, payload)
            if step == "delivery.persist_worktree_leases":
                return await asyncio.to_thread(self._persist_worktree_leases, payload)
            if step == "delivery.prepare_spec":
                return self._prepare_spec(payload)
            if step == "delivery.create_openspec":
                return self._create_openspec(payload)
            if step == "delivery.prepare_maker":
                return self._prepare_maker(payload, deployment=False)
            if step == "delivery.prepare_deployment_maker":
                return self._prepare_maker(payload, deployment=True)
            if step == "delivery.prepare_checker":
                return self._prepare_checker(payload)
            if step == "delivery.persist_handoff":
                return await asyncio.to_thread(self._persist_delivery_handoff, payload)
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

    def _load_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding_id = UUID(str(payload["finding_id"]))
        with self.sessions() as session:
            finding = session.get(Finding, finding_id)
            if finding is None or finding.disposition != "CANDIDATE":
                raise ValueError("verification requires a CANDIDATE finding")
            coverage = session.get(CoverageCell, finding.coverage_cell_id)
            if coverage is None:
                raise ValueError("finding coverage metadata is missing")
            discoverer = coverage.result_json.get("provider")
            if discoverer not in {Provider.CODEX.value, Provider.CLAUDE.value}:
                raise ValueError("finding discoverer provider is missing")
            return {
                **payload,
                "status": "CANDIDATE_LOADED",
                "finding_id": str(finding.id),
                "campaign_id": str(finding.campaign_id),
                "coverage_cell_id": str(finding.coverage_cell_id),
                "fingerprint": finding.fingerprint,
                "project": finding.project,
                "domain": finding.domain,
                "persona": finding.persona,
                "severity": finding.severity,
                "confidence": finding.confidence,
                "risk_class": finding.risk_class,
                "expected_behavior": finding.expected_behavior,
                "actual_behavior": finding.actual_behavior,
                "minimal_reproduction": finding.minimal_reproduction,
                "affected_source_sha": finding.affected_source_sha,
                "affected_deployment_sha": finding.affected_deployment_sha,
                "affected_component": finding.affected_component,
                "discoverer_provider": discoverer,
            }

    def _prepare_verifier(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding = self._object(payload.get("finding"), "finding")
        snapshot = self._object(payload.get("snapshot"), "snapshot")
        ordinal = int(payload["ordinal"])
        if ordinal not in {1, 2}:
            raise ValueError("verification ordinal must be one or two")
        maker = Provider(str(finding["discoverer_provider"]))
        risk = RiskClass(str(finding["risk_class"]))
        routing = self._quota_broker().route(
            role="finding-verifier",
            model_class=ModelClass.JUDGE,
            risk_class=risk,
            maker=maker,
        )
        if routing.primary is None:
            return {**payload, "status": routing.status, "routing_reason": routing.reason}
        run_id = uuid5(
            NAMESPACE_URL,
            f"finding-verifier:{finding['finding_id']}:{snapshot['source_sha']}:{ordinal}",
        )
        prompt = (
            "Independently verify one candidate finding from a fresh immutable snapshot. "
            "Do not modify files. Reproduce only the supplied minimal steps, establish a "
            "positive and a negative control, and reject environment failures. Do not file an "
            "issue.\n\n"
            f"Finding fingerprint: {finding['fingerprint']}\n"
            f"Expected: {finding['expected_behavior']}\n"
            f"Actual: {finding['actual_behavior']}\n"
            f"Minimal reproduction: {json.dumps(finding['minimal_reproduction'])}\n"
            f"Affected component: {finding['affected_component']}\n"
            f"Source SHA: {snapshot['source_sha']}\n"
            f"Deployment SHA: {snapshot['deployment_sha']}\n"
            f"Deployment worktree: {snapshot['deployment_worktree']}\n"
            f"Fresh snapshot evidence SHA256: {snapshot['snapshot_evidence_sha256']}\n"
            f"Independent reproduction ordinal: {ordinal}\n"
        )
        request = AgentRequest(
            run_id=run_id,
            project=Project(str(finding["project"])),
            role="finding-verifier",
            provider=routing.primary,
            model_class=ModelClass.JUDGE,
            prompt=prompt,
            working_directory=str(snapshot["source_worktree"]),
            output_schema=self._verification_output_schema(),
            timeout_seconds=3600,
            effort="high",
            writable=False,
            campaign_id=UUID(str(finding["campaign_id"])),
        )
        return {
            **payload,
            "provider": routing.primary.value,
            "routing_reason": routing.reason,
            "verifier_request": request.model_dump(mode="json"),
            "status": "READY",
        }

    def _evaluate_controls(self, payload: dict[str, Any]) -> dict[str, Any]:
        attempts = payload.get("attempts")
        snapshot = self._object(payload.get("snapshot"), "snapshot")
        if not isinstance(attempts, list) or len(attempts) != 2:
            raise ValueError("finding verification requires exactly two attempts")
        results: list[dict[str, Any]] = []
        anchor = str(snapshot["snapshot_evidence_sha256"])
        for attempt in attempts:
            outer = self._object(attempt, "attempt")
            agent = self._object(outer.get("agent_result"), "agent_result")
            structured = self._object(agent.get("structured_result"), "structured_result")
            evidence = structured.get("evidence")
            if not isinstance(evidence, list) or not any(
                isinstance(item, dict) and item.get("sha256") == anchor for item in evidence
            ):
                raise ValueError("verification attempt is not bound to fresh snapshot evidence")
            results.append(structured)
        environment_valid = all(bool(item.get("environment_valid")) for item in results)
        reproduced = [bool(item.get("reproduced")) for item in results]
        positive = all(bool(item.get("positive_control")) for item in results)
        negative = all(bool(item.get("negative_control")) for item in results)
        if not environment_valid:
            disposition = "BLOCKED"
        elif reproduced == [True, True] and positive and negative:
            disposition = "CONFIRMED"
        elif reproduced == [False, False]:
            disposition = "REJECTED"
        else:
            disposition = "FLAKY"
        return {
            **payload,
            "status": disposition,
            "disposition": disposition,
            "reproduction_count": sum(reproduced),
            "positive_control": positive,
            "negative_control": negative,
            "environment_valid": environment_valid,
        }

    def _dedupe_finding(self, payload: dict[str, Any]) -> dict[str, Any]:
        controls = self._object(payload.get("controls"), "controls")
        finding = self._object(payload.get("finding"), "finding")
        if controls.get("disposition") != "CONFIRMED":
            return {**payload, "status": "SKIPPED", "duplicate_issue_url": None}
        token = self._github_token()
        if token is None:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_DUPLICATE_SEARCH_NOT_CONFIGURED",
                "duplicate_issue_url": None,
            }
        repository = {
            Project.FALCONE.value: "gntik-ai/falcone",
            Project.MUSEMATIC.value: "gntik-ai/musematic",
        }[str(finding["project"])]
        runner = SafeCommandRunner({"gh": self.settings.github_executable})
        try:
            result = runner.run(
                "gh",
                [
                    "issue",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "all",
                    "--search",
                    f'"{finding["fingerprint"]}"',
                    "--json",
                    "number,title,url,state",
                    "--limit",
                    "100",
                ],
                cwd=self.settings.run_root,
                environment={"GH_TOKEN": token},
            )
        except CommandFailure:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_DUPLICATE_SEARCH_FAILED",
                "duplicate_issue_url": None,
            }
        matches = json.loads(result.stdout)
        if not isinstance(matches, list):
            raise ValueError("GitHub duplicate search returned invalid data")
        duplicate = matches[0].get("url") if matches and isinstance(matches[0], dict) else None
        return {
            **payload,
            "status": "DUPLICATE" if duplicate else "CLEAR",
            "duplicate_issue_url": duplicate,
        }

    def _finalize_finding(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding_payload = self._object(payload.get("finding"), "finding")
        controls = self._object(payload.get("controls"), "controls")
        duplicate = self._object(payload.get("duplicate"), "duplicate")
        attempts = payload.get("attempts")
        if not isinstance(attempts, list) or len(attempts) != 2:
            raise ValueError("finding finalization requires two attempts")
        disposition = str(controls["disposition"])
        if duplicate.get("status") == "BLOCKED":
            disposition = "BLOCKED"
        elif duplicate.get("status") == "DUPLICATE":
            disposition = "DUPLICATE"
        finding_id = UUID(str(finding_payload["finding_id"]))
        evidence: list[Any] = []
        verifier_provider: str | None = None
        for attempt in attempts:
            agent = self._object(self._object(attempt, "attempt").get("agent_result"), "agent")
            verifier_provider = verifier_provider or str(agent.get("provider"))
            structured = self._object(agent.get("structured_result"), "structured_result")
            attempt_evidence = structured.get("evidence")
            if isinstance(attempt_evidence, list):
                evidence.extend(attempt_evidence)
        with self.sessions.begin() as session:
            finding = session.get(Finding, finding_id)
            if finding is None or finding.disposition != "CANDIDATE":
                raise ValueError("finding is no longer a candidate")
            finding.disposition = disposition
            finding.evidence_json = [*finding.evidence_json, *evidence]
            coverage = session.get(CoverageCell, finding.coverage_cell_id)
            if coverage is None:
                raise ValueError("finding coverage metadata is missing")
            coverage.status = {
                "CONFIRMED": "CONFIRMED_FINDING",
                "REJECTED": "NO_FINDING",
                "DUPLICATE": "NO_FINDING",
                "FLAKY": "RETEST_REQUIRED",
                "BLOCKED": "BLOCKED",
            }[disposition]
            if disposition == "BLOCKED":
                coverage.blocker = str(duplicate.get("blocker") or "FINDING_VERIFICATION_BLOCKED")
            verification_id = uuid5(
                NAMESPACE_URL,
                f"finding-verification:{finding_id}:{verifier_provider}:{disposition}",
            )
            if session.get(FindingVerification, verification_id) is None:
                session.add(
                    FindingVerification(
                        id=verification_id,
                        finding_id=finding_id,
                        run_id=None,
                        verifier_provider=verifier_provider or "unknown",
                        disposition=disposition,
                        reproduction_count=int(controls.get("reproduction_count", 0)),
                        positive_control=bool(controls.get("positive_control")),
                        negative_control=bool(controls.get("negative_control")),
                        duplicate_issue_url=duplicate.get("duplicate_issue_url"),
                        summary=f"independent verification result: {disposition}",
                    )
                )
        return {
            **payload,
            "status": disposition,
            "disposition": disposition,
            "finding_id": str(finding_id),
            "duplicate_issue_url": duplicate.get("duplicate_issue_url"),
        }

    def _load_confirmed_finding(self, payload: dict[str, Any]) -> dict[str, Any]:
        finding_id = UUID(str(payload["finding_id"]))
        requested_risk = RiskClass(str(payload["risk_class"]))
        orchestration = self._object(payload.get("orchestration"), "orchestration")
        workflow_id = str(orchestration.get("workflow_id", ""))
        if not workflow_id:
            raise ValueError("delivery workflow identity is missing")
        with self.sessions.begin() as session:
            finding = session.get(Finding, finding_id)
            if finding is None or finding.disposition != "CONFIRMED":
                raise ValueError("delivery requires a CONFIRMED finding")
            if requested_risk.value != finding.risk_class:
                raise ValueError("delivery risk class must match authoritative finding risk")
            if not finding.affected_deployment_sha:
                raise ValueError("delivery requires an immutable deployment repository SHA")
            run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
            if run is None:
                raise ValueError("delivery run metadata is missing")
            project = Project(finding.project)
            source_repository, deployment_repository = {
                Project.FALCONE: ("falcone", "falcone-charts"),
                Project.MUSEMATIC: ("musematic", "musematic-deploy"),
            }[project]
            change_set_id = uuid5(NAMESPACE_URL, f"changeset:{finding.id}")
            change_set = session.get(ChangeSet, change_set_id)
            if change_set is None:
                change_set = ChangeSet(
                    id=change_set_id,
                    finding_id=finding.id,
                    issue_id=None,
                    project=project.value,
                    risk_class=finding.risk_class,
                    state="CONFIRMED",
                    version=1,
                    source_repository=source_repository,
                    source_branch=None,
                    source_base_sha=finding.affected_source_sha,
                    source_head_sha=None,
                    source_pr_url=None,
                    deployment_repository=deployment_repository,
                    deployment_branch=None,
                    deployment_base_sha=finding.affected_deployment_sha,
                    deployment_head_sha=None,
                    deployment_pr_url=None,
                    image_digests={},
                    chart_version=None,
                    recovery_json={},
                )
                session.add(change_set)
            run.change_set_id = change_set_id
            run.status = "RUNNING"
            run.project = project.value
            issue = session.scalar(select(Issue).where(Issue.finding_id == finding.id))
            return {
                **payload,
                "status": "CONFIRMED_LOADED",
                "run_id": str(run.id),
                "change_set_id": str(change_set_id),
                "project": project.value,
                "risk_class": finding.risk_class,
                "finding_id": str(finding.id),
                "fingerprint": finding.fingerprint,
                "domain": finding.domain,
                "persona": finding.persona,
                "expected_behavior": finding.expected_behavior,
                "actual_behavior": finding.actual_behavior,
                "minimal_reproduction": finding.minimal_reproduction,
                "affected_component": finding.affected_component,
                "source_repository": source_repository,
                "source_base_sha": finding.affected_source_sha,
                "source_base_ref": "refs/heads/main",
                "deployment_repository": deployment_repository,
                "deployment_base_sha": finding.affected_deployment_sha,
                "deployment_base_ref": "refs/heads/main",
                "issue_number": issue.number if issue else None,
                "issue_url": issue.url if issue else None,
            }

    def _ensure_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("dry_run") is not False:
            return {
                **payload,
                "status": "DRY_RUN",
                "dry_run_summary": (
                    "confirmed finding and ChangeSet loaded; GitHub issue creation, leases, "
                    "worktrees, and writable provider execution were not started"
                ),
            }
        if payload.get("issue_number") and payload.get("issue_url"):
            return {**payload, "status": "ISSUE_READY"}
        if payload.get("risk_class") == RiskClass.R3.value:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "R3_GITHUB_ISSUE_REQUIRES_HUMAN_APPROVAL",
            }
        token = self._github_token()
        if token is None:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_ISSUE_CREATE_NOT_CONFIGURED",
            }
        project = Project(str(payload["project"]))
        repository = {
            Project.FALCONE: "gntik-ai/falcone",
            Project.MUSEMATIC: "gntik-ai/musematic",
        }[project]
        change_set_id = UUID(str(payload["change_set_id"]))
        body_directory = self.settings.state_root / "github"
        body_directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        body_path = body_directory / f"{change_set_id}-issue.md"
        body = (
            "## Confirmed finding\n\n"
            f"Fingerprint: `{payload['fingerprint']}`\n\n"
            f"Expected: {payload['expected_behavior']}\n\n"
            f"Actual: {payload['actual_behavior']}\n\n"
            "Minimal reproduction:\n"
            + "\n".join(f"1. {step}" for step in payload["minimal_reproduction"])
            + "\n\nThis issue was independently verified twice by the Engineering Control Plane.\n"
        )
        self._atomic_text(body_path, body)
        title = (
            f"[{payload['domain']}] confirmed behavior mismatch "
            f"({str(payload['fingerprint'])[:12]})"
        )
        runner = SafeCommandRunner({"gh": self.settings.github_executable})
        try:
            search = runner.run(
                "gh",
                [
                    "issue",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "all",
                    "--search",
                    f'"{payload["fingerprint"]}"',
                    "--json",
                    "number,title,url,state",
                    "--limit",
                    "100",
                ],
                cwd=self.settings.state_root,
                environment={"GH_TOKEN": token},
            )
            matches = json.loads(search.stdout)
            if not isinstance(matches, list):
                raise ValueError("GitHub issue idempotency search returned invalid data")
            if matches and isinstance(matches[0], dict):
                issue_url = str(matches[0].get("url", ""))
            else:
                created = runner.run(
                    "gh",
                    [
                        "issue",
                        "create",
                        "--repo",
                        repository,
                        "--title",
                        title,
                        "--body-file",
                        str(body_path),
                    ],
                    cwd=self.settings.state_root,
                    environment={"GH_TOKEN": token},
                )
                issue_url = created.stdout.strip()
        except CommandFailure:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_ISSUE_CREATE_FAILED",
            }
        issue_number_text = issue_url.rstrip("/").rsplit("/", 1)[-1]
        if not issue_url.startswith("https://github.com/") or not issue_number_text.isdigit():
            raise ValueError("GitHub issue creation returned an invalid URL")
        issue_number = int(issue_number_text)
        with self.sessions.begin() as session:
            finding_id = UUID(str(payload["finding_id"]))
            issue = session.scalar(select(Issue).where(Issue.finding_id == finding_id))
            if issue is None:
                issue = Issue(
                    finding_id=finding_id,
                    repository=repository,
                    number=issue_number,
                    url=issue_url,
                    state="OPEN",
                    openspec_change=None,
                )
                session.add(issue)
                session.flush()
            change_set = session.get(ChangeSet, change_set_id)
            if change_set is None:
                raise ValueError("ChangeSet disappeared during issue creation")
            change_set.issue_id = issue.id
        return {
            **payload,
            "status": "ISSUE_READY",
            "issue_number": issue_number,
            "issue_url": issue_url,
        }

    def _acquire_delivery_leases(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = Project(str(payload["project"]))
        component = self._component_key(project, str(payload["affected_component"]))
        run_id = UUID(str(payload["run_id"]))
        manager = ComponentLeaseManager()
        with self.sessions.begin() as session:
            lease = manager.acquire(
                session,
                component_key=component,
                owner_run_id=run_id,
                mode="implementation",
                ttl=timedelta(hours=4),
            )
        return {
            **payload,
            "status": "LEASES_ACQUIRED",
            "component_key": component,
            "component_lease_id": str(lease.id),
        }

    def _route_delivery(self, payload: dict[str, Any]) -> dict[str, Any]:
        risk = RiskClass(str(payload["risk_class"]))
        broker = self._quota_broker()
        maker = broker.route(role="implementer", model_class=ModelClass.WORKER, risk_class=risk)
        spec = broker.route(
            role="product-spec-architect",
            model_class=ModelClass.JUDGE,
            risk_class=risk,
        )
        if maker.primary is None or maker.checker is None or spec.primary is None:
            return {
                **payload,
                "status": "RATE_LIMITED" if maker.status == "RATE_LIMITED" else "BLOCKED",
                "routing_reason": f"maker={maker.reason};spec={spec.reason}",
            }
        issue = str(payload.get("issue_number") or f"finding-{str(payload['fingerprint'])[:12]}")
        allocation = {
            "change_set_id": payload["change_set_id"],
            "project": payload["project"],
            "issue": issue,
            "source_repository": payload["source_repository"],
            "source_base_sha": payload["source_base_sha"],
            "source_base_ref": payload["source_base_ref"],
            "deployment_repository": payload["deployment_repository"],
            "deployment_base_sha": payload["deployment_base_sha"],
            "deployment_base_ref": payload["deployment_base_ref"],
            "maker_provider": maker.primary.value,
            "checker_provider": maker.checker.value,
        }
        return {
            **payload,
            "status": "ROUTED",
            "spec_provider": spec.primary.value,
            "maker_provider": maker.primary.value,
            "checker_provider": maker.checker.value,
            "routing_reason": f"maker={maker.reason};spec={spec.reason}",
            "allocation_request": allocation,
        }

    def _persist_worktree_leases(self, payload: dict[str, Any]) -> dict[str, Any]:
        change_set_id = UUID(str(payload["change_set_id"]))
        owner_run_id = UUID(str(payload["run_id"]))
        expected_root = PurePosixPath(self.settings.worktree_root.as_posix())
        records = (
            (
                str(payload["source_repository"]),
                str(payload["source_worktree"]),
                str(payload["source_branch"]),
                str(payload["source_base_sha"]),
            ),
            (
                str(payload["deployment_repository"]),
                str(payload["deployment_worktree"]),
                str(payload["deployment_branch"]),
                str(payload["deployment_base_sha"]),
            ),
        )
        expires_at = datetime.now(UTC) + timedelta(hours=8)
        lease_ids: list[str] = []
        with self.sessions.begin() as session:
            for repository, worktree_path, branch, base_sha in records:
                expected_path = expected_root / repository / str(change_set_id)
                if PurePosixPath(worktree_path) != expected_path:
                    raise ValueError("worktree lease path does not match its ChangeSet allocation")
                existing = session.scalar(
                    select(WorktreeLease).where(
                        WorktreeLease.change_set_id == change_set_id,
                        WorktreeLease.repository == repository,
                        WorktreeLease.released_at.is_(None),
                    )
                )
                if existing is not None:
                    if (
                        existing.owner_run_id != owner_run_id
                        or existing.worktree_path != worktree_path
                        or existing.branch != branch
                        or existing.base_sha != base_sha
                    ):
                        raise ValueError("active worktree lease conflicts with this delivery run")
                    lease_ids.append(str(existing.id))
                    continue
                identity = uuid5(NAMESPACE_URL, f"worktree-lease:{change_set_id}:{repository}")
                lease = WorktreeLease(
                    id=identity,
                    change_set_id=change_set_id,
                    repository=repository,
                    worktree_path=worktree_path,
                    branch=branch,
                    owner_run_id=owner_run_id,
                    base_sha=base_sha,
                    expires_at=expires_at,
                    released_at=None,
                )
                session.add(lease)
                lease_ids.append(str(identity))
        return {**payload, "status": "WORKTREE_LEASES_PERSISTED", "worktree_lease_ids": lease_ids}

    def _prepare_spec(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = Provider(str(payload["spec_provider"]))
        change_set_id = UUID(str(payload["change_set_id"]))
        prompt = (
            "Produce a bounded provider-neutral OpenSpec for exactly one confirmed GitHub issue. "
            "Do not modify files and do not propose deployment execution. The source and "
            "deployment repositories belong to one ChangeSet.\n\n"
            f"Issue: {payload['issue_url']}\nFingerprint: {payload['fingerprint']}\n"
            f"Expected: {payload['expected_behavior']}\nActual: {payload['actual_behavior']}\n"
            f"Minimal reproduction: {json.dumps(payload['minimal_reproduction'])}\n"
            f"Risk class: {payload['risk_class']}\n"
            f"Source SHA: {payload['source_base_sha']}\n"
            f"Deployment SHA: {payload['deployment_base_sha']}\n"
            f"Deployment worktree: {payload['deployment_worktree']}\n"
        )
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"delivery-spec:{change_set_id}"),
            project=Project(str(payload["project"])),
            role="product-spec-architect",
            provider=provider,
            model_class=ModelClass.JUDGE,
            prompt=prompt,
            working_directory=str(payload["source_worktree"]),
            output_schema=self._spec_output_schema(),
            timeout_seconds=3600,
            effort="high",
            writable=False,
            change_set_id=change_set_id,
        )
        return {
            **payload,
            "status": "SPEC_READY",
            "spec_request": request.model_dump(mode="json"),
        }

    def _create_openspec(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._object(payload.get("agent_result"), "agent_result")
        structured = self._object(result.get("structured_result"), "structured_result")
        if structured.get("decision") == "BLOCKED":
            return {**payload, "status": "BLOCKED", "blocker": "SPEC_ARCHITECT_BLOCKED"}
        if structured.get("decision") != "READY":
            raise ValueError("spec architect returned an unknown decision")
        change_set_id = UUID(str(payload["change_set_id"]))
        identifier = f"engineering/{change_set_id}"
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            if change_set is None or change_set.issue_id is None:
                raise ValueError("OpenSpec requires a ChangeSet with a GitHub issue")
            issue = session.get(Issue, change_set.issue_id)
            if issue is None:
                raise ValueError("OpenSpec issue metadata is missing")
            issue.openspec_change = identifier
            change_set.state = "PLANNED"
            change_set.version += 1
        return {
            **payload,
            "status": "OPENSPEC_READY",
            "spec_agent_result": result,
            "openspec_identifier": identifier,
            "openspec_document": structured,
        }

    def _prepare_maker(self, payload: dict[str, Any], *, deployment: bool) -> dict[str, Any]:
        provider = Provider(str(payload["maker_provider"]))
        change_set_id = UUID(str(payload["change_set_id"]))
        target = "deployment" if deployment else "source"
        worktree_key = "deployment_worktree" if deployment else "source_worktree"
        request_key = "deployment_maker_request" if deployment else "maker_request"
        prior = payload.get("agent_result") if deployment else None
        if deployment:
            prior_result = self._object(prior, "source maker result")
            prior_structured = self._object(
                prior_result.get("structured_result"), "source maker structured result"
            )
            if prior_structured.get("decision") != "COMPLETE":
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "SOURCE_MAKER_DID_NOT_COMPLETE",
                }
        prompt = (
            f"Implement only the {target} repository portion of one bounded ChangeSet. "
            "Do not deploy, merge, push, read credentials, or alter unrelated files. Run bounded "
            "repository tests, then create one local commit on the assigned branch. Preserve all "
            "existing safety gates.\n\n"
            f"Issue: {payload['issue_url']}\nRisk: {payload['risk_class']}\n"
            f"OpenSpec: {json.dumps(payload['openspec_document'], sort_keys=True)}\n"
            f"Source worktree: {payload['source_worktree']}\n"
            f"Deployment worktree: {payload['deployment_worktree']}\n"
            f"Prior source maker result: {json.dumps(prior, sort_keys=True, default=str)}\n"
        )
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"delivery-{target}-maker:{change_set_id}"),
            project=Project(str(payload["project"])),
            role="implementer",
            provider=provider,
            model_class=ModelClass.WORKER,
            prompt=prompt,
            working_directory=str(payload[worktree_key]),
            output_schema=self._maker_output_schema(),
            timeout_seconds=7200,
            effort="high",
            writable=True,
            change_set_id=change_set_id,
        )
        response = {
            **payload,
            "status": f"{target.upper()}_MAKER_READY",
            request_key: request.model_dump(mode="json"),
        }
        if deployment:
            response["source_maker_result"] = prior
        return response

    def _prepare_checker(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = Provider(str(payload["checker_provider"]))
        change_set_id = UUID(str(payload["change_set_id"]))
        source_result = self._object(payload.get("agent_result"), "agent_result")
        source_maker = self._object(payload.get("source_maker_result"), "source_maker_result")
        for label, result in (
            ("source", source_maker),
            ("deployment", source_result),
        ):
            structured = self._object(result.get("structured_result"), f"{label}_maker_result")
            if structured.get("decision") != "COMPLETE":
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": f"{label.upper()}_MAKER_DID_NOT_COMPLETE",
                }
        prompt = (
            "Independently review both bounded worktrees for one ChangeSet. Do not modify files. "
            "Validate the OpenSpec, source and deployment diffs, tests, immutable image policy, "
            "and risk gates. Request changes on any uncertainty.\n\n"
            f"Issue: {payload['issue_url']}\nRisk: {payload['risk_class']}\n"
            f"OpenSpec: {json.dumps(payload['openspec_document'], sort_keys=True)}\n"
            f"Source worktree: {payload['source_worktree']}\n"
            f"Deployment worktree: {payload['deployment_worktree']}\n"
            f"Latest maker result: {json.dumps(source_result, sort_keys=True, default=str)}\n"
        )
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"delivery-checker:{change_set_id}"),
            project=Project(str(payload["project"])),
            role="code-reviewer",
            provider=provider,
            model_class=ModelClass.JUDGE,
            prompt=prompt,
            working_directory=str(payload["source_worktree"]),
            output_schema=self._checker_output_schema(),
            timeout_seconds=5400,
            effort="high",
            writable=False,
            change_set_id=change_set_id,
        )
        return {
            **payload,
            "status": "CHECKER_READY",
            "deployment_maker_result": source_result,
            "checker_request": request.model_dump(mode="json"),
        }

    def _persist_delivery_handoff(self, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = self._object(payload.get("change_evidence"), "change_evidence")
        source = self._object(evidence.get("source"), "source change evidence")
        deployment = self._object(evidence.get("deployment"), "deployment change evidence")
        if source.get("head_sha") == payload.get("source_base_sha"):
            blocker = "SOURCE_CHANGES_NOT_COMMITTED"
        elif int(deployment.get("changed_file_count", 0)) > 0 and deployment.get(
            "head_sha"
        ) == payload.get("deployment_base_sha"):
            blocker = "DEPLOYMENT_CHANGES_NOT_COMMITTED"
        else:
            blocker = "GIT_PUSH_IDENTITY_NOT_CONFIGURED"
        change_set_id = UUID(str(payload["change_set_id"]))
        run_id = UUID(str(payload["run_id"]))
        with self.sessions.begin() as session:
            change_set = session.get(ChangeSet, change_set_id)
            run = session.get(Run, run_id)
            if change_set is None or run is None:
                raise ValueError("delivery handoff metadata is missing")
            change_set.source_branch = str(payload["source_branch"])
            change_set.deployment_branch = str(payload["deployment_branch"])
            change_set.source_head_sha = str(source["head_sha"])
            change_set.deployment_head_sha = str(deployment["head_sha"])
            change_set.state = "IMPLEMENTING"
            change_set.version += 1
            run.status = "BLOCKED"
            run.error_code = blocker
            run.summary = "bounded local changes are preserved pending scoped Git push identity"
        return {
            **payload,
            "status": "BLOCKED",
            "blocker": blocker,
            "handoff": {
                "change_set_id": str(change_set_id),
                "source_branch": payload["source_branch"],
                "source_head_sha": source["head_sha"],
                "deployment_branch": payload["deployment_branch"],
                "deployment_head_sha": deployment["head_sha"],
                "change_evidence": evidence,
            },
        }

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

    @staticmethod
    def _verification_output_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "summary",
                "reproduced",
                "positive_control",
                "negative_control",
                "environment_valid",
                "evidence",
                "open_questions",
            ],
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 20000},
                "reproduced": {"type": "boolean"},
                "positive_control": {"type": "boolean"},
                "negative_control": {"type": "boolean"},
                "environment_valid": {"type": "boolean"},
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
            },
        }

    def _github_token(self) -> str | None:
        path = self.settings.github_token_file
        if path is None or not path.is_file():
            return None
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & (stat.S_IWGRP | stat.S_IRWXO):
            raise ValueError("GitHub token file permissions are too broad")
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 20:
            raise ValueError("GitHub token file is not provisioned")
        return token

    @staticmethod
    def _atomic_text(path: Path, value: str) -> None:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(value.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)

    @staticmethod
    def _component_key(project: Project, value: str) -> str:
        lowered = value.lower()
        if project is Project.FALCONE:
            component = (
                "gateway"
                if any(marker in lowered for marker in ("gateway", "apisix"))
                else "web-console"
                if any(marker in lowered for marker in ("web", "console", "ui"))
                else "control-plane"
            )
        else:
            component = (
                "runtime-controller"
                if any(marker in lowered for marker in ("runtime", "sandbox"))
                else "web"
                if any(marker in lowered for marker in ("web", "ui"))
                else "control-plane"
            )
        return f"{project.value}:{component}"

    @staticmethod
    def _spec_output_schema() -> dict[str, Any]:
        string_list = {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {"type": "string", "minLength": 1, "maxLength": 4000},
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "decision",
                "title",
                "problem",
                "scope",
                "non_scope",
                "acceptance_criteria",
                "implementation_constraints",
                "test_plan",
                "risk_notes",
                "open_questions",
            ],
            "properties": {
                "decision": {"type": "string", "enum": ["READY", "BLOCKED"]},
                "title": {"type": "string", "minLength": 1, "maxLength": 300},
                "problem": {"type": "string", "minLength": 1, "maxLength": 20000},
                "scope": string_list,
                "non_scope": string_list,
                "acceptance_criteria": string_list,
                "implementation_constraints": string_list,
                "test_plan": string_list,
                "risk_notes": string_list,
                "open_questions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4000},
                },
            },
        }

    @staticmethod
    def _maker_output_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "decision",
                "summary",
                "changed_files",
                "tests",
                "open_questions",
            ],
            "properties": {
                "decision": {"type": "string", "enum": ["COMPLETE", "BLOCKED"]},
                "summary": {"type": "string", "minLength": 1, "maxLength": 20000},
                "changed_files": {
                    "type": "array",
                    "maxItems": 1000,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                "tests": {
                    "type": "array",
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["command", "status"],
                        "properties": {
                            "command": {"type": "string", "maxLength": 4000},
                            "status": {"type": "string", "enum": ["PASS", "FAIL", "SKIPPED"]},
                        },
                    },
                },
                "open_questions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "maxLength": 4000},
                },
            },
        }

    @staticmethod
    def _checker_output_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "summary", "findings", "evidence", "open_questions"],
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["PASS", "REQUEST_CHANGES", "BLOCKED"],
                },
                "summary": {"type": "string", "minLength": 1, "maxLength": 20000},
                "findings": {
                    "type": "array",
                    "maxItems": 500,
                    "items": {"type": "string", "maxLength": 4000},
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
            },
        }
