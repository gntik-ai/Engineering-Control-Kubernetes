from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, create_engine, or_, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.activities.gitops import StagingFlowBlocked, enabled_staging_flow
from engineering_control.api.schemas import ExistingBacklogLoopStart
from engineering_control.config import Settings
from engineering_control.db import models as db
from engineering_control.db.runtime_binding import _current_identity, new_bound_run
from engineering_control.delivery_notifications import (
    attempt_label,
    enqueue_loop_state,
    enqueue_milestone,
    issue_for_finding,
)
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    BACKLOG_PROJECT_PAIRS,
    BACKLOG_SKIP_REASONS,
    BacklogIssueRecord,
    BacklogIssueType,
    GitHubBacklogImportSnapshot,
    _filter_eligibility,
    approval_binding,
    canonical_sha256,
    classify_issue,
    is_hermes_operational_issue,
    is_reauthenticated_backlog_operator,
    plan_records,
)
from engineering_control.engineering.backlog_authority import (
    _current_backlog_approval,
    _global_campaign_authorizes,
    _valid_r3_candidate,
    require_alternative_issue_authority,
)
from engineering_control.engineering.configuration import EngineeringCatalog
from engineering_control.engineering.models import EnhancementProposal
from engineering_control.identity.attestation import (
    ObserverReadinessAttestation,
    SuperadminReadinessAttestation,
    observer_contract_hash,
    superadmin_contract_hash,
)
from engineering_control.models.core import AgentRequest, resolve_agent_effort
from engineering_control.models.enums import ModelClass, Project, Provider
from engineering_control.operations.issue_approval import (
    require_backlog_issue_approval,
    require_issue_request_approval,
)
from engineering_control.operations.issue_delivery_approval import (
    CI_REPAIR_AUTHORIZATION_STEP,
    CI_REPAIR_REGRESSIONS,
    MAX_CI_REPAIR_ROUNDS,
    ci_repair_authorization_id,
    register_child_issue_approval,
)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


_GLOBAL_BACKLOG_RETRY_LIMIT = 5
_GLOBAL_BACKLOG_RETRY_BASE_MINUTES = 15
_TERMINAL_GLOBAL_BLOCKERS = frozenset(
    {
        "FINAL_MERGED_REVISION_DELIVERY_SERVICE_REQUIRED",
        "LLMWIKI_FULL_DELIVERY_INPUTS_UNRESOLVED",
        "BLOCKED_AUTONOMOUS_REPAIR_EXHAUSTED",
        "CORRECTION_LIMIT_EXHAUSTED",
        "NEEDS_PRODUCT_DECISION",
    }
)


def _selectable_backlog_issue() -> Any:
    """Rows a backlog loop may select: eligible ones, plus R3 rows deferred only because
    they need an operator's R3 approval. NEW-96: R3 rows are imported as DEFERRED
    (R3_REQUIRES_OPERATOR), so both selections silently excluded every R3 issue even with
    an approved R3 candidate; the callers still admit such a row only with a valid one."""
    return or_(
        db.BacklogIssueRecord.eligibility_status.in_(
            {"ELIGIBLE_FOR_REVALIDATION", "ELIGIBLE_FOR_RUNTIME_REVALIDATION", "READY"}
        ),
        and_(
            db.BacklogIssueRecord.eligibility_status == "DEFERRED",
            db.BacklogIssueRecord.eligibility_reason == "R3_REQUIRES_OPERATOR",
            db.BacklogIssueRecord.calculated_risk == "R3",
        ),
    )


def _deferred_r3_row_in_filter(row: db.BacklogIssueRecord, import_filter: str) -> bool:
    """NEW-96 review: a DEFERRED/R3_REQUIRES_OPERATOR row is selectable only if the
    campaign's import filter admits its issue type, re-checked at selection so rows
    imported before the filter-first classification cannot slip through."""
    if row.eligibility_status != "DEFERRED":
        return True
    try:
        issue_type = BacklogIssueType(row.issue_type)
    except ValueError:
        return False
    snapshot = row.github_snapshot_json if isinstance(row.github_snapshot_json, dict) else {}
    body = snapshot.get("body_excerpt")
    if not isinstance(body, str):
        return False
    # The same text the import classified on (classify_issue), so "needs product
    # decision" and similar text rules apply identically.
    eligible, _ = _filter_eligibility(issue_type, import_filter, f"{row.title}\n{body}".lower())
    return eligible


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
            "backlog.persist_import",
            "backlog.plan",
            "backlog.approval_candidate",
            "backlog.record_approval",
            "backlog.record_skip",
            "backlog.record_retry",
            "backlog.load_execution",
            "backlog.materialize_candidate",
            "backlog.record_revalidation",
            "backlog.revalidate_gate",
            "backlog.record_loop_state",
            "backlog.restore_published_change_set",
            "notify.write_policy_required",
            "backlog.register_child_run",
            "backlog.register_run",
            "backlog.record_ci_repair",
            "global_backlog.register",
            "global_backlog.select_next",
            "global_backlog.record_result",
            "global_backlog.finalize",
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
        if step == "backlog.persist_import":
            return self._persist_backlog_import(payload)
        if step == "backlog.plan":
            return self._backlog_plan(payload)
        if step == "backlog.approval_candidate":
            return self._backlog_approval_candidate(payload)
        if step == "backlog.record_approval":
            return self._record_backlog_approval(payload)
        if step == "backlog.record_skip":
            return self._record_backlog_skip(payload)
        if step == "backlog.record_retry":
            return self._record_backlog_retry(payload)
        if step == "backlog.load_execution":
            return self._load_backlog_execution(payload)
        if step == "backlog.materialize_candidate":
            return self._materialize_backlog_candidate(payload)
        if step == "backlog.record_revalidation":
            return self._record_backlog_revalidation(payload)
        if step == "backlog.revalidate_gate":
            return self._backlog_revalidate_gate(payload)
        if step == "backlog.record_loop_state":
            return self._record_backlog_loop_state(payload)
        if step == "backlog.restore_published_change_set":
            return self._restore_published_change_set(payload)
        if step == "notify.write_policy_required":
            return self._notify_write_policy_required(payload)
        if step == "backlog.register_child_run":
            return self._register_backlog_child_run(payload)
        if step == "backlog.register_run":
            return self._register_backlog_run(payload)
        if step == "backlog.record_ci_repair":
            return self._record_backlog_ci_repair(payload)
        if step == "global_backlog.register":
            return self._register_global_backlog(payload)
        if step == "global_backlog.select_next":
            return self._select_global_backlog_issue(payload)
        if step == "global_backlog.record_result":
            return self._record_global_backlog_result(payload)
        if step == "global_backlog.finalize":
            return self._finalize_global_backlog(payload)
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
            effort=resolve_agent_effort(ModelClass.WORKER),
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
            effort=resolve_agent_effort(ModelClass.JUDGE),
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

    def _persist_backlog_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        import_filter = str(payload["import_filter"])
        maximum_items = int(payload.get("maximum_items", 500))
        snapshot = GitHubBacklogImportSnapshot.model_validate(payload.get("snapshot"))
        pull_requests = tuple(
            pull_request
            for repository in snapshot.repositories
            for pull_request in repository.pull_requests
        )
        records = [
            classify_issue(
                issue,
                pull_requests,
                project=snapshot.project,
                import_filter=import_filter,
                evaluated_at=snapshot.imported_at,
            )
            for repository in snapshot.repositories
            for issue in repository.issues
        ]
        title_groups: dict[str, list[BacklogIssueRecord]] = {}
        for record in records:
            normalized = " ".join(re.findall(r"[a-z0-9]+", record.title.lower()))
            title_groups.setdefault(normalized, []).append(record)
        records = [
            record.model_copy(
                update={
                    "duplicate_candidates": tuple(
                        sorted(
                            candidate.issue_url_reference
                            for candidate in title_groups[
                                " ".join(re.findall(r"[a-z0-9]+", record.title.lower()))
                            ]
                            if candidate.issue_url_reference != record.issue_url_reference
                        )
                    )
                }
            )
            for record in records
        ]
        by_reference = {f"{record.repository}#{record.issue_number}": record for record in records}
        blocked_reverse: dict[str, set[str]] = {reference: set() for reference in by_reference}
        for record in records:
            owner = f"{record.repository}#{record.issue_number}"
            for dependency in record.dependencies:
                if dependency in blocked_reverse:
                    blocked_reverse[dependency].add(owner)
        records = [
            record.model_copy(
                update={
                    "issues_blocked": tuple(
                        sorted(blocked_reverse[f"{record.repository}#{record.issue_number}"])
                    )
                }
            )
            for record in records
        ]
        planned, graph_hash = plan_records(records)
        plan_hash = canonical_sha256(
            [
                {
                    "repository": record.repository,
                    "number": record.issue_number,
                    "body_sha256": record.body_sha256,
                    "updated_at": record.updated_at.isoformat(),
                    "type": record.issue_type.value,
                    "risk": record.calculated_risk,
                    "eligibility": record.eligibility_status,
                    "reason": record.eligibility_reason,
                    "wave": record.planned_wave,
                    "order": record.planned_order,
                    "dependencies": record.dependencies,
                    "linked_prs": [item.model_dump(mode="json") for item in record.linked_prs],
                }
                for record in planned
            ]
        )
        repositories = snapshot.repositories
        deployment_snapshot = repositories[1] if len(repositories) == 2 else repositories[0]
        pair = BACKLOG_PROJECT_PAIRS[snapshot.project]
        orchestration = payload.get("orchestration")
        workflow_id = str(
            orchestration.get("workflow_id", f"backlog-import-{campaign_id}")
            if isinstance(orchestration, dict)
            else f"backlog-import-{campaign_id}"
        )
        counts = {
            "issue_count": len(planned),
            "eligible_count": sum(
                record.eligibility_status == "ELIGIBLE_FOR_REVALIDATION" for record in planned
            ),
            "deferred_count": sum(record.eligibility_status == "DEFERRED" for record in planned),
            "duplicate_count": sum(
                record.issue_type is BacklogIssueType.DUPLICATE for record in planned
            ),
            "stale_count": sum(record.issue_type is BacklogIssueType.STALE for record in planned),
            "already_fixed_count": sum(
                record.issue_type is BacklogIssueType.ALREADY_FIXED for record in planned
            ),
            "existing_pr_count": sum(
                pull_request.state == "OPEN" for pull_request in pull_requests
            ),
            "blocked_count": sum(
                record.eligibility_reason
                in {
                    "NEEDS_PRODUCT_DECISION",
                    "R3_REQUIRES_OPERATOR",
                    "STALE_REQUIRES_CURRENT_EVIDENCE",
                    "ALREADY_FIXED_REQUIRES_PROOF",
                }
                for record in planned
            ),
        }
        with self.sessions.begin() as session:
            existing = session.get(db.GitHubBacklogCampaign, campaign_id)
            if existing is not None:
                if (
                    existing.project != snapshot.project
                    or existing.plan_hash != plan_hash
                    or existing.source_head_at_import != repositories[0].head_sha
                    or existing.deployment_head_at_import != deployment_snapshot.head_sha
                ):
                    raise ValueError("backlog import idempotency binding changed")
                self._ensure_backlog_run_projection(
                    session,
                    workflow_id=workflow_id,
                    project=snapshot.project,
                    campaign_id=campaign_id,
                    orchestration=orchestration,
                )
                return self._backlog_campaign_envelope(existing, "IMPORTED")
            campaign = db.GitHubBacklogCampaign(
                id=campaign_id,
                project=snapshot.project,
                source_repository=pair[0],
                deployment_repository=pair[1],
                imported_at=snapshot.imported_at,
                issue_query="state:open is:issue paired-repositories",
                import_filter=import_filter,
                source_default_branch=repositories[0].default_branch,
                deployment_default_branch=deployment_snapshot.default_branch,
                source_head_at_import=repositories[0].head_sha,
                deployment_head_at_import=deployment_snapshot.head_sha,
                dependency_graph_hash=graph_hash,
                plan_hash=plan_hash,
                approval_binding=None,
                approval_json={},
                maximum_risk=None,
                maximum_items=maximum_items,
                maximum_concurrency=1,
                delivery_mode=None,
                include_enhancements=import_filter == "enhancements",
                status="IMPORTED",
                workflow_id=workflow_id,
                **counts,
            )
            session.add(campaign)
            # The issue model carries only the campaign UUID, not an ORM
            # relationship.  Flush the parent explicitly so PostgreSQL never
            # schedules child inserts ahead of the campaign row.
            session.flush([campaign])
            self._ensure_backlog_run_projection(
                session,
                workflow_id=workflow_id,
                project=snapshot.project,
                campaign_id=campaign_id,
                orchestration=orchestration,
            )
            for record in planned:
                session.add(
                    db.BacklogIssueRecord(
                        id=uuid5(
                            NAMESPACE_URL,
                            f"backlog:{campaign_id}:{record.repository}:{record.issue_number}",
                        ),
                        backlog_campaign_id=campaign_id,
                        repository=record.repository,
                        issue_number=record.issue_number,
                        issue_url_reference=record.issue_url_reference,
                        title=record.title,
                        state=record.state,
                        labels_json=list(record.labels),
                        milestone=record.milestone,
                        issue_type=record.issue_type.value,
                        openspec_kind=record.openspec_kind,
                        severity=record.severity,
                        calculated_risk=record.calculated_risk,
                        component=record.component,
                        source_or_deployment_owner=record.source_or_deployment_owner,
                        documentation_levels_json=list(record.documentation_levels),
                        dependencies_json=list(record.dependencies),
                        blocking_issues_json=list(record.blocking_issues),
                        issues_blocked_json=list(record.issues_blocked),
                        linked_prs_json=[
                            item.model_dump(mode="json") for item in record.linked_prs
                        ],
                        linked_branches_json=list(record.linked_branches),
                        linked_commits_json=list(record.linked_commits),
                        duplicate_candidates_json=list(record.duplicate_candidates),
                        current_source_reproduction=record.current_source_reproduction,
                        current_runtime_reproduction=record.current_runtime_reproduction,
                        current_runtime_required=record.current_runtime_required,
                        reproduction_mode=record.reproduction_mode.value,
                        candidate_status=record.candidate_status,
                        eligibility_status=record.eligibility_status,
                        eligibility_reason=record.eligibility_reason,
                        planned_wave=record.planned_wave,
                        planned_order=record.planned_order,
                        finding_id=None,
                        change_set_id=None,
                        delivery_status=record.delivery_status,
                        final_closure_status=record.final_closure_status,
                        documentation_impact=record.documentation_impact.value,
                        body_sha256=record.body_sha256,
                        github_updated_at=record.updated_at,
                        github_snapshot_json=record.github_snapshot,
                    )
                )
        return self._envelope(
            "backlog.persist_import",
            {
                **payload,
                "backlog_campaign_id": str(campaign_id),
                "dependency_graph_hash": graph_hash,
                "plan_hash": plan_hash,
                **counts,
            },
            "IMPORTED",
        )

    def _backlog_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        with self.sessions.begin() as session:
            campaign = session.get(db.GitHubBacklogCampaign, campaign_id)
            if campaign is None or campaign.project != payload.get("project"):
                return {**payload, "status": "BLOCKED", "blocker": "BACKLOG_CAMPAIGN_NOT_FOUND"}
            issues = session.scalars(
                select(db.BacklogIssueRecord)
                .where(db.BacklogIssueRecord.backlog_campaign_id == campaign_id)
                .order_by(
                    db.BacklogIssueRecord.planned_order,
                    db.BacklogIssueRecord.repository,
                    db.BacklogIssueRecord.issue_number,
                )
            ).all()
            campaign.status = "PLANNED"
            run = session.scalar(
                select(db.Run).where(db.Run.workflow_id == campaign.workflow_id).limit(1)
            )
            if run is not None:
                run.status = "COMPLETED"
                run.finished_at = datetime.now(UTC)
                run.summary = "GitHub backlog import and dependency plan completed"
            summary = self._serialize_backlog_campaign(campaign)
        return self._envelope(
            "backlog.plan",
            {
                **payload,
                "campaign": summary,
                "queue": [self._serialize_backlog_issue(issue) for issue in issues[:50]],
            },
            "PLANNED",
        )

    @staticmethod
    def _ensure_backlog_run_projection(
        session: Session,
        *,
        workflow_id: str,
        project: str,
        campaign_id: UUID,
        orchestration: Any,
    ) -> None:
        """Project imports into the same workflow-detail model used by every channel."""
        now = datetime.now(UTC)
        run = session.scalar(select(db.Run).where(db.Run.workflow_id == workflow_id).limit(1))
        if run is None:
            run = new_bound_run(
                id=uuid5(NAMESPACE_URL, f"backlog-import-run:{workflow_id}"),
                workflow_id=workflow_id,
                run_type="github-backlog-import",
                status="RUNNING",
                project=project,
                worker="controller",
                started_at=now,
                summary="GitHub backlog import is running",
            )
            session.add(run)
        elif run.run_type != "github-backlog-import" or run.project != project:
            raise ValueError("backlog import workflow conflicts with an existing run projection")
        temporal_run_id = (
            str(orchestration.get("workflow_run_id") or "")
            if isinstance(orchestration, dict)
            else ""
        )
        event_id = session.scalar(
            select(db.DashboardEvent.id)
            .where(
                db.DashboardEvent.workflow_id == workflow_id,
                db.DashboardEvent.event_type == "workflow.started",
            )
            .limit(1)
        )
        if event_id is None:
            session.add(
                db.DashboardEvent(
                    occurred_at=now,
                    level="INFO",
                    event_type="workflow.started",
                    source="controller",
                    worker_kind="controller",
                    project=project,
                    workflow_id=workflow_id,
                    temporal_run_id=temporal_run_id or None,
                    campaign_id=str(campaign_id),
                    entity_type="github_backlog_campaign",
                    entity_id=str(campaign_id),
                    current_phase="backlog.persist_import",
                    message="GitHub backlog import started",
                    metadata_json={"status": "RUNNING"},
                )
            )

    def _backlog_approval_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        project = str(payload["project"])
        delivery_mode = str(payload["delivery_mode"])
        maximum_risk = str(payload["maximum_risk"])
        maximum_items = int(payload.get("maximum_items") or 1)
        with self.sessions.begin() as session:
            campaign = session.get(db.GitHubBacklogCampaign, campaign_id)
            if campaign is None or campaign.project != project:
                return {**payload, "status": "BLOCKED", "blocker": "BACKLOG_CAMPAIGN_NOT_FOUND"}
            binding = approval_binding(
                campaign_id=str(campaign_id),
                project=project,  # type: ignore[arg-type]
                import_filter=campaign.import_filter,  # type: ignore[arg-type]
                source_head=campaign.source_head_at_import,
                deployment_head=campaign.deployment_head_at_import,
                dependency_plan_hash=campaign.plan_hash,
                maximum_risk=maximum_risk,  # type: ignore[arg-type]
                maximum_items=maximum_items,
                delivery_mode=delivery_mode,  # type: ignore[arg-type]
                include_enhancements=campaign.import_filter == "enhancements",
            )
            digest = binding.sha256()
            campaign.approval_binding = digest
            campaign.approval_json = binding.model_dump(mode="json")
            campaign.maximum_risk = maximum_risk
            campaign.maximum_items = maximum_items
            campaign.maximum_concurrency = 1
            campaign.delivery_mode = delivery_mode
            campaign.include_enhancements = binding.include_enhancements
            campaign.status = "AWAITING_APPROVAL"
        return self._envelope(
            "backlog.approval_candidate",
            {
                **payload,
                "approval_binding": digest,
                "approval_candidate": binding.model_dump(mode="json"),
                "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
            },
            "AWAITING_APPROVAL",
        )

    def _record_backlog_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        supplied = str(payload.get("approval_binding", ""))
        confirmation = str(payload.get("typed_confirmation", ""))
        actor = str(payload.get("actor", ""))
        if (
            confirmation != BACKLOG_APPROVAL_PHRASE
            or not actor.startswith("telegram:")
            or not is_reauthenticated_backlog_operator(actor)
        ):
            return {**payload, "status": "BLOCKED", "blocker": "BACKLOG_APPROVAL_INVALID"}
        with self.sessions.begin() as session:
            campaign = session.get(db.GitHubBacklogCampaign, campaign_id)
            if campaign is None or campaign.approval_binding != supplied:
                return {**payload, "status": "BLOCKED", "blocker": "APPROVAL_BINDING_CHANGED"}
            approval = dict(campaign.approval_json)
            if canonical_sha256(approval) != supplied:
                raise ValueError("persisted backlog approval candidate hash changed")
            expires_at = datetime.fromisoformat(str(approval["expires_at"]))
            if expires_at <= datetime.now(UTC):
                return {**payload, "status": "BLOCKED", "blocker": "APPROVAL_EXPIRED"}
            campaign.status = "APPROVED"
            campaign.approval_json = {
                **approval,
                "approved_by": actor,
                "approved_at": datetime.now(UTC).isoformat(),
                "typed_confirmation": BACKLOG_APPROVAL_PHRASE,
            }
        return self._envelope(
            "backlog.record_approval",
            {**payload, "policy_activation": "PENDING_ROOT_RECONCILER"},
            "APPROVED",
        )

    def _record_backlog_skip(self, payload: dict[str, Any]) -> dict[str, Any]:
        reason = str(payload.get("reason_code", ""))
        if reason not in BACKLOG_SKIP_REASONS:
            raise ValueError("backlog skip reason is not allowlisted")
        issue = self._backlog_issue_for_update(payload)
        with self.sessions.begin() as session:
            row = session.get(db.BacklogIssueRecord, issue.id)
            if row is None:
                raise ValueError("backlog issue disappeared during skip")
            row.candidate_status = "SKIPPED"
            row.eligibility_status = "DEFERRED"
            row.eligibility_reason = reason
            row.delivery_status = "SKIPPED"
        return self._envelope("backlog.record_skip", payload, "SKIPPED")

    def _record_backlog_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        issue = self._backlog_issue_for_update(payload)
        with self.sessions.begin() as session:
            row = session.get(db.BacklogIssueRecord, issue.id)
            if row is None:
                raise ValueError("backlog issue disappeared during retry")
            row.candidate_status = "NEEDS_CURRENT_STATE_REVALIDATION"
            row.eligibility_status = "ELIGIBLE_FOR_REVALIDATION"
            row.eligibility_reason = "OPERATOR_RETRY_REQUESTED"
            row.delivery_status = "NOT_STARTED"
        return self._envelope("backlog.record_retry", payload, "RETRY_QUEUED")

    def _load_backlog_execution(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        risk_rank = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
        maximum_risk = str(payload.get("maximum_risk", "R1"))
        maximum = risk_rank.get(maximum_risk)
        if maximum is None:
            raise ValueError("backlog execution maximum risk is invalid")
        with self.sessions.begin() as session:
            try:
                self._require_direct_issue_operation(session, payload)
            except ValueError:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "DIRECT_ISSUE_APPROVAL_REQUIRED_OR_CHANGED",
                }
            campaign = session.get(db.GitHubBacklogCampaign, campaign_id)
            global_campaign = None
            raw_global_id = payload.get("global_campaign_id")
            if raw_global_id is not None:
                global_campaign = session.get(db.Campaign, UUID(str(raw_global_id)))
            globally_authorized = self._global_campaign_authorizes(
                session,
                global_campaign,
                campaign_id,
                maximum_risk,
                str(payload.get("delivery_mode")),
                str(payload.get("target_repository") or ""),
                int(payload.get("target_issue_number") or 0),
                str(payload.get("r3_approval_binding") or ""),
            )
            base_maximum_risk = "R2" if global_campaign is not None else maximum_risk
            approval = _current_backlog_approval(
                campaign,
                delivery_mode=str(payload.get("delivery_mode")),
                maximum_risk=base_maximum_risk,
                now=datetime.now(UTC),
            )
            if (
                campaign is None
                or campaign.project != payload.get("project")
                or approval is None
                or (global_campaign is None and campaign.status == "APPROVED_INVENTORY")
                or (raw_global_id is not None and not globally_authorized)
            ):
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "CURRENT_BACKLOG_APPROVAL_REQUIRED",
                }
            try:
                require_backlog_issue_approval(session, approval)
                reference = approval.issue_approval
                if reference is not None and (
                    reference.operation_id != payload.get("issue_operation_id")
                    or reference.binding_sha256 != payload.get("issue_approval_binding")
                ):
                    raise ValueError("issue campaign and workflow approval differ")
            except ValueError:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "DIRECT_ISSUE_APPROVAL_REQUIRED_OR_CHANGED",
                }
            rows = session.scalars(
                select(db.BacklogIssueRecord)
                .where(
                    db.BacklogIssueRecord.backlog_campaign_id == campaign_id,
                    _selectable_backlog_issue(),
                )
                .order_by(db.BacklogIssueRecord.planned_order)
            ).all()
            # NEW-114: a loop refilling a freed slot excludes the issues it already holds.
            raw_excluded = payload.get("exclude_issue_refs")
            excluded = (
                {str(value)[:240] for value in raw_excluded[:500]}
                if isinstance(raw_excluded, list)
                else set()
            )
            rows = [
                row
                for row in rows
                if f"{row.repository}#{row.issue_number}" not in excluded
                # NEW-114 (fix 5): Hermes's own operational issues are never backlog work,
                # even in campaigns imported before the classifier excluded them.
                and not is_hermes_operational_issue(
                    row.title,
                    str((row.github_snapshot_json or {}).get("body_excerpt") or "")
                    if isinstance(row.github_snapshot_json, dict)
                    else "",
                )
            ]
            target_repository = payload.get("target_repository")
            target_issue_number = payload.get("target_issue_number")
            if target_repository is not None or target_issue_number is not None:
                if not isinstance(target_repository, str) or not isinstance(
                    target_issue_number, int
                ):
                    raise ValueError("direct issue selection binding is incomplete")
                rows = [
                    row
                    for row in rows
                    if row.repository == target_repository
                    and row.issue_number == target_issue_number
                ]
            selected = [
                self._serialize_backlog_issue(row)
                for row in rows
                if _deferred_r3_row_in_filter(row, campaign.import_filter)
                and risk_rank.get(row.calculated_risk, 99) <= maximum
                and (
                    row.calculated_risk != "R3"
                    or self._valid_r3_candidate(
                        session,
                        row,
                        str(payload.get("r3_approval_binding") or ""),
                    )
                )
            ][: int(payload.get("maximum_items", 1))]
            if target_repository is not None and not selected:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "DIRECT_ISSUE_NOT_CURRENTLY_ELIGIBLE",
                }
            if campaign.status in {"APPROVED_POLICY_PENDING", "APPROVED"}:
                campaign.status = "RUNNING"
        return self._envelope(
            "backlog.load_execution",
            {**payload, "items": selected},
            "SELECTED",
        )

    @staticmethod
    def _global_campaign_authorizes(
        session: Any,
        campaign: db.Campaign | None,
        backlog_campaign_id: UUID,
        maximum_risk: str,
        delivery_mode: str,
        target_repository: str,
        target_issue_number: int,
        r3_approval_binding: str,
    ) -> bool:
        return _global_campaign_authorizes(
            session,
            campaign,
            backlog_campaign_id,
            maximum_risk,
            delivery_mode,
            target_repository,
            target_issue_number,
            r3_approval_binding,
        )

    @staticmethod
    def _valid_r3_candidate(
        session: Session,
        issue: db.BacklogIssueRecord,
        binding: str | None = None,
    ) -> db.ActionCandidate | None:
        return _valid_r3_candidate(session, issue, binding)

    def _register_global_backlog(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        raw_ids = payload.get("backlog_campaign_ids")
        if not isinstance(raw_ids, dict) or set(raw_ids) != {"falcone", "musematic", "llmwiki"}:
            raise ValueError("global backlog requires one inventory for every project")
        campaign_ids = {project: UUID(str(value)) for project, value in raw_ids.items()}
        policy_campaign_id = UUID(str(payload["policy_campaign_id"]))
        workflow_id = str(
            (payload.get("orchestration") or {}).get(
                "workflow_id", f"autonomous-backlog-staging-{campaign_id}"
            )
        )
        temporal_run_id = str((payload.get("orchestration") or {}).get("workflow_run_id") or "")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            backlogs = {
                project: session.get(db.GitHubBacklogCampaign, value)
                for project, value in campaign_ids.items()
            }
            if any(
                campaign is None or campaign.project != project
                for project, campaign in backlogs.items()
            ):
                raise ValueError("global backlog inventory binding is invalid")
            delivery_modes = {
                "falcone": "full-delivery",
                "musematic": "full-delivery",
                "llmwiki": "source-pr",
            }
            approvals = {
                project: _current_backlog_approval(
                    backlog,
                    delivery_mode=delivery_modes[project],
                    maximum_risk="R2",
                    now=now,
                )
                for project, backlog in backlogs.items()
            }
            if any(binding is None for binding in approvals.values()):
                raise ValueError(
                    "global backlog requires a current immutable approval for every inventory"
                )
            policy_campaign = session.get(db.GitHubBacklogCampaign, policy_campaign_id)
            owner_ids = {
                backlog.id
                for backlog in backlogs.values()
                if backlog is not None
                and backlog.approval_json.get("policy_role") in {None, "owner"}
            }
            if (
                policy_campaign is None
                or policy_campaign_id not in campaign_ids.values()
                or owner_ids != {policy_campaign_id}
                or policy_campaign.status not in {"APPROVED", "RUNNING"}
                or policy_campaign.approval_json.get("policy_role") not in {None, "owner"}
            ):
                raise ValueError("global backlog requires one active exact GitHub write policy")
            inventory: list[dict[str, Any]] = []
            plan_hashes: dict[str, str] = {}
            for project, backlog in backlogs.items():
                assert backlog is not None
                plan_hashes[project] = backlog.plan_hash
                rows = session.scalars(
                    select(db.BacklogIssueRecord)
                    .where(db.BacklogIssueRecord.backlog_campaign_id == backlog.id)
                    .order_by(db.BacklogIssueRecord.planned_order)
                ).all()
                inventory.extend(
                    {
                        "project": project,
                        "backlog_campaign_id": str(backlog.id),
                        "repository": row.repository,
                        "issue_number": row.issue_number,
                        "body_sha256": row.body_sha256,
                        "updated_at": row.github_updated_at.isoformat(),
                        "risk": row.calculated_risk,
                        "wave": row.planned_wave,
                        "order": row.planned_order,
                    }
                    for row in rows
                )
            plan_hash = canonical_sha256(
                {"campaigns": raw_ids, "plan_hashes": plan_hashes, "inventory": inventory}
            )
            existing = session.get(db.Campaign, campaign_id)
            if existing is None:
                existing = db.Campaign(
                    id=campaign_id,
                    project="platform",
                    name="autonomous-all-project-staging-backlog",
                    status="RUNNING",
                    required_cells=len(inventory),
                    metadata_json={
                        "kind": "autonomous-global-backlog",
                        "workflow_id": workflow_id,
                        "temporal_run_id": temporal_run_id,
                        "backlog_campaign_ids": {
                            key: str(value) for key, value in campaign_ids.items()
                        },
                        "policy_campaign_id": str(policy_campaign_id),
                        "environment": "staging",
                        "maximum_risk": "R2",
                        "delivery_modes": delivery_modes,
                        "inventory_approval_bindings": {
                            project: binding.sha256()
                            for project, binding in approvals.items()
                            if binding is not None
                        },
                        "production": False,
                        "global_active_issues": 1,
                        "global_deployments": 1,
                        "plan_hash": plan_hash,
                        "inventory": inventory,
                        "current_issue": None,
                        "completed_issues": [],
                        "blocked_issues": [],
                        "skipped_issues": [],
                        "external_effect_ledger": [],
                        "notification_watermark": None,
                    },
                )
                session.add(existing)
                session.flush([existing])
            else:
                metadata = existing.metadata_json
                if (
                    existing.project != "platform"
                    or not isinstance(metadata, dict)
                    or metadata.get("kind") != "autonomous-global-backlog"
                    or metadata.get("plan_hash") != plan_hash
                    or metadata.get("workflow_id") != workflow_id
                ):
                    raise ValueError("global backlog idempotency binding changed")
                existing.status = "RUNNING"
            run = session.scalar(select(db.Run).where(db.Run.workflow_id == workflow_id).limit(1))
            if run is None:
                session.add(
                    new_bound_run(
                        id=uuid5(NAMESPACE_URL, f"global-backlog-run:{workflow_id}"),
                        campaign_id=campaign_id,
                        workflow_id=workflow_id,
                        run_type="autonomous-global-backlog",
                        status="RUNNING",
                        project="platform",
                        worker="controller",
                        started_at=now,
                        summary="Global staging backlog campaign is selecting one issue at a time",
                    )
                )
            event = session.scalar(
                select(db.DashboardEvent.id)
                .where(
                    db.DashboardEvent.workflow_id == workflow_id,
                    db.DashboardEvent.event_type == "campaign.started",
                )
                .limit(1)
            )
            if event is None:
                session.add(
                    db.DashboardEvent(
                        occurred_at=now,
                        level="INFO",
                        event_type="campaign.started",
                        source="controller",
                        worker_kind="controller",
                        project="platform",
                        workflow_id=workflow_id,
                        temporal_run_id=temporal_run_id or None,
                        campaign_id=str(campaign_id),
                        entity_type="autonomous_global_backlog",
                        entity_id=str(campaign_id),
                        current_phase="GLOBAL_ISSUE_SELECTION",
                        message="Autonomous global staging backlog campaign started",
                        metadata_json={"status": "RUNNING", "plan_hash": plan_hash},
                    )
                )
        return {**payload, "status": "RUNNING", "plan_hash": plan_hash}

    def _select_global_backlog_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        risk_rank = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
        wave_rank = {"W0": 0, "W1": 1, "W2": 2, "W3": 3, "W4": 4, "W5": 5}
        severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unspecified": 4}
        with self.sessions.begin() as session:
            campaign = session.get(db.Campaign, campaign_id)
            if campaign is None or campaign.status not in {"RUNNING", "PAUSED"}:
                raise ValueError("global backlog campaign is not active")
            metadata = dict(campaign.metadata_json)
            processed = {
                str(item.get("reference"))
                for key in ("completed_issues", "skipped_issues", "blocked_issues")
                for item in metadata.get(key, [])
                if isinstance(item, dict)
                and (key != "blocked_issues" or item.get("retryable") is False)
            }
            now = datetime.now(UTC)
            blocked_until = {
                str(item.get("reference")): _as_utc(
                    datetime.fromisoformat(str(item["retry_after"]))
                )
                for item in metadata.get("blocked_issues", [])
                if isinstance(item, dict) and item.get("reference") and item.get("retry_after")
            }
            ids = metadata.get("backlog_campaign_ids")
            if not isinstance(ids, dict):
                raise ValueError("global backlog inventory binding is missing")
            candidates: list[tuple[str, db.BacklogIssueRecord]] = []
            awaiting_r3_approval = False
            for project, raw_id in ids.items():
                inventory = session.get(db.GitHubBacklogCampaign, UUID(str(raw_id)))
                import_filter = inventory.import_filter if inventory is not None else ""
                rows = session.scalars(
                    select(db.BacklogIssueRecord).where(
                        db.BacklogIssueRecord.backlog_campaign_id == UUID(str(raw_id)),
                        db.BacklogIssueRecord.delivery_status.in_({"NOT_STARTED", "RETRY_PENDING"}),
                        _selectable_backlog_issue(),
                    )
                ).all()
                for row in rows:
                    reference = f"{row.repository}#{row.issue_number}"
                    r3_candidate = (
                        self._valid_r3_candidate(session, row)
                        if row.calculated_risk == "R3"
                        else None
                    )
                    selectable = (
                        _deferred_r3_row_in_filter(row, import_filter)
                        and reference not in processed
                        and blocked_until.get(reference, now) <= now
                        and row.issue_type != BacklogIssueType.ENHANCEMENT.value
                    )
                    if not selectable:
                        continue
                    if row.calculated_risk == "R3" and r3_candidate is None:
                        # NEW-96 review: only an R3 issue the loop would otherwise run makes it
                        # wait for an approval (not processed, enhancement or blocked rows).
                        awaiting_r3_approval = True
                    if risk_rank.get(row.calculated_risk, 99) <= 2 or r3_candidate is not None:
                        candidates.append((project, row))
            if not candidates:
                metadata["current_issue"] = None
                campaign.metadata_json = metadata
                pending_retries = [value for value in blocked_until.values() if value > now]
                if pending_retries or awaiting_r3_approval:
                    retry_seconds = max(
                        30,
                        int((min(pending_retries) - now).total_seconds())
                        if pending_retries
                        else 900,
                    )
                    return {
                        **payload,
                        "status": "WAITING",
                        "item": None,
                        "retry_after_seconds": min(retry_seconds, 900),
                        "blocker": (
                            "AWAITING_R3_APPROVAL"
                            if awaiting_r3_approval and not pending_retries
                            else "BLOCKED_ISSUE_RETRY_DELAY"
                        ),
                    }
                return {**payload, "status": "EMPTY", "item": None}
            project, row = min(
                candidates,
                key=lambda value: (
                    wave_rank.get(value[1].planned_wave, 99),
                    severity_rank.get(value[1].severity, 99),
                    -len(value[1].issues_blocked_json or []),
                    risk_rank.get(value[1].calculated_risk, 99),
                    value[1].repository,
                    value[1].issue_number,
                ),
            )
            item = {
                "project": project,
                "backlog_campaign_id": str(row.backlog_campaign_id),
                "repository": row.repository,
                "issue_number": row.issue_number,
                "reference": f"{row.repository}#{row.issue_number}",
                "risk": row.calculated_risk,
                "wave": row.planned_wave,
                "delivery_mode": str(
                    (metadata.get("delivery_modes") or {}).get(project) or "source-pr"
                ),
            }
            if row.calculated_risk == "R3":
                r3_candidate = self._valid_r3_candidate(session, row)
                if r3_candidate is None:
                    raise ValueError("approved R3 candidate disappeared during selection")
                item["execution_maximum_risk"] = "R3"
                item["r3_approval_binding"] = r3_candidate.binding_hash
            metadata["current_issue"] = item
            metadata["notification_watermark"] = datetime.now(UTC).isoformat()
            campaign.metadata_json = metadata
            run = session.scalar(select(db.Run).where(db.Run.campaign_id == campaign_id).limit(1))
            if run is not None:
                run.status = "RUNNING"
                run.summary = f"Current issue: {item['reference']}"
            session.add(
                db.DashboardEvent(
                    occurred_at=datetime.now(UTC),
                    level="INFO",
                    event_type="campaign.issue_selected",
                    source="controller",
                    worker_kind="controller",
                    project=project,
                    workflow_id=str(metadata["workflow_id"]),
                    temporal_run_id=str(metadata.get("temporal_run_id") or "") or None,
                    issue_number=row.issue_number,
                    campaign_id=str(campaign_id),
                    entity_type="github_issue",
                    entity_id=item["reference"],
                    current_phase="CURRENT_STATE_REVALIDATION",
                    message="Global backlog selected one eligible issue",
                    metadata_json={"status": "RUNNING", "risk": row.calculated_risk},
                )
            )
        return {**payload, "status": "SELECTED", "item": item}

    def _record_global_backlog_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        item = payload.get("item")
        result = payload.get("result")
        if not isinstance(item, dict) or not isinstance(result, dict):
            raise ValueError("global backlog result binding is invalid")
        status = str(result.get("status") or "FAILED")
        blocker = str(result.get("blocker") or "")[:120] or None
        terminal_bucket = (
            "completed_issues"
            if status in {"COMPLETE", "COMPLETED", "SUCCESS", "FIXED"}
            else "skipped_issues"
            if status == "SKIPPED"
            else "blocked_issues"
        )
        permanent_blocker = (
            blocker in _TERMINAL_GLOBAL_BLOCKERS
            or bool(blocker and blocker.startswith("FINAL_"))
            or status in {"BLOCKED_AUTONOMOUS_REPAIR_EXHAUSTED", "NEEDS_PRODUCT_DECISION"}
        )
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            campaign = session.get(db.Campaign, campaign_id)
            if campaign is None:
                raise ValueError("global backlog campaign disappeared")
            metadata = dict(campaign.metadata_json)
            prior = next(
                (
                    existing
                    for existing in metadata.get("blocked_issues", [])
                    if isinstance(existing, dict)
                    and existing.get("reference") == str(item["reference"])
                ),
                None,
            )
            attempt = int((prior or {}).get("attempt") or 0) + 1
            retryable = (
                terminal_bucket == "blocked_issues"
                and not permanent_blocker
                and attempt < _GLOBAL_BACKLOG_RETRY_LIMIT
            )
            retry_minutes = min(
                _GLOBAL_BACKLOG_RETRY_BASE_MINUTES * (2 ** max(0, attempt - 1)),
                360,
            )
            record = {
                "reference": str(item["reference"]),
                "status": status,
                "blocker": blocker,
                "recorded_at": now.isoformat(),
                "attempt": attempt if terminal_bucket == "blocked_issues" else 0,
                "retryable": retryable if terminal_bucket == "blocked_issues" else False,
                "retry_after": (
                    (now + timedelta(minutes=retry_minutes)).isoformat() if retryable else None
                ),
                "retry_exhausted": (
                    terminal_bucket == "blocked_issues"
                    and not permanent_blocker
                    and attempt >= _GLOBAL_BACKLOG_RETRY_LIMIT
                ),
            }
            for other_bucket in (
                "completed_issues",
                "blocked_issues",
                "skipped_issues",
            ):
                if other_bucket != terminal_bucket:
                    metadata[other_bucket] = [
                        existing
                        for existing in metadata.get(other_bucket, [])
                        if not (
                            isinstance(existing, dict)
                            and existing.get("reference") == record["reference"]
                        )
                    ]
            bucket = list(metadata.get(terminal_bucket, []))
            bucket = [
                existing
                for existing in bucket
                if not (
                    isinstance(existing, dict) and existing.get("reference") == record["reference"]
                )
            ]
            bucket.append(record)
            metadata[terminal_bucket] = bucket
            metadata["current_issue"] = None
            metadata["notification_watermark"] = record["recorded_at"]
            campaign.metadata_json = metadata
            issue = session.scalar(
                select(db.BacklogIssueRecord).where(
                    db.BacklogIssueRecord.backlog_campaign_id
                    == UUID(str(item["backlog_campaign_id"])),
                    db.BacklogIssueRecord.repository == str(item["repository"]),
                    db.BacklogIssueRecord.issue_number == int(item["issue_number"]),
                )
            )
            if issue is not None and blocker == "FINAL_MERGED_REVISION_DELIVERY_SERVICE_REQUIRED":
                issue.delivery_status = "CANDIDATE_VERIFIED_FINAL_SERVICE_BLOCKED"
                issue.final_closure_status = "BLOCKED_FINAL_SERVICE"
            elif issue is not None and retryable:
                issue.delivery_status = "RETRY_PENDING"
            run = session.scalar(select(db.Run).where(db.Run.campaign_id == campaign_id).limit(1))
            if run is not None:
                run.status = "RUNNING"
                run.summary = f"Last issue result: {record['reference']} {status}"
        return {**payload, "status": "RUNNING", "last_result": record}

    def _finalize_global_backlog(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["campaign_id"]))
        terminal = str(payload.get("terminal_status", "COMPLETE"))
        if terminal not in {"COMPLETE", "STOPPED", "CANCELLED"}:
            raise ValueError("global backlog terminal state is invalid")
        now = datetime.now(UTC)
        deactivation: dict[str, Any] | None = None
        with self.sessions.begin() as session:
            campaign = session.get(db.Campaign, campaign_id)
            if campaign is None:
                raise ValueError("global backlog campaign disappeared")
            campaign.status = terminal
            metadata = dict(campaign.metadata_json)
            metadata["current_issue"] = None
            metadata["terminal_at"] = now.isoformat()
            campaign.metadata_json = metadata
            raw_policy_id = metadata.get("policy_campaign_id")
            raw_inventory_ids = metadata.get("backlog_campaign_ids")
            if raw_policy_id is None or not isinstance(raw_inventory_ids, dict):
                raise ValueError("global backlog policy binding is missing")
            policy_campaign = session.get(db.GitHubBacklogCampaign, UUID(str(raw_policy_id)))
            if (
                policy_campaign is None
                or str(policy_campaign.id)
                not in {str(value) for value in raw_inventory_ids.values()}
                or policy_campaign.approval_json.get("policy_role") not in {None, "owner"}
            ):
                raise ValueError("global backlog policy campaign is invalid")
            raw_approval = policy_campaign.approval_json.get("approval")
            approval = (
                raw_approval if isinstance(raw_approval, dict) else policy_campaign.approval_json
            )
            actor = approval.get("approved_by") if isinstance(approval, dict) else None
            if not isinstance(actor, str) or not is_reauthenticated_backlog_operator(actor):
                raise ValueError("global backlog policy approval evidence is missing")
            for raw_inventory_id in raw_inventory_ids.values():
                inventory = session.get(db.GitHubBacklogCampaign, UUID(str(raw_inventory_id)))
                if inventory is not None:
                    inventory.status = terminal
            deactivation = {
                "action": "disable-existing-backlog",
                "backlog_campaign_id": str(policy_campaign.id),
                "project": policy_campaign.project,
                "approval_binding": policy_campaign.approval_binding,
                "safe_repositories": [],
                "requested_by": actor,
                "requested_at": now.isoformat(),
            }
            run = session.scalar(select(db.Run).where(db.Run.campaign_id == campaign_id).limit(1))
            if run is not None:
                run.status = terminal
                run.finished_at = now
                run.summary = f"Global backlog campaign reached {terminal}"
        if deactivation is not None:
            self._write_policy_request(deactivation)
        return {**payload, "status": terminal}

    def _runtime_proven_after_deploy(self, payload: dict[str, Any]) -> bool:
        """NEW-69: full delivery with an enabled staging flow verifies the runtime."""
        if payload.get("delivery_mode") != "full-delivery":
            return False
        try:
            enabled_staging_flow(self.settings.project_config_root, str(payload.get("project")))
        except (StagingFlowBlocked, ValueError):
            return False
        return True

    @staticmethod
    def _runtime_proof_waived(payload: dict[str, Any]) -> bool:
        """NEW-88: the operator accepted, for one source-pr issue operation, the independent
        current-source revalidation as the proof of a runtime/deployment-gated issue."""
        return (
            payload.get("runtime_proof") == "source-revalidation"
            and payload.get("delivery_mode") == "source-pr"
            and payload.get("target_issue_number") is not None
        )

    def _backlog_revalidate_gate(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.sessions() as session:
            try:
                self._require_direct_issue_operation(session, payload)
            except ValueError:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "DIRECT_ISSUE_APPROVAL_REQUIRED_OR_CHANGED",
                }
        item = payload.get("item")
        if not isinstance(item, dict):
            raise ValueError("backlog revalidation requires one typed issue record")
        source = str(item.get("current_source_reproduction", ""))
        runtime = str(item.get("current_runtime_reproduction", ""))
        mode = str(item.get("reproduction_mode", ""))
        linked = item.get("linked_prs")
        adoptable = (
            any(
                isinstance(value, dict) and value.get("status") == "ADOPTABLE_ACTIVE_PR"
                for value in linked
            )
            if isinstance(linked, list)
            else False
        )
        if adoptable:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "EXISTING_PR_ADOPTION_RECONCILIATION_REQUIRED",
            }
        reproduced = source in {
            "REPRODUCED_CURRENT_SOURCE",
            "REPRODUCED_CURRENT_DEPLOYMENT",
            "PROVEN_ALREADY_FIXED",
        }
        # NEW-69 (operator decision 2026-09-26): no step records a runtime reproduction,
        # so RUNTIME_REQUIRED issues (any text mentioning staging, login, 404, ...) could
        # never be delivered. In full delivery through an enabled staging flow, the
        # post-deploy verification (Argo sync + workloads on the new image) is the runtime
        # proof; such issues proceed on the current-source revalidation.
        runtime_gate = mode in {"RUNTIME_REQUIRED", "DEPLOYMENT_REQUIRED"} and not (
            self._runtime_proven_after_deploy(payload) or self._runtime_proof_waived(payload)
        )
        # NEW-88: the independent current-source revalidation always runs first; before, a
        # runtime-gated issue paused on the runtime blocker without ever being revalidated.
        if not reproduced:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "CURRENT_SOURCE_REVALIDATION_REQUIRED",
            }
        if runtime_gate and runtime not in {
            "REPRODUCED_CURRENT_RUNTIME",
            "REPRODUCED_CURRENT_DEPLOYMENT",
        }:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "CURRENT_RUNTIME_REVALIDATION_REQUIRED",
            }
        if not item.get("finding_id"):
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "REVALIDATED_ISSUE_FINDING_MATERIALIZATION_REQUIRED",
            }
        with self.sessions() as session:
            finding = session.get(db.Finding, UUID(str(item["finding_id"])))
            if finding is None or finding.disposition != "CONFIRMED":
                raise ValueError("materialized backlog finding is not confirmed")
            finding_id = str(finding.id)
        return self._envelope(
            "backlog.revalidate_gate",
            {**payload, "finding_id": finding_id, "risk_class": item["calculated_risk"]},
            "READY",
        )

    @staticmethod
    def _require_direct_issue_operation(session: Session, payload: dict[str, Any]) -> None:
        operation_id = payload.get("issue_operation_id")
        binding = payload.get("issue_approval_binding")
        direct = (
            payload.get("target_repository") is not None
            or payload.get("target_issue_number") is not None
        )
        if operation_id is None and binding is None:
            if direct:
                request = ExistingBacklogLoopStart.model_validate(
                    {
                        key: payload[key]
                        for key in ExistingBacklogLoopStart.model_fields
                        if key in payload
                    }
                )
                require_alternative_issue_authority(session, request)
            return
        if not direct or not isinstance(binding, str) or operation_id is None:
            raise ValueError("direct issue approval binding is incomplete")
        request = ExistingBacklogLoopStart.model_validate(
            {key: payload[key] for key in ExistingBacklogLoopStart.model_fields if key in payload}
        )
        require_issue_request_approval(session, request)

    def _materialize_backlog_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = payload.get("item")
        if not isinstance(item, dict):
            raise ValueError("backlog materialization requires one typed issue record")
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        repository = str(item["repository"])
        issue_number = int(item["issue_number"])
        issue_key = f"{repository}#{issue_number}"
        coverage_id = uuid5(NAMESPACE_URL, f"backlog-coverage:{campaign_id}:{issue_key}")
        fingerprint = canonical_sha256(
            {
                "campaign": str(campaign_id),
                "repository": repository,
                "issue_number": issue_number,
                "body_sha256": item["body_sha256"],
            }
        )
        finding_id = uuid5(NAMESPACE_URL, f"backlog-finding:{fingerprint}")
        issue_id = uuid5(NAMESPACE_URL, f"backlog-existing-issue:{campaign_id}:{issue_key}")
        discoverer = "codex" if issue_number % 2 == 0 else "claude"
        with self.sessions.begin() as session:
            backlog = session.get(db.GitHubBacklogCampaign, campaign_id)
            record = session.scalar(
                select(db.BacklogIssueRecord).where(
                    db.BacklogIssueRecord.backlog_campaign_id == campaign_id,
                    db.BacklogIssueRecord.repository == repository,
                    db.BacklogIssueRecord.issue_number == issue_number,
                )
            )
            if backlog is None or record is None:
                raise ValueError("backlog materialization record disappeared")
            campaign = session.get(db.Campaign, campaign_id)
            if campaign is None:
                campaign = db.Campaign(
                    id=campaign_id,
                    project=backlog.project,
                    name=f"existing-github-backlog-{campaign_id}",
                    status="BACKLOG_REVALIDATION",
                    source_sha=backlog.source_head_at_import,
                    deployment_sha=backlog.deployment_head_at_import,
                    required_cells=backlog.issue_count,
                    metadata_json={
                        "kind": "existing-github-backlog",
                        "backlog_campaign_id": str(campaign_id),
                        "plan_hash": backlog.plan_hash,
                    },
                )
                session.add(campaign)
            coverage = session.get(db.CoverageCell, coverage_id)
            if coverage is None:
                # NEW-16 follow-up, 2026-09-22: uq_coverage_cell is keyed on
                # (campaign_id, repository_surface, domain, persona, flow, lens,
                # environment) -- NOT this row's own primary key. persona/flow/
                # lens/environment are fixed literals for every backlog issue
                # (see below), so domain is the only column with enough
                # variability to distinguish issues -- but plain
                # record.component collides the moment two issues in the same
                # campaign share a component, hit live in production on the
                # first campaign that ever attempted more than one issue.
                # Suffixing the issue reference guarantees a unique domain per
                # issue while keeping the component prefix for readability;
                # truncated so the issue suffix always survives even if
                # component (up to 200 chars) alone would have exceeded 120.
                domain_suffix = f":{issue_key}"
                domain = (record.component[: 120 - len(domain_suffix)] + domain_suffix)[:120]
                coverage = db.CoverageCell(
                    id=coverage_id,
                    campaign_id=campaign_id,
                    repository_surface=record.source_or_deployment_owner,
                    domain=domain,
                    persona="existing-github-issue",
                    flow="current-state-revalidation",
                    lens="correctness",
                    environment="staging" if record.current_runtime_required else "source",
                    status="CANDIDATE_FOUND",
                    blocker=None,
                    exclusion_approval_id=None,
                    evidence_json=[
                        {
                            "kind": "github-issue-reference",
                            "url": record.issue_url_reference,
                            "body_sha256": record.body_sha256,
                        }
                    ],
                    result_json={
                        "provider": discoverer,
                        "decision": "FAIL",
                        "source": "existing-github-backlog-import",
                    },
                )
                session.add(coverage)
            finding = session.get(db.Finding, finding_id)
            if finding is None:
                snapshot = record.github_snapshot_json
                excerpt = str(snapshot.get("body_excerpt") or "")[:4000]
                finding = db.Finding(
                    id=finding_id,
                    campaign_id=campaign_id,
                    coverage_cell_id=coverage_id,
                    fingerprint=fingerprint,
                    project=backlog.project,
                    domain=record.component[:120],
                    persona="existing-github-issue",
                    severity=("medium" if record.severity == "unspecified" else record.severity),
                    confidence=0.5,
                    risk_class=record.calculated_risk,
                    disposition="CANDIDATE",
                    expected_behavior=record.title,
                    actual_behavior=excerpt or "See the immutable existing GitHub issue reference.",
                    evidence_json=[
                        {
                            "kind": "github-issue-reference",
                            "url": record.issue_url_reference,
                            "body_sha256": record.body_sha256,
                            "updated_at": record.github_updated_at.isoformat(),
                        }
                    ],
                    minimal_reproduction=[
                        {
                            "step": "revalidate-existing-issue",
                            "issue_url": record.issue_url_reference,
                            "reproduction_mode": record.reproduction_mode,
                        }
                    ],
                    affected_source_sha=backlog.source_head_at_import,
                    affected_deployment_sha=backlog.deployment_head_at_import,
                    affected_image_digest=None,
                    affected_component=record.component,
                    impact_json={
                        "repository_surface": record.source_or_deployment_owner,
                        "flow": "current-state-revalidation",
                        "existing_issue": record.issue_url_reference,
                        "documentation_impact": record.documentation_impact,
                    },
                    recommended_action=(
                        "Reproduce against the current immutable source/deployment pair, then "
                        "apply only the smallest issue-bound change."
                    ),
                )
                session.add(finding)
            existing_issue = session.get(db.Issue, issue_id)
            if existing_issue is None:
                session.add(
                    db.Issue(
                        id=issue_id,
                        finding_id=finding_id,
                        repository=repository,
                        number=issue_number,
                        url=record.issue_url_reference,
                        state="OPEN",
                        openspec_change=None,
                    )
                )
            record.finding_id = finding_id
            record.candidate_status = "CANDIDATE_MATERIALIZED"
        return self._envelope(
            "backlog.materialize_candidate",
            {**payload, "finding_id": str(finding_id), "project": str(payload["project"])},
            "CANDIDATE_MATERIALIZED",
        )

    def _record_backlog_revalidation(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = payload.get("item")
        verification = payload.get("verification")
        if not isinstance(item, dict) or not isinstance(verification, dict):
            raise ValueError("backlog revalidation result is not typed")
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        status = str(verification.get("status", "FAILED"))
        repository = str(item["repository"])
        issue_number = int(item["issue_number"])
        with self.sessions.begin() as session:
            record = session.scalar(
                select(db.BacklogIssueRecord).where(
                    db.BacklogIssueRecord.backlog_campaign_id == campaign_id,
                    db.BacklogIssueRecord.repository == repository,
                    db.BacklogIssueRecord.issue_number == issue_number,
                )
            )
            if record is None:
                raise ValueError("backlog revalidation record disappeared")
            if status == "CONFIRMED":
                record.current_source_reproduction = (
                    "REPRODUCED_CURRENT_DEPLOYMENT"
                    if record.reproduction_mode == "DEPLOYMENT_REQUIRED"
                    else "REPRODUCED_CURRENT_SOURCE"
                )
                record.candidate_status = "REPRODUCED_CURRENT_SOURCE"
                record.eligibility_status = (
                    "ELIGIBLE_FOR_RUNTIME_REVALIDATION"
                    if record.current_runtime_required
                    else "READY"
                )
                record.eligibility_reason = (
                    "CURRENT_RUNTIME_REVALIDATION_REQUIRED"
                    if record.current_runtime_required
                    else "CURRENT_SOURCE_REVALIDATION_PASS"
                )
            elif status in {"REJECTED", "DUPLICATE"}:
                record.candidate_status = "NOT_REPRODUCIBLE"
                record.eligibility_status = "DEFERRED"
                record.eligibility_reason = (
                    "DUPLICATE" if status == "DUPLICATE" else "NOT_REPRODUCIBLE"
                )
            else:
                record.candidate_status = "REVALIDATION_BLOCKED"
                record.eligibility_status = "ELIGIBLE_FOR_REVALIDATION"
                record.eligibility_reason = f"REVALIDATION_{status}"
        return self._envelope(
            "backlog.record_revalidation",
            {**payload, "verification_status": status},
            "REVALIDATION_RECORDED",
        )

    _CTL = "/opt/engineering-control/venv/bin/engineering-ctl"

    def _notify_write_policy_required(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-84: tell the operator (Telegram, via the outbox) that a flow waits for a
        GitHub write approval, with the exact commands; the flow resumes by itself."""

        def bounded(key: str, pattern: str) -> str | None:
            value = str(payload.get(key) or "")
            return value if re.fullmatch(pattern, value) else None

        project = bounded("project", r"falcone|musematic|llmwiki")
        campaign = bounded("backlog_campaign_id", r"[0-9a-f-]{36}")
        workflow_id = bounded("workflow_id", r"[A-Za-z0-9._:-]{1,300}")
        step = bounded("step", r"[a-z_]+\.[a-z_]+")
        reason = bounded("reason_code", r"[A-Z0-9_]{1,120}") or "POLICY_DENIED"
        issue = bounded("issue", r"[A-Za-z0-9._/-]{1,120}#[0-9]{1,9}")
        if project and campaign:
            commands = [
                f"{self._CTL} backlog-approval-candidate {project} {campaign} "
                "full-delivery R2 --max-items 1",
                f"{self._CTL} backlog-approve {project} {campaign} <approval_binding from the "
                "previous output> APPROVE_EXISTING_GITHUB_BACKLOG_LOOP",
            ]
        else:
            commands = [f"{self._CTL} backlog-status <loop workflow id> to find the campaign"]
        message = (
            f"Hermes is waiting for a GitHub write approval ({reason})"
            + (f" for {issue}" if issue else "")
            + f" at {step or 'a write step'}. Approve the campaign; the flow re-checks every "
            "5 minutes and resumes by itself."
        )
        with self.sessions.begin() as session:
            session.add(
                db.NotificationOutbox(
                    topic="telegram.engineering-write-policy-required",
                    status="PENDING",
                    payload_json={
                        "message": message,
                        "project": project,
                        "backlog_campaign_id": campaign,
                        "workflow_id": workflow_id,
                        "step": step,
                        "reason_code": reason,
                        "commands": commands,
                    },
                    attempts=0,
                    next_attempt_at=datetime.now(UTC),
                    delivered_at=None,
                    last_error_code=None,
                )
            )
        return {"status": "NOTIFIED"}

    @staticmethod
    def _release_loop_child_leases(session: Any, loop_workflow_id: str) -> int:
        """Release every unreleased lease owned by one loop's child runs."""
        child_runs = set(
            session.scalars(
                select(db.Run.id).where(db.Run.workflow_id.like(f"{loop_workflow_id}-issue-%"))
            ).all()
        )
        if not child_runs:
            return 0
        now = datetime.now(UTC)
        released = 0
        for model in (db.ComponentLease, db.WorktreeLease):
            for lease in session.scalars(
                select(model).where(
                    model.owner_run_id.in_(child_runs),
                    model.released_at.is_(None),
                )
            ).all():
                lease.released_at = now
                released += 1
        return released

    def _record_backlog_loop_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        status = str(payload.get("loop_status", "PAUSED"))
        if status not in {"PAUSED", "CANCELLED", "COMPLETE", "RUNNING", "STOPPED", "FAILED"}:
            raise ValueError("backlog loop status is not allowlisted")
        completion = payload.get("completion")
        if completion is not None and (
            status != "COMPLETE" or completion != "COMPLETE_NOTHING_DELIVERED"
        ):
            raise ValueError("backlog loop completion is not allowlisted")
        run_workflow_id = payload.get("workflow_id")
        deactivation: dict[str, Any] | None = None
        # An activity retry after a committed attempt must not notify twice.
        already_notified = False
        with self.sessions.begin() as session:
            campaign = session.get(db.GitHubBacklogCampaign, campaign_id)
            if campaign is None:
                raise ValueError("backlog campaign disappeared")
            # NEW-127: the state before this record decides whether it is a transition.
            previous_status: str | None = campaign.status
            if isinstance(run_workflow_id, str) and run_workflow_id:
                # NEW-16: the top-level loop run must reach a terminal Run.status
                # here (whichever branch below returns) so OperationsRepository's
                # self-heal can ever recognize this operation as finished. Without
                # this, an operation record keyed on this workflow stays STARTED
                # forever and permanently blocks every future attempt.
                run = session.scalar(select(db.Run).where(db.Run.workflow_id == run_workflow_id))
                if run is not None:
                    previous_status = run.status
                    run.status = status
                    # NEW-33b: a paused loop is still alive; only terminal states finish it.
                    run.finished_at = (
                        datetime.now(UTC)
                        if status in {"CANCELLED", "COMPLETE", "STOPPED", "FAILED"}
                        else None
                    )
                    if completion is not None:
                        # NEW-114: COMPLETE stays the terminal status every consumer knows;
                        # the error code says that nothing was delivered.
                        already_notified = run.error_code == completion
                        run.error_code = str(completion)
                        run.summary = self._nothing_delivered_summary(payload)
                    elif status == "COMPLETE" and run.error_code == "COMPLETE_NOTHING_DELIVERED":
                        run.error_code = None
                if status in {"CANCELLED", "STOPPED", "FAILED"}:
                    # NEW-60: a cancelled loop's child runs never reach their own lease
                    # release; the next loop for the same issue then failed with
                    # COMPONENT_LEASE_CONFLICT / worktree lease conflicts (#993).
                    self._release_loop_child_leases(session, run_workflow_id)
            raw_global_id = payload.get("global_campaign_id")
            linked_global_ids = {
                candidate.id
                for candidate in session.scalars(
                    select(db.Campaign).where(
                        db.Campaign.project == "platform",
                        db.Campaign.status.in_({"RUNNING", "PAUSED"}),
                    )
                ).all()
                if isinstance(candidate.metadata_json, dict)
                and candidate.metadata_json.get("kind") == "autonomous-global-backlog"
                and isinstance(candidate.metadata_json.get("backlog_campaign_ids"), dict)
                and str(campaign_id)
                in {
                    str(value) for value in candidate.metadata_json["backlog_campaign_ids"].values()
                }
            }
            if raw_global_id is None and linked_global_ids:
                raise ValueError("global child backlog requires its persisted parent binding")
            if raw_global_id is not None:
                global_campaign = session.get(db.Campaign, UUID(str(raw_global_id)))
                metadata = global_campaign.metadata_json if global_campaign is not None else None
                inventory_ids = (
                    metadata.get("backlog_campaign_ids") if isinstance(metadata, dict) else None
                )
                if (
                    global_campaign is None
                    or global_campaign.project != "platform"
                    or not isinstance(metadata, dict)
                    or metadata.get("kind") != "autonomous-global-backlog"
                    or not isinstance(inventory_ids, dict)
                    or str(campaign_id) not in {str(value) for value in inventory_ids.values()}
                    or global_campaign.id not in linked_global_ids
                ):
                    raise ValueError("global child backlog binding is invalid")
                self._enqueue_loop_state(session, payload, status, previous_status, completion)
                return self._envelope(
                    "backlog.record_loop_state",
                    {**payload, "inventory_status_preserved": campaign.status},
                    status,
                )
            campaign.status = status
            if status in {"CANCELLED", "COMPLETE", "STOPPED"}:
                approval = campaign.approval_json.get("approval")
                actor = approval.get("approved_by") if isinstance(approval, dict) else None
                if not isinstance(actor, str) or not is_reauthenticated_backlog_operator(actor):
                    raise ValueError("terminal backlog campaign approval evidence is missing")
                deactivation = {
                    "action": "disable-existing-backlog",
                    "backlog_campaign_id": str(campaign.id),
                    "project": campaign.project,
                    "approval_binding": campaign.approval_binding,
                    "safe_repositories": [],
                    "requested_by": actor,
                    "requested_at": datetime.now(UTC).isoformat(),
                }
            if completion is not None and not already_notified:
                self._enqueue_nothing_delivered(session, payload)
            self._enqueue_loop_state(session, payload, status, previous_status, completion)
        if deactivation is not None:
            self._write_policy_request(deactivation)
        return self._envelope("backlog.record_loop_state", payload, status)

    @staticmethod
    def _enqueue_loop_state(
        session: Any,
        payload: dict[str, Any],
        status: str,
        previous_status: str | None,
        completion: Any,
    ) -> None:
        """NEW-127: tell the operator about every loop-state transition (once)."""
        if completion is not None:
            return  # NEW-114's nothing-delivered notification already reports it.
        enqueue_loop_state(session, payload, status=status, previous=previous_status)

    @staticmethod
    def _nothing_delivered_summary(payload: dict[str, Any]) -> str:
        raw = payload.get("outcomes")
        outcomes = [str(value)[:160] for value in raw[:20]] if isinstance(raw, list) else []
        refill_blocker = payload.get("refill_blocker")
        return (
            "Backlog loop completed with nothing delivered"
            + (": " + "; ".join(outcomes) if outcomes else " (no eligible issue)")
            + (f"; next issue not selected: {str(refill_blocker)[:120]}" if refill_blocker else "")
        )[:2000]

    def _enqueue_nothing_delivered(self, session: Any, payload: dict[str, Any]) -> None:
        """NEW-114: tell the operator a loop finished without delivering anything."""
        workflow_id = str(payload.get("workflow_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,300}", workflow_id):
            workflow_id = ""
        project = str(payload.get("project") or "")
        if project not in {"falcone", "musematic", "llmwiki"}:
            project = ""
        summary = self._nothing_delivered_summary(payload)
        session.add(
            db.NotificationOutbox(
                topic="telegram.engineering-backlog-nothing-delivered",
                status="PENDING",
                payload_json={
                    "message": (
                        f"Hermes backlog loop {workflow_id or '(unknown)'}"
                        + (f" for {project}" if project else "")
                        + f" finished with nothing delivered. {summary}"
                    )[:3000],
                    "project": project or None,
                    "backlog_campaign_id": str(payload.get("backlog_campaign_id") or ""),
                    "workflow_id": workflow_id or None,
                    "completion": "COMPLETE_NOTHING_DELIVERED",
                    "commands": [f"{self._CTL} backlog-status {workflow_id or '<workflow id>'}"],
                },
                attempts=0,
                next_attempt_at=datetime.now(UTC),
                delivered_at=None,
                last_error_code=None,
            )
        )

    def _register_backlog_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        workflow_id = str(payload.get("workflow_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,240}", workflow_id):
            raise ValueError("backlog run workflow identity is invalid")
        with self.sessions.begin() as session:
            campaign = session.get(db.GitHubBacklogCampaign, campaign_id)
            if campaign is None:
                raise ValueError("backlog campaign disappeared")
            existing = session.scalar(select(db.Run).where(db.Run.workflow_id == workflow_id))
            if existing is not None:
                return self._envelope(
                    "backlog.register_run",
                    {**payload, "run_id": str(existing.id)},
                    "REGISTERED",
                )
            # NEW-97: a loop whose start was blocked at selection registers its Run already
            # closed, so its operation ends and the issue can be started again.
            closed_blocker = payload.get("closed_blocker")
            now = datetime.now(UTC)
            # The loop registers its own Run, so it is bound to this execution at once
            # (2b678d9 run binding); an unbound row could never be enrolled later.
            run = new_bound_run(
                campaign_id=None,
                workflow_id=workflow_id,
                run_type="existing-backlog-loop",
                status="STOPPED" if closed_blocker else "RUNNING",
                project=campaign.project,
                started_at=now,
            )
            if closed_blocker:
                run.finished_at = now
                run.summary = f"Blocked before start: {str(closed_blocker)[:200]}"
            session.add(run)
            session.flush()
            return self._envelope(
                "backlog.register_run",
                {**payload, "run_id": str(run.id)},
                "REGISTERED",
            )

    def _record_backlog_ci_repair(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-112 review: durably record the CI-repair round the loop is about to start.

        Written by the loop's own verified execution, only after a recorded CI regression
        on a ChangeSet whose origin delivery this same loop registered; the delivery-child
        approval check accepts a CI-repair child only against this record.
        """
        step = "backlog.record_ci_repair"
        workflow_id = str(payload.get("workflow_id", ""))
        ordinal, repair_round = payload.get("ordinal"), payload.get("repair_round")
        if (
            not re.fullmatch(r"[A-Za-z0-9._:-]{1,240}", workflow_id)
            or type(ordinal) is not int
            or not 1 <= ordinal <= 500
            or type(repair_round) is not int
            or not 1 <= repair_round <= 999
        ):
            raise ValueError("CI-repair authorization request is invalid")
        if repair_round > MAX_CI_REPAIR_ROUNDS:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "CI_REPAIR_ROUND_LIMIT_REACHED",
                "last_step": "backlog.record_ci_repair",
            }
        change_set_id = UUID(str(payload["change_set_id"]))
        campaign_id = UUID(str(payload["backlog_campaign_id"]))

        def blocked(blocker: str) -> dict[str, Any]:
            return {**payload, "status": "BLOCKED", "blocker": blocker, "last_step": step}

        # Authority comes only from the verified activity identity (2b678d9 run binding:
        # workflow id plus first-execution run id), never from the payload: the caller
        # must be the parent loop execution itself.
        identity = _current_identity.get()
        if identity is None:
            return blocked("CI_REPAIR_CALLER_UNVERIFIED")
        if identity.workflow_id != workflow_id:
            return blocked("CI_REPAIR_CALLER_NOT_PARENT_LOOP")
        workflow_id = identity.workflow_id
        with self.sessions.begin() as session:
            loops = session.scalars(
                select(db.Run)
                .where(
                    db.Run.workflow_id == workflow_id, db.Run.run_type == "existing-backlog-loop"
                )
                .limit(2)
            ).all()
            if len(loops) != 1 or loops[0].temporal_first_execution_run_id is None:
                return blocked("CI_REPAIR_PARENT_RUN_UNVERIFIED")
            loop = loops[0]
            if loop.temporal_first_execution_run_id != identity.first_run_id:
                return blocked("CI_REPAIR_CALLER_NOT_PARENT_LOOP")
            change_set = session.get(db.ChangeSet, change_set_id, with_for_update=True)
            recovery = (change_set.recovery_json or {}) if change_set is not None else {}
            gitops = recovery.get("staging_gitops")
            regression = gitops.get("blocker") if isinstance(gitops, dict) else None
            if regression not in CI_REPAIR_REGRESSIONS:
                return blocked("CI_REPAIR_REGRESSION_NOT_RECORDED")
            reference = recovery.get("issue_execution_approval")
            origin = (
                session.get(db.Step, UUID(str(reference["approval_id"])))
                if isinstance(reference, dict) and reference.get("approval_id")
                else None
            )
            origin_value = origin.result_json if origin is not None else {}
            origin_request = origin_value.get("request") if isinstance(origin_value, dict) else None
            if (
                origin is None
                or origin_value.get("parent_workflow_id") != workflow_id
                or not isinstance(origin_request, dict)
                or origin_request.get("backlog_campaign_id") != str(campaign_id)
            ):
                return blocked("CI_REPAIR_PARENT_DID_NOT_PUBLISH_CHANGE_SET")
            value = {
                "version": 1,
                "parent_workflow_id": workflow_id,
                "parent_run_id": str(loop.id),
                "parent_first_execution_run_id": loop.temporal_first_execution_run_id,
                "backlog_campaign_id": str(campaign_id),
                "change_set_id": str(change_set_id),
                "origin_approval_id": str(origin.id),
                "ordinal": ordinal,
                "repair_round": repair_round,
                "regression": regression,
            }
            identifier = ci_repair_authorization_id(loop.id, change_set_id, ordinal, repair_round)
            existing = session.get(db.Step, identifier)
            if existing is None:
                # NEW-127: tell the operator a CI-repair round starts and which checks fail.
                failing = gitops.get("ci_regressions") if isinstance(gitops, dict) else None
                checks = (
                    [str(name)[:120] for name in failing[:8]] if isinstance(failing, list) else []
                )
                target = gitops.get("ci_regression_target") if isinstance(gitops, dict) else None
                enqueue_milestone(
                    session,
                    event="CI_REPAIR_STARTED",
                    dedupe_key=str(identifier),
                    project=change_set.project if change_set is not None else None,
                    issue=issue_for_finding(session, change_set.finding_id, campaign_id)
                    if change_set is not None
                    else None,
                    workflow_id=workflow_id,
                    detail=(
                        f"round {repair_round} for {regression}"
                        + (f" on the {target} PR" if target in {"source", "deployment"} else "")
                        + (f"; failing checks: {', '.join(checks)}" if checks else "")
                    ),
                    extra={"repair_round": repair_round, "failing_checks": checks},
                )
                session.add(
                    db.Step(
                        id=identifier,
                        run_id=loop.id,
                        name=CI_REPAIR_AUTHORIZATION_STEP,
                        ordinal=repair_round,
                        status="COMPLETE",
                        idempotency_key=f"ci-repair-authorization:{identifier}",
                        attempt=1,
                        result_json=value,
                    )
                )
            elif existing.run_id != loop.id or {
                key: existing.result_json.get(key) for key in value if key != "regression"
            } != {key: item for key, item in value.items() if key != "regression"}:
                raise ValueError("CI-repair authorization changed during idempotent retry")
        return self._envelope(
            step, {**payload, "repair_authorization": str(identifier)}, "RECORDED"
        )

    # NEW-102: states a CI-repair re-delivery leaves behind when it stops before publishing.
    _REPAIR_INTERMEDIATE_STATES = frozenset({"PLANNED", "IMPLEMENTING", "REQUEST_CHANGES"})

    def _restore_published_change_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """NEW-102: return a ChangeSet to its published PRs after a CI repair stopped
        without publishing (falcone #1001: left PLANNED, so the recheck was refused).

        Two phases around a read of the live PR heads (gitops.pr_heads, GitHub worker):
        "plan" names the bound PRs and the ChangeSet version, and "apply" restores SOURCE_PR
        only if the version is unchanged (no late repair write) and every bound PR is open
        at exactly the head recorded at publication. Heads return to those published heads,
        so unpublished commits are never delivered. Any mismatch is NOT_RESTORED.
        """
        step = "backlog.restore_published_change_set"
        phase = str(payload.get("phase") or "")
        if phase not in {"plan", "apply"}:
            raise ValueError("restore phase is invalid")
        change_set_id = UUID(str(payload["change_set_id"]))

        def refused(reason: str) -> dict[str, Any]:
            return {**self._envelope(step, payload, "NOT_RESTORED"), "reason": reason[:80]}

        with self.sessions.begin() as session:
            change_set = session.get(db.ChangeSet, change_set_id, with_for_update=True)
            if change_set is None:
                raise ValueError("CI-repair ChangeSet is missing")
            recovery = dict(change_set.recovery_json or {})
            evidence = recovery.get("change_evidence")
            source = evidence.get("source") if isinstance(evidence, dict) else None
            deployment = evidence.get("deployment") if isinstance(evidence, dict) else None
            published = {
                str(change_set.source_pr_url or ""): (
                    source.get("head_sha") if isinstance(source, dict) else None
                )
            }
            if change_set.deployment_pr_url:
                published[str(change_set.deployment_pr_url)] = (
                    deployment.get("head_sha") if isinstance(deployment, dict) else None
                )
            if not change_set.source_pr_url or not all(map(_is_sha, published.values())):
                return refused("PUBLICATION_BINDING_MISSING")
            # An already-published binding (the repair stopped before resetting the state,
            # or an earlier apply committed but its result was lost) is restored as a no-op.
            already = (
                change_set.state in {"SOURCE_PR", "SOURCE_MERGED"}
                and change_set.source_head_sha == published[str(change_set.source_pr_url)]
                and (
                    not change_set.deployment_pr_url
                    or change_set.deployment_head_sha
                    == published[str(change_set.deployment_pr_url)]
                )
            )
            if change_set.state not in self._REPAIR_INTERMEDIATE_STATES and not already:
                return refused(f"STATE_{change_set.state}")
            if phase == "plan":
                return {
                    **self._envelope(step, payload, "READY"),
                    "version": change_set.version,
                    "pull_requests": [{"url": url} for url in published],
                }
            if payload.get("version") != change_set.version:
                return refused("CHANGE_SET_CHANGED_SINCE_PLAN")
            live = payload.get("live_heads")
            by_url = (
                {str(item.get("url")): item for item in live if isinstance(item, dict)}
                if isinstance(live, list)
                else {}
            )
            source_merged = False
            for url, head in published.items():
                observed = by_url.get(url)
                state = observed.get("state") if observed is not None else None
                # NEW-103: the source PR may already be merged while its deployment PR is
                # repaired; a merged source at its published head is still that binding.
                if url == change_set.source_pr_url and state == "MERGED":
                    source_merged = True
                elif state != "OPEN":
                    return refused("PUBLISHED_PR_NOT_OPEN")
                if observed is None or observed.get("head_sha") != head:
                    return refused("PUBLISHED_PR_HEAD_CHANGED")
            if already:
                return {**self._envelope(step, payload, "RESTORED"), "already_published": True}
            recovery["ci_repair_restore"] = {
                "from_state": change_set.state,
                "from_source_head_sha": change_set.source_head_sha,
                "from_deployment_head_sha": change_set.deployment_head_sha,
                "restored_at": datetime.now(UTC).isoformat(),
                "workflow_id": str(payload.get("workflow_id") or "")[:240],
            }
            change_set.recovery_json = recovery
            change_set.state = "SOURCE_MERGED" if source_merged else "SOURCE_PR"
            change_set.source_head_sha = str(published[str(change_set.source_pr_url)])
            if change_set.deployment_pr_url:
                change_set.deployment_head_sha = str(published[str(change_set.deployment_pr_url)])
            change_set.version += 1
        return self._envelope(step, payload, "RESTORED")

    def _register_backlog_child_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        child_kind = str(payload.get("child_kind", ""))
        child_workflow_id = str(payload.get("child_workflow_id", ""))
        allowed = {
            "finding-verification",
            "issue-delivery",
            "delivery-reconciliation",
            "staging-deployment",
            "runtime-verification",
            "staging-gitops",
        }
        if child_kind not in allowed:
            raise ValueError("backlog child run kind is not allowlisted")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,240}", child_workflow_id):
            raise ValueError("backlog child workflow identity is invalid")
        finding_id = UUID(str(payload.get("finding_id", "")))
        change_set_id = (
            UUID(str(payload["change_set_id"]))
            if child_kind not in {"finding-verification", "issue-delivery"}
            else None
        )
        with self.sessions.begin() as session:
            finding = session.get(db.Finding, finding_id)
            if finding is None:
                raise ValueError("backlog child finding is missing")
            if child_kind == "issue-delivery" and finding.disposition != "CONFIRMED":
                raise ValueError("backlog delivery child requires a confirmed finding")
            change_set = session.get(db.ChangeSet, change_set_id) if change_set_id else None
            if change_set_id is not None and (
                change_set is None
                or change_set.finding_id != finding.id
                or change_set.project != finding.project
            ):
                raise ValueError("backlog delivery-stage ChangeSet binding is invalid")
            existing = session.scalar(select(db.Run).where(db.Run.workflow_id == child_workflow_id))
            if existing is not None:
                existing_change_set = (
                    session.get(db.ChangeSet, existing.change_set_id)
                    if existing.change_set_id is not None
                    else None
                )
                change_set_matches = existing.change_set_id == change_set_id or (
                    child_kind == "issue-delivery"
                    and existing_change_set is not None
                    and existing_change_set.finding_id == finding.id
                    and existing_change_set.project == finding.project
                )
                if (
                    existing.run_type != child_kind
                    or existing.project != finding.project
                    or not change_set_matches
                    or (
                        child_kind == "finding-verification" and existing.summary != str(finding.id)
                    )
                ):
                    raise ValueError("backlog child run idempotency binding changed")
                approval = (
                    register_child_issue_approval(session, payload, existing, finding)
                    if child_kind == "issue-delivery"
                    else None
                )
                return self._envelope(
                    "backlog.register_child_run",
                    {
                        **payload,
                        "run_id": str(existing.id),
                        **({"issue_approval_id": str(approval.id)} if approval else {}),
                    },
                    "REGISTERED",
                )
            run = new_bound_run(
                campaign_id=(finding.campaign_id if child_kind == "finding-verification" else None),
                change_set_id=change_set_id,
                workflow_id=child_workflow_id,
                run_type=child_kind,
                status="QUEUED",
                project=finding.project,
                summary=(str(finding.id) if child_kind == "finding-verification" else None),
            )
            session.add(run)
            session.flush()
            approval = (
                register_child_issue_approval(session, payload, run, finding)
                if child_kind == "issue-delivery"
                else None
            )
            if child_kind == "issue-delivery":
                # NEW-127: one "delivery started (attempt N)" per delivery child.
                enqueue_milestone(
                    session,
                    event="DELIVERY_STARTED",
                    dedupe_key=child_workflow_id,
                    project=finding.project,
                    issue=issue_for_finding(
                        session, finding.id, payload.get("backlog_campaign_id")
                    ),
                    workflow_id=child_workflow_id,
                    detail=attempt_label(child_workflow_id),
                )
            return self._envelope(
                "backlog.register_child_run",
                {
                    **payload,
                    "run_id": str(run.id),
                    **({"issue_approval_id": str(approval.id)} if approval else {}),
                },
                "REGISTERED",
            )

    def _write_policy_request(self, request: dict[str, Any]) -> None:
        root = self.settings.state_root / "github-policy-requests"
        metadata = root.stat()
        if not root.is_dir() or metadata.st_mode & 0o022:
            raise ValueError("GitHub policy request directory is unavailable or insecure")
        target = root / "activate.json"
        temporary = root / f".loop-state-{os.getpid()}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            # NEW-94 review: never overwrite a request the root reconciler has not consumed
            # yet (a late disable clobbered a pending enable, or deleted another campaign's
            # disable). link() fails if one is pending; the activity retries once consumed.
            try:
                os.link(temporary, target)
            except FileExistsError as error:
                raise RuntimeError("GITHUB_POLICY_REQUEST_PENDING") from error
        finally:
            temporary.unlink(missing_ok=True)

    def _backlog_issue_for_update(self, payload: dict[str, Any]) -> db.BacklogIssueRecord:
        campaign_id = UUID(str(payload["backlog_campaign_id"]))
        repository = str(payload["repository"])
        number = int(payload["issue_number"])
        with self.sessions() as session:
            row = session.scalar(
                select(db.BacklogIssueRecord).where(
                    db.BacklogIssueRecord.backlog_campaign_id == campaign_id,
                    db.BacklogIssueRecord.repository == repository,
                    db.BacklogIssueRecord.issue_number == number,
                )
            )
            if row is None:
                raise ValueError("backlog issue record does not exist")
            session.expunge(row)
            return row

    @staticmethod
    def _serialize_backlog_campaign(campaign: db.GitHubBacklogCampaign) -> dict[str, Any]:
        return {
            "backlog_campaign_id": str(campaign.id),
            "project": campaign.project,
            "source_repository": campaign.source_repository,
            "deployment_repository": campaign.deployment_repository,
            "imported_at": campaign.imported_at.isoformat(),
            "import_filter": campaign.import_filter,
            "source_head_at_import": campaign.source_head_at_import,
            "deployment_head_at_import": campaign.deployment_head_at_import,
            "issue_count": campaign.issue_count,
            "eligible_count": campaign.eligible_count,
            "deferred_count": campaign.deferred_count,
            "duplicate_count": campaign.duplicate_count,
            "stale_count": campaign.stale_count,
            "already_fixed_count": campaign.already_fixed_count,
            "existing_pr_count": campaign.existing_pr_count,
            "blocked_count": campaign.blocked_count,
            "dependency_graph_hash": campaign.dependency_graph_hash,
            "plan_hash": campaign.plan_hash,
            "approval_binding": campaign.approval_binding,
            "maximum_risk": campaign.maximum_risk,
            "maximum_items": campaign.maximum_items,
            "maximum_concurrency": campaign.maximum_concurrency,
            "delivery_mode": campaign.delivery_mode,
            "include_enhancements": campaign.include_enhancements,
            "status": campaign.status,
            "workflow_id": campaign.workflow_id,
        }

    @staticmethod
    def _serialize_backlog_issue(issue: db.BacklogIssueRecord) -> dict[str, Any]:
        return {
            "repository": issue.repository,
            "repository_short_name": issue.repository.rsplit("/", 1)[-1],
            "issue_number": issue.issue_number,
            "issue_url_reference": issue.issue_url_reference,
            "title": issue.title,
            "state": issue.state,
            "labels": issue.labels_json,
            "milestone": issue.milestone,
            "issue_type": issue.issue_type,
            "openspec_kind": issue.openspec_kind,
            "severity": issue.severity,
            "calculated_risk": issue.calculated_risk,
            "component": issue.component,
            "source_or_deployment_owner": issue.source_or_deployment_owner,
            "documentation_levels": issue.documentation_levels_json,
            "dependencies": issue.dependencies_json,
            "blocking_issues": issue.blocking_issues_json,
            "issues_blocked": issue.issues_blocked_json,
            "linked_prs": issue.linked_prs_json,
            "linked_branches": issue.linked_branches_json,
            "linked_commits": issue.linked_commits_json,
            "duplicate_candidates": issue.duplicate_candidates_json,
            "current_source_reproduction": issue.current_source_reproduction,
            "current_runtime_reproduction": issue.current_runtime_reproduction,
            "current_runtime_required": issue.current_runtime_required,
            "reproduction_mode": issue.reproduction_mode,
            "candidate_status": issue.candidate_status,
            "eligibility_status": issue.eligibility_status,
            "eligibility_reason": issue.eligibility_reason,
            "planned_wave": issue.planned_wave,
            "planned_order": issue.planned_order,
            "finding_id": str(issue.finding_id) if issue.finding_id else None,
            "change_set_id": str(issue.change_set_id) if issue.change_set_id else None,
            "delivery_status": issue.delivery_status,
            "final_closure_status": issue.final_closure_status,
            "documentation_impact": issue.documentation_impact,
            "github_updated_at": issue.github_updated_at.isoformat(),
            "body_sha256": issue.body_sha256,
        }

    def _backlog_campaign_envelope(
        self, campaign: db.GitHubBacklogCampaign, status: str
    ) -> dict[str, Any]:
        return self._envelope(
            "backlog.persist_import",
            {"campaign": self._serialize_backlog_campaign(campaign)},
            status,
        )

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


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None
