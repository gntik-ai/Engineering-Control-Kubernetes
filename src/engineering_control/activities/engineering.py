from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from engineering_control.config import Settings
from engineering_control.db import models as db
from engineering_control.engineering.configuration import EngineeringCatalog
from engineering_control.engineering.models import EnhancementProposal
from engineering_control.identity.attestation import (
    ObserverReadinessAttestation,
    SuperadminReadinessAttestation,
    observer_contract_hash,
    superadmin_contract_hash,
)
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider


class EngineeringActivityHandler:
    """Controller-only bounded planning and durable loop/enhancement metadata activities."""

    supported_steps = frozenset(
        {
            "campaign.plan",
            "project.persist_snapshot",
            "audit.classify_candidate",
            "audit.discover_enhancements",
            "audit.verify_cleanup",
            "loop.load_selection",
            "loop.acquire_component",
            "loop.record_skip",
            "loop.record_blocker",
            "loop.record_item",
            "loop.release_component",
            "loop.finalize",
            "enhancement.load_evidence",
            "enhancement.prepare_discovery",
            "enhancement.validate_evidence",
            "enhancement.dedupe",
            "enhancement.persist",
            "enhancement.persist_publication",
            "enhancement.load_approved",
            "enhancement.create_openspec",
            "enhancement.publication_dry_run",
            "enhancement.prepare_solution",
        }
    )

    _risk_rank = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.catalog = EngineeringCatalog(
            project_root=settings.project_config_root,
            persona_root=settings.persona_config_root,
            flow_root=settings.flow_config_root,
            credential_root=settings.credential_root,
            attestation_root=settings.credential_attestation_root,
            browser_worker_identity=settings.browser_worker_identity,
            scan_worker_identity=settings.scan_worker_identity,
        )
        database_url = settings.database_url.get_secret_value()
        engine_options: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("postgresql"):
            engine_options.update(
                pool_size=2,
                max_overflow=1,
                connect_args={"connect_timeout": 5},
            )
        self.engine = create_engine(database_url, **engine_options)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step == "campaign.plan":
            plan = self.catalog.plan(
                str(payload["project"]),
                str(payload.get("preset", "snapshot")),
                str(payload.get("environment", "staging")),
            )
            value = plan.model_dump(mode="json")
            raw_attestation = payload.get("worker_attestation")
            if isinstance(raw_attestation, dict):
                attestation = SuperadminReadinessAttestation.model_validate(raw_attestation)
                authority = (
                    "falcone" if payload.get("project") == "llmwiki" else payload.get("project")
                )
                if (
                    attestation.project != authority
                    or attestation.worker_identity != self.settings.browser_worker_identity
                    or attestation.contract_hash != superadmin_contract_hash(str(authority))
                ):
                    raise ValueError("worker superadmin attestation authority mismatch")
                reference = f"worker-attestation:{authority}-superadmin"
                for item in value["credential_readiness"]:
                    if item["reference"] == reference:
                        item.update(
                            present=attestation.file_present,
                            permissions_valid=True,
                            worker_attested=attestation.ready(),
                            blocker="NONE" if attestation.ready() else "ATTESTATION_INVALID",
                        )
                value["blockers"] = [item for item in value["blockers"] if reference not in item]
                if not attestation.ready():
                    value["blockers"].append(f"CREDENTIAL:{reference}:{attestation.blocker_code}")
            raw_observer_attestation = payload.get("observer_attestation")
            if isinstance(raw_observer_attestation, dict):
                observer_attestation = ObserverReadinessAttestation.model_validate(
                    raw_observer_attestation
                )
                project = str(payload.get("project"))
                if (
                    observer_attestation.project != project
                    or observer_attestation.worker_identity != self.settings.scan_worker_identity
                    or observer_attestation.contract_hash != observer_contract_hash(project)
                ):
                    raise ValueError("worker observer attestation authority mismatch")
                reference = f"worker-attestation:{project}-observer"
                for item in value["credential_readiness"]:
                    if item["reference"] == reference:
                        item.update(
                            present=observer_attestation.file_present,
                            permissions_valid=observer_attestation.permissions_valid,
                            worker_attested=observer_attestation.ready(),
                            blocker=(
                                "NONE" if observer_attestation.ready() else "ATTESTATION_INVALID"
                            ),
                        )
                value["blockers"] = [item for item in value["blockers"] if reference not in item]
                if observer_attestation.ready():
                    value["kubernetes_readiness"] = "OPERATIONAL"
                else:
                    value["kubernetes_readiness"] = "BLOCKED_BY_CREDENTIAL"
                    value["blockers"].append(
                        f"CREDENTIAL:{reference}:{observer_attestation.blocker_code}"
                    )
            value["domains"] = self._domains(str(payload.get("preset", "snapshot")))
            return {**payload, **value, "status": "PLANNED"}
        if step == "project.persist_snapshot":
            return self._persist_project_snapshot(payload)
        if step == "loop.load_selection":
            return self._load_loop_selection(payload)
        if step == "enhancement.load_evidence":
            return self._load_enhancement_evidence(payload)
        if step == "enhancement.dedupe":
            return self._prepare_product_review(self._dedupe_enhancement(payload))
        if step == "enhancement.load_approved":
            return self._load_approved_enhancement(payload)
        if step == "enhancement.persist":
            return self._persist_enhancement(payload)
        if step == "enhancement.persist_publication":
            return self._persist_enhancement_publication(payload)
        if step == "enhancement.publication_dry_run":
            return self._envelope(step, payload, "DRY_RUN")
        if step == "enhancement.prepare_solution":
            return self._envelope(step, payload, "SOLUTION_PLANNED")
        if step == "enhancement.create_openspec":
            return self._envelope(step, payload, "OPENSPEC_PLANNED")
        if step == "enhancement.prepare_discovery":
            return self._prepare_enhancement_discovery(payload)
        if step == "enhancement.validate_evidence":
            return self._validate_enhancement_evidence(payload)
        if step.startswith("loop."):
            return self._envelope(step, payload, "PASS")
        return self._envelope(step, payload, "PASS")

    def _persist_project_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            campaign_id = UUID(str(payload["campaign_id"]))
            project = Project(str(payload["project"]))
        except (KeyError, ValueError):
            return {**payload, "status": "BLOCKED", "blocker": "SNAPSHOT_IDENTITY_INVALID"}
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            return {**payload, "status": "BLOCKED", "blocker": "SNAPSHOT_PAYLOAD_MISSING"}
        source_sha = str(snapshot.get("source_sha", ""))
        deployment_sha = str(snapshot.get("deployment_sha", ""))
        if not self._git_sha(source_sha) or not self._git_sha(deployment_sha):
            return {**payload, "status": "BLOCKED", "blocker": "SNAPSHOT_SHA_INVALID"}
        pair = payload.get("pair_snapshot")
        if not isinstance(pair, dict):
            pair = snapshot.get("pair_snapshot")
        pair_contract = pair.get("pair_snapshot") if isinstance(pair, dict) else None
        runtime = pair_contract.get("runtime") if isinstance(pair_contract, dict) else None
        live_revision = runtime.get("deployed_revision") if isinstance(runtime, dict) else None
        status = str(pair.get("status")) if isinstance(pair, dict) else "REPOSITORIES_CAPTURED"
        record = {
            "source_sha": source_sha,
            "deployment_sha": deployment_sha,
            "source_worktree": snapshot.get("source_worktree"),
            "deployment_worktree": snapshot.get("deployment_worktree"),
            "source_snapshot": snapshot.get("source_snapshot"),
            "deployment_snapshot": snapshot.get("deployment_snapshot"),
            "pair_snapshot": pair_contract,
            "pair_status": status,
            "pair_blocker": pair.get("blocker") if isinstance(pair, dict) else None,
        }
        with self.sessions.begin() as session:
            campaign = session.get(db.Campaign, campaign_id)
            if campaign is None or campaign.project != project.value:
                return {**payload, "status": "BLOCKED", "blocker": "CAMPAIGN_NOT_FOUND"}
            identity = uuid5(NAMESPACE_URL, f"project-snapshot:{campaign_id}")
            persisted = session.get(db.ProjectSnapshot, identity)
            if persisted is None:
                persisted = db.ProjectSnapshot(
                    id=identity,
                    campaign_id=campaign_id,
                    project=project.value,
                    source_sha=source_sha,
                    deployment_sha=deployment_sha,
                    live_revision=str(live_revision) if live_revision else None,
                    status=status,
                    snapshot_json=record,
                )
                session.add(persisted)
            else:
                if persisted.source_sha != source_sha or persisted.deployment_sha != deployment_sha:
                    raise ValueError("persisted campaign snapshot immutable SHAs changed")
                persisted.live_revision = str(live_revision) if live_revision else None
                persisted.status = status
                persisted.snapshot_json = record
            campaign.source_sha = source_sha
            campaign.deployment_sha = deployment_sha
        return self._envelope(
            "project.persist_snapshot",
            {
                **payload,
                "project_snapshot_id": str(identity),
                "persisted_pair_status": status,
            },
            "SNAPSHOT_PERSISTED",
        )

    def _prepare_enhancement_discovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        handles = payload.get("evidence_handles")
        worktree = payload.get("source_worktree")
        if not isinstance(handles, list) or not handles:
            return {**payload, "status": "BLOCKED", "blocker": "AUDIT_EVIDENCE_REQUIRED"}
        if not isinstance(worktree, str) or not worktree:
            return {**payload, "status": "BLOCKED", "blocker": "AUDIT_WORKTREE_REQUIRED"}
        campaign_id = UUID(str(payload["campaign_id"]))
        project = Project(str(payload["project"]))
        schema = self._enhancement_discovery_schema()
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"enhancement-discovery:{campaign_id}"),
            project=project,
            role="domain-auditor",
            provider=Provider.CODEX,
            model_class=ModelClass.WORKER,
            prompt=(
                "Discover at most one bounded enhancement from the supplied redacted audit "
                "evidence handles and immutable source worktree. Separate product friction from "
                "correctness defects. Do not publish or implement it. Every evidence handle in "
                "the proposal must be selected verbatim from this allowlist:\n"
                + json.dumps(handles, separators=(",", ":"))
            ),
            working_directory=worktree,
            output_schema=schema,
            timeout_seconds=3600,
            effort="medium",
            writable=False,
            campaign_id=campaign_id,
        )
        return self._envelope(
            "enhancement.prepare_discovery",
            {
                **payload,
                "provider": Provider.CODEX.value,
                "discovery_provider": Provider.CODEX.value,
                "discovery_request": request.model_dump(mode="json"),
            },
            "PREPARED",
        )

    def _prepare_product_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("status") == "DUPLICATE":
            return payload
        proposal = payload.get("enhancement")
        discovery = payload.get("discovery_request")
        if not isinstance(proposal, dict) or not isinstance(discovery, dict):
            return {**payload, "status": "BLOCKED", "blocker": "PRODUCT_REVIEW_INPUT_MISSING"}
        campaign_id = UUID(str(payload["campaign_id"]))
        request = AgentRequest(
            run_id=uuid5(NAMESPACE_URL, f"enhancement-product-review:{campaign_id}"),
            project=Project(str(payload["project"])),
            role="product-spec-architect",
            provider=Provider.CLAUDE,
            model_class=ModelClass.JUDGE,
            prompt=(
                "Independently review this evidence-backed enhancement. Approve only when it is "
                "not a defect or duplicate, has measurable acceptance criteria, bounded risk, "
                "and a safe rollout. Do not publish, approve on behalf of a human, or implement.\n"
                + json.dumps(proposal, separators=(",", ":"), sort_keys=True)
            ),
            working_directory=str(discovery["working_directory"]),
            output_schema=self._enhancement_review_schema(),
            timeout_seconds=3600,
            effort="high",
            writable=False,
            campaign_id=campaign_id,
        )
        return self._envelope(
            "enhancement.dedupe",
            {
                **payload,
                "provider": Provider.CLAUDE.value,
                "product_review_provider": Provider.CLAUDE.value,
                "product_review_request": request.model_dump(mode="json"),
            },
            "UNIQUE",
        )

    def _validate_enhancement_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        enhancement = payload.get("enhancement")
        if not isinstance(enhancement, dict):
            agent_result = payload.get("agent_result")
            structured = (
                agent_result.get("structured_result") if isinstance(agent_result, dict) else None
            )
            enhancement = structured.get("enhancement") if isinstance(structured, dict) else None
        if not isinstance(enhancement, dict):
            return {**payload, "status": "BLOCKED", "blocker": "ENHANCEMENT_SCHEMA_REQUIRED"}
        try:
            proposal = EnhancementProposal.model_validate(enhancement)
        except ValueError:
            return {**payload, "status": "BLOCKED", "blocker": "ENHANCEMENT_SCHEMA_INVALID"}
        evidence = set(str(item) for item in payload.get("evidence_handles", []))
        if not set(proposal.evidence_handles).issubset(evidence):
            return {**payload, "status": "BLOCKED", "blocker": "ENHANCEMENT_EVIDENCE_UNBOUND"}
        return self._envelope(
            "enhancement.validate_evidence",
            {**payload, "enhancement": proposal.model_dump(mode="json")},
            "EVIDENCE_VALIDATED",
        )

    def _load_loop_selection(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            campaign_id = UUID(str(payload["campaign_id"]))
        except (KeyError, ValueError):
            return {**payload, "status": "BLOCKED", "blocker": "CAMPAIGN_ID_INVALID"}
        maximum = str(payload.get("maximum_risk", "R1"))
        maximum_rank = self._risk_rank.get(maximum)
        if maximum_rank is None:
            return {**payload, "status": "BLOCKED", "blocker": "MAXIMUM_RISK_INVALID"}
        with self.sessions() as session:
            campaign = session.get(db.Campaign, campaign_id)
            if campaign is None or campaign.project != payload.get("project"):
                return {**payload, "status": "BLOCKED", "blocker": "CAMPAIGN_NOT_FOUND"}
            findings = session.scalars(
                select(db.Finding)
                .where(db.Finding.campaign_id == campaign_id)
                .where(db.Finding.disposition == "CONFIRMED")
                .order_by(db.Finding.created_at, db.Finding.id)
            ).all()
        items = [
            {
                "finding_id": str(finding.id),
                "risk_class": finding.risk_class,
                "component": finding.affected_component,
                "fingerprint": finding.fingerprint,
            }
            for finding in findings
            if self._risk_rank.get(finding.risk_class, 99) <= maximum_rank
        ]
        return self._envelope("loop.load_selection", {**payload, "items": items}, "SELECTED")

    def _load_enhancement_evidence(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            campaign_id = UUID(str(payload["campaign_id"]))
        except (KeyError, ValueError):
            return {**payload, "status": "BLOCKED", "blocker": "CAMPAIGN_ID_INVALID"}
        with self.sessions() as session:
            campaign = session.get(db.Campaign, campaign_id)
            if campaign is None or campaign.project != payload.get("project"):
                return {**payload, "status": "BLOCKED", "blocker": "CAMPAIGN_NOT_FOUND"}
            artifacts = session.scalars(
                select(db.Artifact)
                .where(db.Artifact.campaign_id == campaign_id)
                .where(db.Artifact.redaction_status.in_(["REDACTED", "NOT_REQUIRED"]))
                .order_by(db.Artifact.created_at)
            ).all()
            snapshot = session.scalar(
                select(db.ProjectSnapshot).where(db.ProjectSnapshot.campaign_id == campaign_id)
            )
        handles = [f"artifact:{artifact.id}:{artifact.sha256}" for artifact in artifacts]
        if not handles:
            return {**payload, "status": "BLOCKED", "blocker": "AUDIT_EVIDENCE_REQUIRED"}
        if snapshot is None:
            return {**payload, "status": "BLOCKED", "blocker": "PROJECT_SNAPSHOT_REQUIRED"}
        source_worktree = snapshot.snapshot_json.get("source_worktree")
        if not isinstance(source_worktree, str) or not source_worktree:
            return {**payload, "status": "BLOCKED", "blocker": "AUDIT_WORKTREE_REQUIRED"}
        return self._envelope(
            "enhancement.load_evidence",
            {
                **payload,
                "evidence_handles": handles,
                "source_worktree": source_worktree,
                "source_sha": snapshot.source_sha,
                "deployment_sha": snapshot.deployment_sha,
            },
            "EVIDENCE_LOADED",
        )

    def _dedupe_enhancement(self, payload: dict[str, Any]) -> dict[str, Any]:
        fingerprint = self._fingerprint(payload.get("enhancement", payload))
        with self.sessions() as session:
            duplicate = session.scalar(
                select(db.Enhancement).where(db.Enhancement.fingerprint == fingerprint)
            )
        return self._envelope(
            "enhancement.dedupe",
            {
                **payload,
                "enhancement_fingerprint": fingerprint,
                "duplicate_enhancement_id": (
                    duplicate.enhancement_key if duplicate is not None else None
                ),
            },
            "DUPLICATE" if duplicate is not None else "UNIQUE",
        )

    def _persist_enhancement(self, payload: dict[str, Any]) -> dict[str, Any]:
        enhancement = payload.get("enhancement")
        if not isinstance(enhancement, dict):
            return {**payload, "status": "BLOCKED", "blocker": "ENHANCEMENT_SCHEMA_REQUIRED"}
        agent_result = payload.get("agent_result")
        review = agent_result.get("structured_result") if isinstance(agent_result, dict) else None
        if not isinstance(review, dict) or review.get("decision") != "APPROVE":
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "PRODUCT_SPEC_REVIEW_NOT_APPROVED",
            }
        fingerprint = str(payload.get("enhancement_fingerprint") or self._fingerprint(enhancement))
        key = "enh-" + fingerprint.removeprefix("sha256:")[:32]
        binding = hashlib.sha256(
            json.dumps(
                {"enhancement_id": key, "fingerprint": fingerprint, "payload": enhancement},
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.sessions.begin() as session:
            existing = session.scalar(
                select(db.Enhancement).where(db.Enhancement.fingerprint == fingerprint)
            )
            if existing is None:
                session.add(
                    db.Enhancement(
                        enhancement_key=key,
                        project=str(payload["project"]),
                        campaign_id=str(payload["campaign_id"]),
                        persona=str(enhancement["persona"]),
                        affected_repository=str(enhancement["affected_repository"]),
                        risk_class=str(enhancement["risk"]),
                        status="CANDIDATE_ENHANCEMENT",
                        fingerprint=fingerprint,
                        approval_binding_sha256=binding,
                        payload_json=enhancement,
                    )
                )
            else:
                key = existing.enhancement_key
        return self._envelope(
            "enhancement.persist",
            {**payload, "enhancement_id": key, "approval_binding": binding},
            "CANDIDATE_ENHANCEMENT",
        )

    def _persist_enhancement_publication(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("enhancement_id", ""))
        issue_url = str(payload.get("issue_url", ""))
        if payload.get("status") != "PUBLISHED" or not issue_url.startswith(
            "https://github.com/gntik-ai/"
        ):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "ENHANCEMENT_PUBLICATION_EVIDENCE_INVALID",
            }
        with self.sessions.begin() as session:
            enhancement = session.scalar(
                select(db.Enhancement)
                .where(db.Enhancement.enhancement_key == key)
                .with_for_update()
            )
            if enhancement is None or enhancement.status != "APPROVED":
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "ENHANCEMENT_APPROVAL_REQUIRED",
                }
            expected_prefix = f"https://github.com/{enhancement.affected_repository}/issues/"
            if not issue_url.startswith(expected_prefix):
                raise ValueError("published enhancement issue repository changed")
            enhancement.status = "PUBLISHED"
            enhancement.payload_json = {**enhancement.payload_json, "issue_url": issue_url}
        return self._envelope("enhancement.persist_publication", payload, "PUBLISHED")

    @staticmethod
    def _enhancement_discovery_schema() -> dict[str, Any]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["enhancement"],
            "properties": {"enhancement": EnhancementProposal.model_json_schema()},
        }

    @staticmethod
    def _enhancement_review_schema() -> dict[str, Any]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "summary", "evidence"],
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "BLOCKED"],
                },
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                "evidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {"type": "string", "minLength": 1, "maxLength": 2000},
                },
            },
        }

    @staticmethod
    def _git_sha(value: str) -> bool:
        return len(value) == 40 and all(character in "0123456789abcdef" for character in value)

    def _load_approved_enhancement(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("enhancement_id", ""))
        binding = str(payload.get("approval_binding", ""))
        with self.sessions() as session:
            enhancement = session.scalar(
                select(db.Enhancement).where(db.Enhancement.enhancement_key == key)
            )
        if enhancement is None:
            return {**payload, "status": "BLOCKED", "blocker": "ENHANCEMENT_NOT_FOUND"}
        if enhancement.status != "APPROVED" or enhancement.approval_binding_sha256 != binding:
            return {**payload, "status": "BLOCKED", "blocker": "ENHANCEMENT_APPROVAL_REQUIRED"}
        return {
            **payload,
            **enhancement.payload_json,
            "project": enhancement.project,
            "campaign_id": enhancement.campaign_id,
            "approval_validated": True,
            "status": "APPROVED",
        }

    @staticmethod
    def _domains(preset: str) -> list[str]:
        if preset == "snapshot":
            return ["deployment"]
        if preset == "ux":
            return ["frontend", "product"]
        if preset == "security":
            return ["authentication", "authorization", "tenancy", "supply-chain"]
        if preset == "deployment":
            return ["deployment", "kubernetes", "operations"]
        return ["product", "api", "authorization", "deployment", "ux"]

    @staticmethod
    def _fingerprint(value: Any) -> str:
        canonical = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str).encode()
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _envelope(step: str, payload: dict[str, Any], status: str) -> dict[str, Any]:
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()
        return {
            **payload,
            "last_step": step,
            "status": status,
            "evidence_sha256": hashlib.sha256(canonical).hexdigest(),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
