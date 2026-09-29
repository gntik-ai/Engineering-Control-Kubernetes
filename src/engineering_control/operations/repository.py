from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml
from sqlalchemy import and_, create_engine, desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.observability import DashboardObservabilityProjection
from engineering_control.db.models import (
    ActionCandidate,
    Artifact,
    BacklogIssueRecord,
    Campaign,
    ConnectionSnapshot,
    CoverageCell,
    DashboardEvent,
    Enhancement,
    Finding,
    GitHubBacklogCampaign,
    NotificationOutbox,
    OperationExport,
    OperationRequest,
    ProjectSnapshot,
    QuotaSnapshot,
    ReauthenticationBinding,
    Run,
    SolutionLoop,
    WorkerHeartbeat,
)
from engineering_control.engineering.backlog import (
    BACKLOG_APPROVAL_PHRASE,
    BacklogApprovalBinding,
    canonical_sha256,
    is_reauthenticated_backlog_operator,
    r3_approval_payload_matches,
)
from engineering_control.engineering.models import ProjectManifest
from engineering_control.models.reconciliation import DeliveryControlPolicy
from engineering_control.operations.models import (
    AdvancedAuditCommand,
    AuditAllProjectsCommand,
    AuditPlanCommand,
    AuditStartCommand,
    AutonomousBacklogCommand,
    CandidateView,
    CapabilityResult,
    Command,
    ConnectionView,
    EnhancementApproveCommand,
    EnhancementDiscoverCommand,
    EnhancementRejectCommand,
    EnhancementRunCommand,
    EvidenceExportCampaignCommand,
    EvidenceExportWorkflowCommand,
    FindingVerifyCommand,
    LLMWikiBootstrapPlanCommand,
    OperationView,
    RefreshConnectionsCommand,
    SolveBacklogCommand,
    SolveIssueCommand,
    SourceChannel,
    WorkflowControlCommand,
    parse_command,
)
from engineering_control.security.redaction import SecretRedactor
from engineering_control.workflows.schedule_intervals import QUOTA_REFRESH_INTERVAL

_ACTIVE_OPERATION_STATES = frozenset({"PENDING", "DISPATCHING", "STARTED", "RUNNING"})
# NEW-16, 2026-09-22: this must stay comfortably above QUOTA_REFRESH_INTERVAL, not an
# independent literal -- a hardcoded 300s (5 min) here against a schedule that only
# refreshes every 30 min made solve_backlog/solve_issue's preflight fail ~83% of the
# time. The buffer absorbs schedule jitter and the refresh workflow's own run time.
_PROVIDER_HEALTH_STALE_AFTER = QUOTA_REFRESH_INTERVAL + timedelta(minutes=10)
_CAMPAIGN_EXPORT_COLLECTIONS = ("root_blockers", "findings", "runs", "artifacts")
_TERMINAL_WORKFLOW_STATES = frozenset(
    {
        "COMPLETE",
        "COMPLETED",
        "SUCCESS",
        "FAILED",
        "CANCELLED",
        "SKIPPED",
        "STOPPED",
        "STOPPED_AFTER_CURRENT",
        "TIMED_OUT",
    }
)


def _global_inventory_approval_current(
    campaign: GitHubBacklogCampaign,
    *,
    project: str,
    now: datetime,
) -> bool:
    delivery_mode = "source-pr" if project == "llmwiki" else "full-delivery"
    if (
        campaign.project != project
        or campaign.status
        not in {"APPROVED_POLICY_PENDING", "APPROVED_INVENTORY", "APPROVED", "RUNNING"}
        or campaign.delivery_mode != delivery_mode
        or campaign.maximum_risk != "R2"
        or not campaign.approval_binding
        or not isinstance(campaign.approval_json, dict)
    ):
        return False
    approval = campaign.approval_json
    raw_binding = approval.get("binding")
    binding_value = raw_binding if isinstance(raw_binding, dict) else approval
    raw_approval = approval.get("approval")
    approval_record = raw_approval if isinstance(raw_approval, dict) else approval
    if (
        approval_record.get("typed_confirmation") != BACKLOG_APPROVAL_PHRASE
        or not isinstance(approval_record.get("approved_by"), str)
        or not approval_record.get("approved_at")
    ):
        return False
    try:
        binding = BacklogApprovalBinding.model_validate(
            {field: binding_value.get(field) for field in BacklogApprovalBinding.model_fields}
        )
    except ValueError:
        return False
    expires_at = (
        binding.expires_at
        if binding.expires_at.tzinfo is not None
        else binding.expires_at.replace(tzinfo=UTC)
    )
    return (
        binding.sha256() == campaign.approval_binding
        and binding.backlog_campaign_id == str(campaign.id)
        and binding.project == project
        and binding.source_head == campaign.source_head_at_import
        and binding.deployment_head == campaign.deployment_head_at_import
        and binding.dependency_plan_hash == campaign.plan_hash
        and binding.delivery_mode == delivery_mode
        and binding.maximum_risk == "R2"
        and expires_at > now
    )


def command_target(command: Command) -> tuple[str, str, str | None]:
    if isinstance(command, SolveIssueCommand):
        return "github_issue", f"{command.repository}#{command.issue_number}", command.project.value
    if isinstance(command, SolveBacklogCommand):
        return "backlog_campaign", str(command.backlog_campaign_id), command.project
    if isinstance(command, AutonomousBacklogCommand):
        return "global_backlog", str(command.campaign_id), "platform"
    if isinstance(command, (AuditPlanCommand, AuditStartCommand, AdvancedAuditCommand)):
        if isinstance(command, AdvancedAuditCommand):
            return (
                "audit",
                f"{command.project.value}:{command.name}:{command.source_sha}:{command.deployment_sha}",
                command.project.value,
            )
        return (
            "audit",
            f"{command.project.value}:{command.preset}:{command.environment}",
            command.project.value,
        )
    if isinstance(command, AuditAllProjectsCommand):
        return (
            "audit_group",
            f"all:{command.preset}:{command.environment}:{command.mode}",
            "all",
        )
    if isinstance(command, WorkflowControlCommand):
        return "workflow", command.workflow_id, None
    if isinstance(command, RefreshConnectionsCommand):
        return "connections", command.scope, None
    if isinstance(command, EnhancementDiscoverCommand):
        return "enhancement_campaign", str(command.campaign_id), command.project.value
    if isinstance(
        command,
        (EnhancementApproveCommand, EnhancementRejectCommand, EnhancementRunCommand),
    ):
        return "enhancement", command.enhancement_id, command.project.value
    if isinstance(command, FindingVerifyCommand):
        return "finding", str(command.finding_id), None
    if isinstance(command, LLMWikiBootstrapPlanCommand):
        return "llmwiki", "staging-bootstrap-plan", "llmwiki"
    if isinstance(command, EvidenceExportCampaignCommand):
        return "audit_campaign", str(command.campaign_id), None
    if isinstance(command, EvidenceExportWorkflowCommand):
        return "workflow", command.workflow_id, None
    raise ValueError("command target is not registered")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _r3_delivery_briefing(manifest: ProjectManifest, surface: str) -> dict[str, Any]:
    """Build the project-specific fields of an R3 approval briefing from its manifest.

    Grounded in each project's real deployment strategy rather than one hardcoded
    (originally musematic-only) template: argocd's verification/rollback steps match
    musematic's actual ArgoCD sync semantics (proven -- this route has real approval
    history under exactly this content); helm's match falcone's actual
    FalconeReleaseAdapter.verify()/rollback() checks (deployed-status + immutable-digest
    presence; helm rollback with an exact-manifest-restoration check); repo-local-manifest
    (llmwiki-staging) has no production counterpart and no real R3 approval history yet,
    so its checklist is deliberately generic and should get a first real dry run before
    being trusted the way musematic's has been.
    """
    repository = (
        manifest.repositories.source if surface == "source" else manifest.repositories.deployment
    ).full_name
    workflow = next(
        (item for item in manifest.deployment.build_workflows if item.repository == repository),
        manifest.deployment.build_workflows[0],
    )
    build_workflow = f"{workflow.repository}/.github/workflows/{workflow.workflow}"
    build_contract_state = (
        "READY" if workflow.enabled else (workflow.blocker or "CANDIDATE_BUILD_CONTRACT_MISSING")
    )
    strategy = manifest.deployment.strategy
    if strategy == "argocd":
        return {
            "build_workflow": build_workflow,
            "build_contract_state": build_contract_state,
            "delivery_adapter": f"argocd:{manifest.deployment.argocd_application}",
            "verification": [
                "Argo-Synced-and-Healthy",
                "identity-endpoint-200",
                "negative-tenant-controls",
                "final-provenance",
            ],
            "rollback": "revert-protected-GitOps-change-and-reconcile",
        }
    if strategy == "helm":
        return {
            "build_workflow": build_workflow,
            "build_contract_state": build_contract_state,
            "delivery_adapter": f"helm:{manifest.deployment.helm_release}",
            "verification": [
                "helm-release-deployed-with-approved-image-digests",
                "identity-endpoint-200",
                "negative-tenant-controls",
                "final-provenance",
            ],
            "rollback": "helm-rollback-to-prior-revision-and-verify-exact-manifest-restored",
        }
    return {
        "build_workflow": build_workflow,
        "build_contract_state": build_contract_state,
        "delivery_adapter": f"repo-local-manifest:{manifest.deployment.adapter}",
        "verification": [
            "staging-manifest-applied",
            "identity-endpoint-200",
            "negative-tenant-controls",
        ],
        "rollback": "revert-repo-local-manifest-and-reapply-staging-bootstrap",
    }


class OperationsRepository:
    """Durable operation store. It never reads service or provider credentials."""

    def __init__(
        self,
        database_url: str,
        *,
        export_retention_days: int = 7,
        export_max_bytes: int = 1024 * 1024,
        project_config_root: Path | None = None,
    ) -> None:
        if not 1 <= export_retention_days <= 30:
            raise ValueError("operation export retention is outside its safety bound")
        if not 65_536 <= export_max_bytes <= 5 * 1024 * 1024:
            raise ValueError("operation export size is outside its safety bound")
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.redactor = SecretRedactor()
        self.observability = DashboardObservabilityProjection(self.sessions, self.redactor)
        self.export_retention_days = export_retention_days
        self.export_max_bytes = export_max_bytes
        self.project_config_root = (
            project_config_root.absolute() if project_config_root is not None else None
        )

    async def candidate(self, command: Command, session_binding_hash: str) -> CandidateView:
        return await asyncio.to_thread(self._candidate, command, session_binding_hash)

    async def r3_issue_candidate(
        self, project: str, surface: str, issue_number: int
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._r3_issue_candidate,
            project,
            surface,
            issue_number,
            True,
        )

    async def r3_issue_candidate_status(
        self, project: str, surface: str, issue_number: int
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._r3_issue_candidate,
            project,
            surface,
            issue_number,
            False,
        )

    def _r3_issue_candidate(
        self,
        project: str,
        surface: str,
        issue_number: int,
        create: bool = True,
    ) -> dict[str, Any]:
        if project not in {"falcone", "musematic", "llmwiki"}:
            raise ValueError("R3 approval project is not allowlisted")
        if surface not in {"source", "deployment"}:
            raise ValueError("R3 approval surface is not allowlisted")
        if issue_number < 1:
            raise ValueError("R3 approval issue number is invalid")
        try:
            manifest = self._load_project_manifest(project)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
            raise ValueError("R3 approval project contract is unconfigured or invalid") from error
        repositories = manifest.repositories
        expected_repository = (
            repositories.source if surface == "source" else repositories.deployment
        ).full_name
        briefing = _r3_delivery_briefing(manifest, surface)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            issue = session.scalar(
                select(BacklogIssueRecord)
                .join(
                    GitHubBacklogCampaign,
                    GitHubBacklogCampaign.id == BacklogIssueRecord.backlog_campaign_id,
                )
                .where(
                    GitHubBacklogCampaign.project == project,
                    BacklogIssueRecord.source_or_deployment_owner == surface,
                    BacklogIssueRecord.issue_number == issue_number,
                    BacklogIssueRecord.state == "OPEN",
                )
                .order_by(desc(GitHubBacklogCampaign.imported_at))
                .limit(1)
            )
            if issue is None or issue.calculated_risk != "R3":
                raise ValueError("current backlog issue is not an approval-bound R3 issue")
            if issue.repository != expected_repository:
                raise ValueError("R3 approval issue repository binding changed")
            campaign = session.get(GitHubBacklogCampaign, issue.backlog_campaign_id)
            if campaign is None:
                raise ValueError("R3 backlog campaign disappeared")
            target_id = f"{issue.repository}#{issue_number}"
            immutable_payload = {
                "project": project,
                "repository": issue.repository,
                "issue_number": issue.issue_number,
                "issue_updated_at": issue.github_updated_at.isoformat(),
                "issue_body_sha256": issue.body_sha256,
                "source_repository": campaign.source_repository,
                "deployment_repository": campaign.deployment_repository,
                "source_branch": campaign.source_default_branch,
                "deployment_branch": campaign.deployment_default_branch,
                "source_head": campaign.source_head_at_import,
                "deployment_head": campaign.deployment_head_at_import,
                "execution_mode": "W0_BASELINE_REPAIR",
                "possible_migration_or_backfill_scope": "issue-specific-additive-only",
                "tenant_safety": [
                    "canonical-principal-invariant-required",
                    "idempotent-repair-required",
                    "cross-tenant-repair-prohibited-without-proven-ownership",
                ],
                "github_operations": [
                    "issue-comment",
                    "bounded-branch-push",
                    "pr-create-or-adopt",
                    "ci-inspect",
                    "protected-merge-after-gates",
                ],
                "environment": "staging",
                "implementation_correction_limit": 5,
                "deployment_correction_limit": 3,
                "production": False,
                **briefing,
            }
            existing_rows = session.scalars(
                select(ActionCandidate)
                .where(
                    ActionCandidate.action == "r3_issue_approval",
                    ActionCandidate.target_id == target_id,
                    ActionCandidate.expires_at > now,
                )
                .order_by(desc(ActionCandidate.created_at))
            ).all()
            for existing in existing_rows:
                stored = existing.payload_json
                if all(stored.get(key) == value for key, value in immutable_payload.items()):
                    return {
                        "status": (
                            "AWAITING_R3_APPROVAL" if existing.used_at is None else "R3_APPROVED"
                        ),
                        "candidate_id": str(existing.id),
                        "binding": existing.binding_hash,
                        "confirmation_phrase": existing.confirmation_phrase,
                        "candidate": stored,
                        "telegram_command": (
                            f"/loop approve-r3 {project} {surface} {issue_number} "
                            f"{existing.binding_hash} {existing.confirmation_phrase}"
                        ),
                    }
            if not create:
                raise ValueError("R3 approval candidate has not been created")
            expires_at = now + timedelta(hours=24)
            payload = {**immutable_payload, "expires_at": expires_at.isoformat()}
            binding = canonical_sha256(payload)
            phrase = f"APPROVE_R3_{project.upper()}_{issue_number}_{binding[:16].upper()}"
            bound_candidate = session.scalar(
                select(ActionCandidate).where(
                    ActionCandidate.action == "r3_issue_approval",
                    ActionCandidate.binding_hash == binding,
                )
            )
            if bound_candidate is None:
                bound_candidate = ActionCandidate(
                    action="r3_issue_approval",
                    project=project,
                    target_type="github_issue",
                    target_id=target_id,
                    payload_json=payload,
                    capability_status="AVAILABLE",
                    blockers_json=[],
                    binding_hash=binding,
                    confirmation_phrase=phrase,
                    session_binding_hash=canonical_sha256({"r3": "cross-channel"}),
                    created_at=now,
                    expires_at=expires_at,
                    used_at=None,
                )
                session.add(bound_candidate)
                session.flush()
            return {
                "status": (
                    "AWAITING_R3_APPROVAL" if bound_candidate.used_at is None else "R3_APPROVED"
                ),
                "candidate_id": str(bound_candidate.id),
                "binding": binding,
                "confirmation_phrase": phrase,
                "candidate": payload,
                "telegram_command": (
                    f"/loop approve-r3 {project} {surface} {issue_number} {binding} {phrase}"
                ),
            }

    async def approve_r3_issue(
        self,
        project: str,
        surface: str,
        issue_number: int,
        binding: str,
        phrase: str,
        actor: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._approve_r3_issue,
            project,
            surface,
            issue_number,
            binding,
            phrase,
            actor,
        )

    def _approve_r3_issue(
        self,
        project: str,
        surface: str,
        issue_number: int,
        binding: str,
        phrase: str,
        actor: str,
    ) -> dict[str, Any]:
        if not is_reauthenticated_backlog_operator(actor):
            raise ValueError("R3 approval requires an allowlisted operator channel")
        candidate = self._r3_issue_candidate(project, surface, issue_number, False)
        if candidate["binding"] != binding or candidate["confirmation_phrase"] != phrase:
            raise ValueError("R3 approval binding or phrase changed")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(
                ActionCandidate,
                UUID(str(candidate["candidate_id"])),
                with_for_update=True,
            )
            if row is None or _utc(row.expires_at) <= now:
                raise ValueError("R3 approval candidate expired")
            if row.binding_hash != binding or row.confirmation_phrase != phrase:
                raise ValueError("R3 approval candidate changed before persistence")
            if row.used_at is None:
                row.used_at = now
                row.payload_json = {
                    **row.payload_json,
                    "approved_by": actor,
                    "approved_at": now.isoformat(),
                }
                session.add(
                    DashboardEvent(
                        occurred_at=now,
                        level="WARNING",
                        event_type="r3_issue_approved",
                        source="controller",
                        host=None,
                        worker_kind="controller",
                        project=project,
                        workflow_id=None,
                        temporal_run_id=None,
                        issue_number=issue_number,
                        campaign_id=None,
                        entity_type="action_candidate",
                        entity_id=str(row.id),
                        current_phase="R3_APPROVED",
                        message="Immutable R3 issue approval accepted",
                        metadata_json={"approved_by": actor, "binding": binding},
                    )
                )
            approved_by = row.payload_json.get("approved_by")
        return {
            "status": "R3_APPROVED",
            "binding": binding,
            "target": f"{candidate['candidate']['repository']}#{issue_number}",
            "approved_by": approved_by,
            "production": False,
        }

    def _candidate(self, command: Command, session_binding_hash: str) -> CandidateView:
        if len(session_binding_hash) != 64:
            raise ValueError("session binding is invalid")
        target_type, target_id, project = command_target(command)
        now = datetime.now(UTC)
        payload = command.model_dump(mode="json")
        with self.sessions.begin() as session:
            capability, binding = self._capability(session, command, now)
            project = project or capability.project
            if isinstance(
                command,
                (EnhancementApproveCommand, EnhancementRejectCommand, EnhancementRunCommand),
            ) and isinstance(binding.get("approval_binding_sha256"), str):
                payload["approval_binding"] = binding["approval_binding_sha256"]
            immutable = {
                "action": command.action,
                "target_type": target_type,
                "target_id": target_id,
                "project": project,
                "payload": payload,
                "resolved": binding,
                "production": False,
            }
            if isinstance(command, RefreshConnectionsCommand):
                immutable["candidate_nonce"] = secrets.token_hex(16)
            binding_hash = canonical_sha256(immutable)
            phrase = self._confirmation_phrase(command, binding_hash)
            existing = session.scalar(
                select(ActionCandidate).where(
                    ActionCandidate.binding_hash == binding_hash,
                    ActionCandidate.session_binding_hash == session_binding_hash,
                    ActionCandidate.used_at.is_(None),
                    ActionCandidate.expires_at > now,
                )
            )
            if existing is None:
                existing = ActionCandidate(
                    action=command.action,
                    project=project,
                    target_type=target_type,
                    target_id=target_id,
                    payload_json=payload,
                    capability_status=capability.status,
                    blockers_json=capability.blockers,
                    binding_hash=binding_hash,
                    confirmation_phrase=phrase,
                    session_binding_hash=session_binding_hash,
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                    used_at=None,
                )
                session.add(existing)
                session.flush()
            return CandidateView(
                candidate_id=existing.id,
                action=existing.action,
                target_type=existing.target_type,
                target_id=existing.target_id,
                project=existing.project,
                binding_hash=existing.binding_hash,
                confirmation_phrase=existing.confirmation_phrase,
                capability=capability,
                operation_binding=immutable,
                expires_at=existing.expires_at,
            )

    def _capability(
        self,
        session: Session,
        command: Command,
        now: datetime,
        *,
        exclude_operation_id: UUID | None = None,
    ) -> tuple[CapabilityResult, dict[str, Any]]:
        blockers: list[str] = []
        required_credentials: list[str] = []
        binding: dict[str, Any] = {}
        project_name: str | None = None
        mode: str | None = None
        if isinstance(command, SolveBacklogCommand):
            project_name = command.project
            mode = command.delivery_mode
        elif isinstance(command, AutonomousBacklogCommand):
            project_name = "platform"
            mode = "global-staging-r2"
        elif isinstance(
            command,
            (SolveIssueCommand, AuditPlanCommand, AuditStartCommand, AdvancedAuditCommand),
        ):
            project_name = command.project.value
            mode = (
                command.delivery_mode
                if isinstance(command, SolveIssueCommand)
                else "advanced-matrix"
                if isinstance(command, AdvancedAuditCommand)
                else command.preset
            )
        elif isinstance(command, AuditAllProjectsCommand):
            project_name = "all"
            mode = f"{command.mode}:{command.preset}"
        elif isinstance(command, EnhancementDiscoverCommand):
            project_name = command.project.value
        elif isinstance(
            command,
            (EnhancementApproveCommand, EnhancementRejectCommand, EnhancementRunCommand),
        ):
            project_name = command.project.value
            mode = command.execution_mode if isinstance(command, EnhancementRunCommand) else None
        elif isinstance(command, LLMWikiBootstrapPlanCommand):
            project_name = command.project
        elif isinstance(command, EvidenceExportCampaignCommand):
            mode = command.format
        elif isinstance(command, EvidenceExportWorkflowCommand):
            mode = command.format

        if isinstance(command, AutonomousBacklogCommand):
            binding = {
                "campaign_id": str(command.campaign_id),
                "backlog_campaign_ids": {
                    key: str(value) for key, value in command.backlog_campaign_ids.items()
                },
                "policy_campaign_id": str(command.policy_campaign_id),
                "environment": "staging",
                "maximum_risk": "R2",
                "global_active_issues": 1,
                "global_deployments": 1,
                "production": False,
            }
            for project, campaign_id in command.backlog_campaign_ids.items():
                campaign = session.get(GitHubBacklogCampaign, campaign_id)
                if campaign is None or campaign.project != project:
                    blockers.append(f"{project.upper()}_BACKLOG_INVENTORY_INVALID")
                elif not _global_inventory_approval_current(
                    campaign,
                    project=project,
                    now=now,
                ):
                    blockers.append(f"{project.upper()}_BACKLOG_APPROVAL_NOT_CURRENT")
                else:
                    binding.setdefault("inventory_approval_bindings", {})[project] = (
                        campaign.approval_binding
                    )
            policy = session.get(GitHubBacklogCampaign, command.policy_campaign_id)
            owner_ids = {
                campaign.id
                for campaign in (
                    session.get(GitHubBacklogCampaign, campaign_id)
                    for campaign_id in command.backlog_campaign_ids.values()
                )
                if campaign is not None
                and campaign.approval_json.get("policy_role") in {None, "owner"}
            }
            if (
                policy is None
                or owner_ids != {command.policy_campaign_id}
                or policy.status not in {"APPROVED", "RUNNING"}
                or policy.approval_json.get("policy_role") not in {None, "owner"}
            ):
                blockers.append("GITHUB_WRITE_POLICY_NOT_ACTIVE")
            active = session.scalar(
                select(Run).where(
                    Run.run_type == "autonomous-global-backlog",
                    Run.status.in_(tuple(_ACTIVE_OPERATION_STATES | {"PAUSED", "BLOCKED"})),
                )
            )
            if active is not None:
                blockers.append("BACKLOG_CAMPAIGN_ALREADY_ACTIVE")
                binding["existing_workflow_id"] = active.workflow_id
        elif isinstance(command, SolveIssueCommand):
            if command.maximum_risk.value == "R3" and command.r3_approval_binding is None:
                blockers.append("R3_OPERATOR_ACTION_REQUIRED")
            if command.delivery_mode == "full-delivery":
                blockers.extend(self._full_delivery_blockers(command.project.value))
            issue = session.scalar(
                select(BacklogIssueRecord).where(
                    BacklogIssueRecord.backlog_campaign_id == command.backlog_campaign_id,
                    BacklogIssueRecord.repository == command.repository,
                    BacklogIssueRecord.issue_number == command.issue_number,
                )
            )
            campaign = session.get(GitHubBacklogCampaign, command.backlog_campaign_id)
            if issue is None or campaign is None or campaign.project != command.project.value:
                blockers.append("ISSUE_NOT_IN_PERSISTED_PROJECT_BACKLOG")
            else:
                binding = {
                    "backlog_campaign_id": str(campaign.id),
                    "source_repository": campaign.source_repository,
                    "deployment_repository": campaign.deployment_repository,
                    "source_head": campaign.source_head_at_import,
                    "deployment_head": campaign.deployment_head_at_import,
                    "plan_hash": campaign.plan_hash,
                    "issue_title": issue.title,
                    "issue_state": issue.state,
                    "issue_body_sha256": issue.body_sha256,
                    "risk": issue.calculated_risk,
                    "eligibility": issue.eligibility_status,
                }
                if issue.state.upper() != "OPEN":
                    blockers.append("GITHUB_ISSUE_NOT_OPEN")
                if issue.issue_type.upper() in {"PR", "PULL_REQUEST"}:
                    blockers.append("GITHUB_REFERENCE_IS_PULL_REQUEST")
                risk = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
                if risk.get(issue.calculated_risk, 99) > risk[command.maximum_risk.value]:
                    blockers.append("ISSUE_RISK_EXCEEDS_BOUND")
                if command.r3_approval_binding is not None and not self._approved_r3_binding(
                    session, issue, campaign, command.r3_approval_binding, datetime.now(UTC)
                ):
                    # NEW-95: the binding must name an approved, unexpired R3 candidate for
                    # exactly this issue, and the issue must be unchanged since approval.
                    blockers.append("R3_APPROVAL_REQUIRED")
                issue_operation = self._active_operation(
                    session,
                    "solve_issue",
                    f"{command.repository}#{command.issue_number}",
                    exclude_operation_id=exclude_operation_id,
                )
                if issue_operation is not None:
                    blockers.append("ISSUE_WORKFLOW_ALREADY_ACTIVE")
                    binding.update(
                        {
                            "existing_operation_id": str(issue_operation.id),
                            "existing_workflow_id": issue_operation.workflow_id,
                        }
                    )
            required_credentials.extend(["github-broker", "native-codex", "native-claude"])
            blockers.extend(self._provider_blockers(session))
        elif isinstance(command, SolveBacklogCommand):
            if command.maximum_risk.value == "R3":
                blockers.append("R3_OPERATOR_ACTION_REQUIRED")
            if command.delivery_mode == "full-delivery":
                blockers.extend(self._full_delivery_blockers(command.project))
            campaign = session.get(GitHubBacklogCampaign, command.backlog_campaign_id)
            if campaign is None or campaign.project != command.project:
                blockers.append("BACKLOG_CAMPAIGN_NOT_FOUND")
            else:
                binding = {
                    "backlog_campaign_id": str(campaign.id),
                    "source_repository": campaign.source_repository,
                    "deployment_repository": campaign.deployment_repository,
                    "source_head": campaign.source_head_at_import,
                    "deployment_head": campaign.deployment_head_at_import,
                    "plan_hash": campaign.plan_hash,
                    "issue_count": campaign.issue_count,
                    "eligible_count": campaign.eligible_count,
                    "import_filter": campaign.import_filter,
                }
                if (
                    command.issue_filter is not None
                    and campaign.import_filter != command.issue_filter
                ):
                    blockers.append("BACKLOG_QUERY_BINDING_CHANGED")
                backlog_operation = self._active_operation(
                    session,
                    "solve_backlog",
                    str(campaign.id),
                    exclude_operation_id=exclude_operation_id,
                )
                if backlog_operation is not None:
                    blockers.append("BACKLOG_CAMPAIGN_ALREADY_ACTIVE")
                    binding.update(
                        {
                            "existing_operation_id": str(backlog_operation.id),
                            "existing_workflow_id": backlog_operation.workflow_id,
                        }
                    )
            required_credentials.extend(["github-broker", "native-codex", "native-claude"])
            blockers.extend(self._provider_blockers(session))
        elif isinstance(command, (AuditPlanCommand, AuditStartCommand, AdvancedAuditCommand)):
            if isinstance(command, AdvancedAuditCommand):
                binding.update(
                    {
                        "name": command.name,
                        "source_sha": command.source_sha,
                        "deployment_sha": command.deployment_sha,
                        "coverage_cells": len(command.domains)
                        * len(command.personas)
                        * len(command.lenses)
                        * len(command.environments),
                        "execute": command.execute,
                    }
                )
                if command.execute:
                    required_credentials.extend(["native-codex", "native-claude"])
                    blockers.extend(self._provider_blockers(session))
            else:
                if command.preset == "documentation":
                    binding["effective_preset"] = "static-full"
                binding.update(
                    {
                        "preset": command.preset,
                        "environment": command.environment,
                        "publish_findings": command.publish_findings,
                    }
                )
                if isinstance(command, AuditStartCommand):
                    recent_plan = session.scalar(
                        select(OperationRequest.id).where(
                            OperationRequest.action == "audit_plan",
                            OperationRequest.target_id
                            == f"{command.project.value}:{command.preset}:{command.environment}",
                            OperationRequest.status == "COMPLETED",
                            OperationRequest.completed_at > now - timedelta(hours=1),
                        )
                    )
                    if recent_plan is None:
                        blockers.append("AUDIT_PLAN_REQUIRED")
                    required_credentials.extend(["native-codex", "native-claude"])
                    blockers.extend(self._provider_blockers(session))
        elif isinstance(command, AuditAllProjectsCommand):
            binding = {
                "projects": ["falcone", "musematic", "llmwiki"],
                "project_count": 3,
                "execution": "sequential",
                "preset": command.preset,
                "effective_preset": (
                    "static-full" if command.preset == "documentation" else command.preset
                ),
                "environment": command.environment,
                "mode": command.mode,
                "publish_findings": command.publish_findings,
            }
            if command.mode == "start":
                recent_plan = session.scalar(
                    select(OperationRequest.id).where(
                        OperationRequest.action == "audit_all_projects",
                        OperationRequest.target_id
                        == f"all:{command.preset}:{command.environment}:plan",
                        OperationRequest.status == "COMPLETED",
                        OperationRequest.completed_at > now - timedelta(hours=1),
                    )
                )
                if recent_plan is None:
                    blockers.append("ALL_PROJECT_AUDIT_PLAN_REQUIRED")
                active_audit_operation = self._active_operation(
                    session,
                    "audit_all_projects",
                    f"all:{command.preset}:{command.environment}:start",
                    exclude_operation_id=exclude_operation_id,
                )
                if active_audit_operation is not None:
                    blockers.append("ALL_PROJECT_AUDIT_ALREADY_ACTIVE")
                    binding.update(
                        {
                            "existing_operation_id": str(active_audit_operation.id),
                            "existing_workflow_id": active_audit_operation.workflow_id,
                        }
                    )
                required_credentials.extend(["native-codex", "native-claude"])
                blockers.extend(self._provider_blockers(session))
        elif isinstance(command, WorkflowControlCommand):
            state = self._workflow_state(session, command.workflow_id)
            binding = {"workflow_id": command.workflow_id, "observed_status": state}
            if state is None:
                blockers.append("WORKFLOW_NOT_FOUND")
            elif command.action == "retry" and state.upper() not in {
                "FAILED",
                "TIMED_OUT",
                "BLOCKED",
                "PAUSED",
            }:
                blockers.append("WORKFLOW_IS_NOT_RETRYABLE")
            elif command.action == "nudge" and state.upper() in _TERMINAL_WORKFLOW_STATES:
                blockers.append("WORKFLOW_IS_TERMINAL")
            elif command.action != "retry" and state.upper() in _TERMINAL_WORKFLOW_STATES:
                blockers.append("WORKFLOW_IS_TERMINAL")
            elif command.action == "resume" and state.upper() not in {"PAUSED", "BLOCKED"}:
                blockers.append("WORKFLOW_IS_NOT_PAUSED")
            elif command.action in {"pause", "pause_after_current"} and state.upper() in {
                "PAUSED",
                "BLOCKED",
            }:
                blockers.append("WORKFLOW_ALREADY_PAUSED")
            elif command.action == "stop_after_current" and state.upper() in {
                "PAUSED",
                "BLOCKED",
            }:
                blockers.append("WORKFLOW_IS_NOT_RUNNING")
            elif command.action in {"pause_after_current", "stop_after_current"} and not (
                command.workflow_id.startswith("backlog-loop-")
                or command.workflow_id.startswith("synthetic-backlog")
            ):
                blockers.append("CONTROL_REQUIRES_BACKLOG_CAMPAIGN")
        elif isinstance(command, RefreshConnectionsCommand):
            binding = {"scope": command.scope, "probe_policy": "BOUNDED_READ_ONLY"}
        elif isinstance(
            command,
            (EnhancementApproveCommand, EnhancementRejectCommand, EnhancementRunCommand),
        ):
            enhancement = session.scalar(
                select(Enhancement).where(Enhancement.enhancement_key == command.enhancement_id)
            )
            if enhancement is None:
                blockers.append("ENHANCEMENT_NOT_FOUND")
            else:
                binding = {
                    "enhancement_id": enhancement.enhancement_key,
                    "project": enhancement.project,
                    "campaign_id": enhancement.campaign_id,
                    "persona": enhancement.persona,
                    "affected_repository": enhancement.affected_repository,
                    "risk": enhancement.risk_class,
                    "status": enhancement.status,
                    "fingerprint": enhancement.fingerprint,
                    "approval_binding_sha256": enhancement.approval_binding_sha256,
                }
                if enhancement.project != command.project.value:
                    blockers.append("ENHANCEMENT_PROJECT_BINDING_CHANGED")
                if enhancement.approval_binding_sha256 is None:
                    blockers.append("ENHANCEMENT_APPROVAL_BINDING_MISSING")
                if (
                    command.approval_binding is not None
                    and command.approval_binding != enhancement.approval_binding_sha256
                ):
                    blockers.append("ENHANCEMENT_APPROVAL_BINDING_CHANGED")
                if isinstance(command, EnhancementApproveCommand) and enhancement.status != (
                    "CANDIDATE_ENHANCEMENT"
                ):
                    blockers.append("ENHANCEMENT_IS_NOT_A_CANDIDATE")
                if isinstance(command, EnhancementRejectCommand) and enhancement.status not in {
                    "CANDIDATE_ENHANCEMENT",
                    "APPROVED",
                }:
                    blockers.append("ENHANCEMENT_IS_NOT_REJECTABLE")
                if isinstance(command, EnhancementRunCommand):
                    if enhancement.status != "APPROVED":
                        blockers.append("ENHANCEMENT_APPROVAL_REQUIRED")
                    risk_rank = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}
                    if risk_rank.get(enhancement.risk_class, 99) > risk_rank[command.risk.value]:
                        blockers.append("ENHANCEMENT_RISK_EXCEEDS_BOUND")
                    active_operation = self._active_operation(
                        session,
                        command.action,
                        command.enhancement_id,
                        exclude_operation_id=exclude_operation_id,
                    )
                    if active_operation is not None:
                        blockers.append("ENHANCEMENT_WORKFLOW_ALREADY_ACTIVE")
                        binding.update(
                            {
                                "existing_operation_id": str(active_operation.id),
                                "existing_workflow_id": active_operation.workflow_id,
                            }
                        )
                    if command.execution_mode != "plan-only":
                        required_credentials.extend(["native-codex", "native-claude"])
                        blockers.extend(self._provider_blockers(session))
        elif isinstance(
            command,
            (EnhancementDiscoverCommand, FindingVerifyCommand, LLMWikiBootstrapPlanCommand),
        ):
            binding = {"typed_application_service": True}
            blockers.extend(self._provider_blockers(session))
        elif isinstance(command, EvidenceExportCampaignCommand):
            audit_campaign = session.get(Campaign, command.campaign_id)
            if audit_campaign is None:
                blockers.append("CAMPAIGN_NOT_FOUND")
            else:
                project_name = audit_campaign.project
                binding = {
                    "campaign_id": str(audit_campaign.id),
                    "project": audit_campaign.project,
                    "name": audit_campaign.name,
                    "status": audit_campaign.status,
                    "updated_at": _utc(audit_campaign.updated_at).isoformat(),
                    "coverage_cells": int(
                        session.scalar(
                            select(func.count(CoverageCell.id)).where(
                                CoverageCell.campaign_id == audit_campaign.id
                            )
                        )
                        or 0
                    ),
                    "findings": int(
                        session.scalar(
                            select(func.count(Finding.id)).where(
                                Finding.campaign_id == audit_campaign.id
                            )
                        )
                        or 0
                    ),
                }
        elif isinstance(command, EvidenceExportWorkflowCommand):
            run = session.scalar(
                select(Run)
                .where(Run.workflow_id == command.workflow_id)
                .order_by(desc(Run.updated_at))
                .limit(1)
            )
            if run is None:
                blockers.append("WORKFLOW_NOT_FOUND")
            else:
                project_name = run.project
                binding = {
                    "workflow_id": run.workflow_id,
                    "run_id": str(run.id),
                    "project": run.project,
                    "run_type": run.run_type,
                    "status": run.status,
                    "updated_at": _utc(run.updated_at).isoformat(),
                }

        status = (
            "AVAILABLE"
            if not blockers
            else "RUNNING"
            if blockers
            in (
                ["ISSUE_WORKFLOW_ALREADY_ACTIVE"],
                ["BACKLOG_CAMPAIGN_ALREADY_ACTIVE"],
                ["ENHANCEMENT_WORKFLOW_ALREADY_ACTIVE"],
            )
            else "BLOCKED"
        )
        return (
            CapabilityResult(
                capability=command.action,
                status=cast(Any, status),
                project=project_name,
                mode=str(mode) if mode is not None else None,
                blockers=sorted(set(blockers)),
                required_approval=(
                    "DASHBOARD_PASSWORD_REAUTHENTICATION"
                    if command.action != "refresh_connections"
                    else "DASHBOARD_PASSWORD_REAUTHENTICATION"
                ),
                required_credentials=required_credentials,
                checked_at=now,
                next_action=(
                    "Resolve the listed blockers and run preflight again."
                    if blockers
                    else "Confirm the immutable operation binding."
                ),
            ),
            binding,
        )

    def _load_project_manifest(self, project: str) -> ProjectManifest:
        """Load and validate one project's contract from project_config_root.

        Raises ValueError (with the project contract unconfigured/invalid distinction
        collapsed) on any missing, symlinked, oversized, or otherwise unsafe manifest --
        callers that need a fine-grained blocker code should catch OSError/ValueError/
        yaml.YAMLError separately, as _full_delivery_blockers does below.
        """
        root = self.project_config_root
        if root is None:
            raise ValueError("project contract root is not configured")
        path = root / f"{project}.yaml"
        if root.is_symlink() or path.is_symlink():
            raise ValueError("project contract cannot be a symlink")
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        metadata = resolved_path.stat()
        if (
            resolved_path.parent != resolved_root
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o022
            or metadata.st_size > 512 * 1024
        ):
            raise ValueError("project contract metadata is unsafe")
        raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8"))
        manifest = ProjectManifest.model_validate(raw)
        if manifest.project != project:
            raise ValueError("project contract identity mismatch")
        return manifest

    @staticmethod
    def _approved_r3_binding(
        session: Session,
        issue: BacklogIssueRecord,
        campaign: GitHubBacklogCampaign,
        binding: str,
        now: datetime,
    ) -> bool:
        row = session.scalar(
            select(ActionCandidate).where(
                ActionCandidate.action == "r3_issue_approval",
                ActionCandidate.target_id == f"{issue.repository}#{issue.issue_number}",
                ActionCandidate.binding_hash == binding,
                ActionCandidate.used_at.is_not(None),
                ActionCandidate.expires_at > now,
            )
        )
        if row is None or issue.calculated_risk != "R3":
            return False
        return r3_approval_payload_matches(
            row.payload_json,
            repository=issue.repository,
            issue_number=issue.issue_number,
            body_sha256=issue.body_sha256,
            github_updated_at=issue.github_updated_at.isoformat(),
            source_head=campaign.source_head_at_import,
            deployment_head=campaign.deployment_head_at_import,
        )

    def _full_delivery_blockers(self, project: str) -> list[str]:
        blockers: list[str] = []
        try:
            manifest = self._load_project_manifest(project)
            policy = DeliveryControlPolicy.model_validate(manifest.delivery_control)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            if self.project_config_root is None:
                return [*blockers, "FULL_DELIVERY_PROJECT_CONTRACT_NOT_CONFIGURED"]
            return [*blockers, "FULL_DELIVERY_PROJECT_CONTRACT_INVALID"]

        if policy.staging_flow is not None and policy.staging_flow.enabled:
            # Post-merge staging GitOps flow: the pre-merge candidate build,
            # immutable promotion and scoped staging-releaser gates do not apply;
            # the flow's own contract is cross-checked against the project instead.
            return list(dict.fromkeys(manifest.staging_flow_blockers()))
        if not policy.candidate_build.enabled:
            blockers.append(
                policy.candidate_build.blocker or "FULL_DELIVERY_CANDIDATE_BUILD_DISABLED"
            )
        if not policy.immutable_promotion.enabled:
            blockers.append(
                policy.immutable_promotion.blocker or "FULL_DELIVERY_IMMUTABLE_PROMOTION_DISABLED"
            )
        if not manifest.deployment.staging_policy.enabled:
            blockers.append(manifest.deployment.staging_policy.blocker)
        return list(dict.fromkeys(blockers))

    def _provider_blockers(self, session: Session) -> list[str]:
        blockers: list[str] = []
        for provider in ("codex", "claude"):
            snapshot = session.scalar(
                select(QuotaSnapshot)
                .where(QuotaSnapshot.provider == provider)
                .order_by(desc(QuotaSnapshot.captured_at))
                .limit(1)
            )
            if snapshot is None:
                blockers.append(f"{provider.upper()}_HEALTH_NOT_REPORTED")
                continue
            age = datetime.now(UTC) - _utc(snapshot.captured_at)
            if age > _PROVIDER_HEALTH_STALE_AFTER:
                blockers.append(f"{provider.upper()}_HEALTH_STALE")
            if snapshot.cli_present is False:
                blockers.append(f"{provider.upper()}_CLI_NOT_INSTALLED")
            if snapshot.authenticated is False:
                blockers.append(f"{provider.upper()}_AUTH_NOT_READY")
            if snapshot.rate_limited:
                blockers.append(f"{provider.upper()}_RATE_LIMITED")
        return blockers

    def _workflow_state(self, session: Session, workflow_id: str) -> str | None:
        run = session.scalar(
            select(Run)
            .where(Run.workflow_id == workflow_id)
            .order_by(desc(Run.updated_at))
            .limit(1)
        )
        if run is not None:
            return run.status
        loop = session.scalar(select(SolutionLoop).where(SolutionLoop.workflow_id == workflow_id))
        return loop.status if loop is not None else None

    @staticmethod
    def _confirmation_phrase(command: Command, binding_hash: str) -> str | None:
        if isinstance(command, SolveBacklogCommand):
            return f"SOLVE BACKLOG {binding_hash[:8].upper()}"
        if isinstance(command, AuditStartCommand) and command.preset in {
            "full-e2e",
            "security",
        }:
            return f"START AUDIT {binding_hash[:8].upper()}"
        if isinstance(command, AuditAllProjectsCommand) and command.mode == "start":
            return f"START ALL AUDITS {binding_hash[:8].upper()}"
        if isinstance(command, EnhancementApproveCommand):
            return f"APPROVE ENHANCEMENT {binding_hash[:8].upper()}"
        if isinstance(command, EnhancementRejectCommand):
            return f"REJECT ENHANCEMENT {binding_hash[:8].upper()}"
        if isinstance(command, EnhancementRunCommand) and command.execution_mode != "plan-only":
            return f"RUN ENHANCEMENT {binding_hash[:8].upper()}"
        return None

    def _active_target(
        self,
        session: Session,
        action: str,
        target_id: str,
        *,
        exclude_operation_id: UUID | None = None,
    ) -> bool:
        return (
            self._active_operation(
                session,
                action,
                target_id,
                exclude_operation_id=exclude_operation_id,
            )
            is not None
        )

    def _active_operation(
        self,
        session: Session,
        action: str,
        target_id: str,
        *,
        exclude_operation_id: UUID | None = None,
    ) -> OperationRequest | None:
        statement = (
            select(OperationRequest)
            .where(
                OperationRequest.action == action,
                OperationRequest.target_id == target_id,
                OperationRequest.status.in_(_ACTIVE_OPERATION_STATES),
            )
            .order_by(desc(OperationRequest.created_at))
        )
        if exclude_operation_id is not None:
            statement = statement.where(OperationRequest.id != exclude_operation_id)
        now = datetime.now(UTC)
        for operation in session.scalars(statement):
            if operation.status == "STARTED" and operation.workflow_id:
                workflow_state = self._workflow_state(session, operation.workflow_id)
                if (
                    workflow_state is not None
                    and workflow_state.upper() in _TERMINAL_WORKFLOW_STATES
                ):
                    operation.status = "COMPLETED"
                    operation.completed_at = operation.completed_at or now
                    continue
            return operation
        return None

    async def runless_started_operations(
        self, *, after: tuple[datetime, UUID] | None = None, limit: int = 20
    ) -> list[tuple[UUID, str, str, datetime]]:
        return await asyncio.to_thread(self._runless_started_operations, after, limit)

    def _runless_started_operations(
        self, after: tuple[datetime, UUID] | None, limit: int
    ) -> list[tuple[UUID, str, str, datetime]]:
        # NEW-97: a workflow that ended before recording a Run leaves its operation STARTED.
        # Only Temporal can tell whether it is still alive, so the caller checks each one,
        # paging by (created_at, id) so a long-lived page never hides newer operations.
        statement = (
            select(OperationRequest)
            .where(
                OperationRequest.status == "STARTED",
                OperationRequest.workflow_id.is_not(None),
                OperationRequest.temporal_run_id.is_not(None),
                ~select(Run.id).where(Run.workflow_id == OperationRequest.workflow_id).exists(),
                ~select(SolutionLoop.id)
                .where(SolutionLoop.workflow_id == OperationRequest.workflow_id)
                .exists(),
            )
            .order_by(OperationRequest.created_at, OperationRequest.id)
            .limit(limit)
        )
        if after is not None:
            created_at, operation_id = after
            statement = statement.where(
                or_(
                    OperationRequest.created_at > created_at,
                    and_(
                        OperationRequest.created_at == created_at,
                        OperationRequest.id > operation_id,
                    ),
                )
            )
        with self.sessions() as session:
            return [
                (
                    row.id,
                    cast(str, row.workflow_id),
                    cast(str, row.temporal_run_id),
                    row.created_at,
                )
                for row in session.scalars(statement)
            ]

    async def complete_closed_runless_operation(self, operation_id: UUID) -> bool:
        return await asyncio.to_thread(self._complete_closed_runless_operation, operation_id)

    def _complete_closed_runless_operation(self, operation_id: UUID) -> bool:
        """Complete a STARTED operation whose workflow Temporal reports closed without a Run."""
        with self.sessions.begin() as session:
            row = session.get(OperationRequest, operation_id, with_for_update=True)
            if row is None or row.status != "STARTED" or not row.workflow_id:
                return False
            if self._workflow_state(session, row.workflow_id) is not None:
                return False
            row.status = "COMPLETED"
            row.completed_at = row.completed_at or datetime.now(UTC)
            return True

    async def capability(
        self,
        command: Command,
        *,
        exclude_operation_id: UUID | None = None,
    ) -> CapabilityResult:
        return await asyncio.to_thread(self._capability_view, command, exclude_operation_id)

    def _capability_view(
        self,
        command: Command,
        exclude_operation_id: UUID | None,
    ) -> CapabilityResult:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            capability, _binding = self._capability(
                session,
                command,
                now,
                exclude_operation_id=exclude_operation_id,
            )
            return capability

    async def create_reauthentication(self, candidate_id: UUID, session_binding_hash: str) -> str:
        return await asyncio.to_thread(
            self._create_reauthentication, candidate_id, session_binding_hash
        )

    def _create_reauthentication(self, candidate_id: UUID, session_binding_hash: str) -> str:
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.sessions.begin() as session:
            candidate = session.get(ActionCandidate, candidate_id, with_for_update=True)
            if (
                candidate is None
                or candidate.used_at is not None
                or _utc(candidate.expires_at) <= now
                or not hmac.compare_digest(candidate.session_binding_hash, session_binding_hash)
                or candidate.capability_status != "AVAILABLE"
            ):
                raise ValueError("action candidate is unavailable")
            prior = session.scalar(
                select(ReauthenticationBinding).where(
                    ReauthenticationBinding.candidate_id == candidate_id
                )
            )
            if prior is not None:
                session.delete(prior)
                session.flush()
            session.add(
                ReauthenticationBinding(
                    candidate_id=candidate_id,
                    token_digest=digest,
                    session_binding_hash=session_binding_hash,
                    created_at=now,
                    expires_at=now + timedelta(minutes=5),
                    used_at=None,
                )
            )
        return token

    async def queue_candidate(
        self,
        candidate_id: UUID,
        reauthentication_token: str,
        session_binding_hash: str,
        confirmation_phrase: str | None,
    ) -> OperationView:
        return await asyncio.to_thread(
            self._queue_candidate,
            candidate_id,
            reauthentication_token,
            session_binding_hash,
            confirmation_phrase,
        )

    def _queue_candidate(
        self,
        candidate_id: UUID,
        reauthentication_token: str,
        session_binding_hash: str,
        confirmation_phrase: str | None,
    ) -> OperationView:
        now = datetime.now(UTC)
        token_digest = hashlib.sha256(reauthentication_token.encode()).hexdigest()
        try:
            with self.sessions.begin() as session:
                candidate = session.get(ActionCandidate, candidate_id, with_for_update=True)
                binding = session.scalar(
                    select(ReauthenticationBinding)
                    .where(ReauthenticationBinding.candidate_id == candidate_id)
                    .with_for_update()
                )
                if candidate is None or binding is None:
                    raise ValueError("reauthentication binding is unavailable")
                if candidate.used_at is not None or _utc(candidate.expires_at) <= now:
                    raise ValueError("action candidate expired or was already used")
                if binding.used_at is not None or _utc(binding.expires_at) <= now:
                    raise ValueError("reauthentication expired or was already used")
                if not hmac.compare_digest(candidate.session_binding_hash, session_binding_hash):
                    raise ValueError("action candidate session binding changed")
                if not hmac.compare_digest(binding.session_binding_hash, session_binding_hash):
                    raise ValueError("reauthentication session binding changed")
                if not hmac.compare_digest(binding.token_digest, token_digest):
                    raise ValueError("reauthentication token is invalid")
                expected_phrase = candidate.confirmation_phrase or ""
                if not hmac.compare_digest(expected_phrase, confirmation_phrase or ""):
                    raise ValueError("typed confirmation does not match the operation binding")
                existing = session.scalar(
                    select(OperationRequest).where(
                        OperationRequest.idempotency_key == candidate.binding_hash
                    )
                )
                if existing is None and self._active_target(
                    session, candidate.action, candidate.target_id
                ):
                    existing = session.scalar(
                        select(OperationRequest)
                        .where(
                            OperationRequest.action == candidate.action,
                            OperationRequest.target_id == candidate.target_id,
                            OperationRequest.status.in_(_ACTIVE_OPERATION_STATES),
                        )
                        .order_by(desc(OperationRequest.created_at))
                        .limit(1)
                    )
                if existing is None:
                    existing = OperationRequest(
                        action=candidate.action,
                        project=candidate.project,
                        target_type=candidate.target_type,
                        target_id=candidate.target_id,
                        payload_json=candidate.payload_json,
                        source_channel="dashboard",
                        operator_class="password-reauthenticated-operator",
                        idempotency_key=candidate.binding_hash,
                        binding_hash=candidate.binding_hash,
                        workflow_id=None,
                        temporal_run_id=None,
                        status="PENDING",
                        safe_error=None,
                        created_at=now,
                        confirmed_at=now,
                        started_at=None,
                        completed_at=None,
                    )
                    session.add(existing)
                    session.flush()
                candidate.used_at = now
                binding.used_at = now
                return self._operation_view(existing)
        except IntegrityError:
            with self.sessions() as session:
                candidate = session.get(ActionCandidate, candidate_id)
                if candidate is None:
                    raise
                existing = session.scalar(
                    select(OperationRequest).where(
                        OperationRequest.idempotency_key == candidate.binding_hash
                    )
                )
                if existing is None:
                    raise
                return self._operation_view(existing)

    async def operation(self, operation_id: UUID) -> OperationView | None:
        return await asyncio.to_thread(self._operation, operation_id)

    def _operation(self, operation_id: UUID) -> OperationView | None:
        with self.sessions() as session:
            row = session.get(OperationRequest, operation_id)
            if row is None:
                return None
            result_available = False
            if row.status == "COMPLETED" and row.action in {
                "evidence_export_campaign",
                "evidence_export_workflow",
            }:
                operation_export = session.scalar(
                    select(OperationExport).where(OperationExport.operation_id == row.id)
                )
                result_available = self._valid_export(operation_export, datetime.now(UTC))
            return self._operation_view(row, result_available=result_available)

    async def claim_pending(self) -> tuple[UUID, Command, SourceChannel, str] | None:
        return await asyncio.to_thread(self._claim_pending)

    def _claim_pending(self) -> tuple[UUID, Command, SourceChannel, str] | None:
        with self.sessions.begin() as session:
            statement = (
                select(OperationRequest)
                .where(OperationRequest.status == "PENDING")
                .order_by(OperationRequest.created_at)
                .limit(1)
            )
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = session.scalar(statement)
            if row is None:
                return None
            row.status = "DISPATCHING"
            row.started_at = datetime.now(UTC)
            return (
                row.id,
                parse_command(row.payload_json),
                cast(SourceChannel, row.source_channel),
                row.operator_class,
            )

    async def claim(self, operation_id: UUID) -> tuple[UUID, Command, SourceChannel, str] | None:
        return await asyncio.to_thread(self._claim, operation_id)

    def _claim(self, operation_id: UUID) -> tuple[UUID, Command, SourceChannel, str] | None:
        with self.sessions.begin() as session:
            row = session.get(OperationRequest, operation_id, with_for_update=True)
            if row is None or row.status != "PENDING":
                return None
            row.status = "DISPATCHING"
            row.started_at = datetime.now(UTC)
            return (
                row.id,
                parse_command(row.payload_json),
                cast(SourceChannel, row.source_channel),
                row.operator_class,
            )

    async def finish(
        self,
        operation_id: UUID,
        *,
        status: str,
        workflow_id: str | None = None,
        run_id: str | None = None,
        safe_error: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._finish,
            operation_id,
            status,
            workflow_id,
            run_id,
            safe_error,
        )

    def _finish(
        self,
        operation_id: UUID,
        status: str,
        workflow_id: str | None,
        run_id: str | None,
        safe_error: str | None,
    ) -> None:
        if status not in {"STARTED", "COMPLETED", "FAILED", "BLOCKED"}:
            raise ValueError("operation terminal dispatch status is invalid")
        with self.sessions.begin() as session:
            row = session.get(OperationRequest, operation_id, with_for_update=True)
            if row is None:
                raise ValueError("operation disappeared during dispatch")
            row.status = status
            row.workflow_id = workflow_id
            row.temporal_run_id = run_id
            row.safe_error = (
                str(self.redactor.redact(safe_error[:2000]).value)[:500] if safe_error else None
            )
            if status in {"COMPLETED", "FAILED", "BLOCKED"}:
                row.completed_at = datetime.now(UTC)

    async def record_dispatch_event(
        self,
        operation_id: UUID,
        *,
        event_type: str,
        level: str,
        message: str,
        workflow_id: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._record_dispatch_event,
            operation_id,
            event_type,
            level,
            message,
            workflow_id,
        )

    def _record_dispatch_event(
        self,
        operation_id: UUID,
        event_type: str,
        level: str,
        message: str,
        workflow_id: str | None,
    ) -> None:
        with self.sessions.begin() as session:
            operation = session.get(OperationRequest, operation_id)
            if operation is None:
                return
            redacted = str(self.redactor.redact(message).value)[:500]
            session.add(
                DashboardEvent(
                    occurred_at=datetime.now(UTC),
                    level=level,
                    event_type=event_type,
                    source="controller",
                    host=None,
                    worker_kind="controller",
                    project=operation.project,
                    workflow_id=(
                        workflow_id
                        or (operation.target_id if operation.target_type == "workflow" else None)
                    ),
                    temporal_run_id=operation.temporal_run_id,
                    issue_number=(
                        int(operation.target_id.rsplit("#", 1)[1])
                        if operation.target_type == "github_issue"
                        and operation.target_id.rsplit("#", 1)[-1].isdigit()
                        else None
                    ),
                    campaign_id=(
                        operation.target_id
                        if operation.target_type in {"backlog_campaign", "audit", "audit_campaign"}
                        else None
                    ),
                    entity_type="operation",
                    entity_id=str(operation.id),
                    current_phase=operation.status,
                    message=redacted,
                    metadata_json={
                        "action": operation.action,
                        "status": operation.status,
                        "operation": str(operation.id),
                    },
                )
            )
            if event_type == "operation.started" and workflow_id:
                session.add(
                    NotificationOutbox(
                        topic="telegram.engineering-operation-started",
                        status="PENDING",
                        payload_json={
                            "operation_id": str(operation.id),
                            "project": operation.project,
                            "target": operation.target_id,
                            "workflow_id": workflow_id,
                            "phase": "QUEUED",
                        },
                        attempts=0,
                        next_attempt_at=datetime.now(UTC),
                        delivered_at=None,
                        last_error_code=None,
                    )
                )

    async def create_external(
        self,
        command: Command,
        *,
        source_channel: SourceChannel,
        operator_class: str,
        idempotency_key: str,
    ) -> OperationView:
        return await asyncio.to_thread(
            self._create_external,
            command,
            source_channel,
            operator_class,
            idempotency_key,
        )

    def _create_external(
        self,
        command: Command,
        source_channel: SourceChannel,
        operator_class: str,
        idempotency_key: str,
    ) -> OperationView:
        if not 16 <= len(idempotency_key) <= 128:
            raise ValueError("idempotency key is invalid")
        target_type, target_id, project = command_target(command)
        payload = command.model_dump(mode="json")
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        with self.sessions.begin() as session:
            resolved_binding: dict[str, Any] = {}
            if isinstance(command, (EvidenceExportCampaignCommand, EvidenceExportWorkflowCommand)):
                capability, resolved_binding = self._capability(session, command, datetime.now(UTC))
                project = project or capability.project
            if (
                isinstance(
                    command,
                    (EnhancementApproveCommand, EnhancementRejectCommand, EnhancementRunCommand),
                )
                and command.approval_binding is None
            ):
                enhancement = session.scalar(
                    select(Enhancement).where(Enhancement.enhancement_key == command.enhancement_id)
                )
                if enhancement is not None and isinstance(enhancement.approval_binding_sha256, str):
                    payload["approval_binding"] = enhancement.approval_binding_sha256
            binding_hash = canonical_sha256(
                {
                    "payload": payload,
                    "target_type": target_type,
                    "target_id": target_id,
                    "project": project,
                    "resolved": resolved_binding,
                    "production": False,
                }
                if resolved_binding
                else payload
            )
            existing = session.scalar(
                select(OperationRequest).where(OperationRequest.idempotency_key == digest)
            )
            if existing is None and self._active_target(session, command.action, target_id):
                existing = session.scalar(
                    select(OperationRequest)
                    .where(
                        OperationRequest.action == command.action,
                        OperationRequest.target_id == target_id,
                        OperationRequest.status.in_(_ACTIVE_OPERATION_STATES),
                    )
                    .order_by(desc(OperationRequest.created_at))
                    .limit(1)
                )
            if existing is None:
                existing = OperationRequest(
                    action=command.action,
                    project=project,
                    target_type=target_type,
                    target_id=target_id,
                    payload_json=payload,
                    source_channel=source_channel,
                    operator_class=operator_class[:80],
                    idempotency_key=digest,
                    binding_hash=binding_hash,
                    workflow_id=None,
                    temporal_run_id=None,
                    status="PENDING",
                    safe_error=None,
                    created_at=datetime.now(UTC),
                    confirmed_at=datetime.now(UTC),
                    started_at=None,
                    completed_at=None,
                )
                session.add(existing)
                session.flush()
            return self._operation_view(existing)

    async def upsert_connection(self, value: dict[str, Any]) -> None:
        await asyncio.to_thread(self._upsert_connection, value)

    def _upsert_connection(self, value: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.scalar(
                select(ConnectionSnapshot).where(ConnectionSnapshot.component == value["component"])
            )
            if row is None:
                row = ConnectionSnapshot(
                    component=value["component"],
                    logical_target=value["logical_target"],
                    status=value["status"],
                    checked_at=value.get("checked_at", now),
                    last_success_at=value.get("last_success_at"),
                    last_failure_at=value.get("last_failure_at"),
                    latency_ms=value.get("latency_ms"),
                    safe_error=value.get("safe_error"),
                    source=value["source"],
                    next_action=value.get("next_action"),
                    detail_json=value.get("details", {}),
                )
                session.add(row)
            else:
                for key, target in (
                    ("logical_target", "logical_target"),
                    ("status", "status"),
                    ("checked_at", "checked_at"),
                    ("last_success_at", "last_success_at"),
                    ("last_failure_at", "last_failure_at"),
                    ("latency_ms", "latency_ms"),
                    ("safe_error", "safe_error"),
                    ("source", "source"),
                    ("next_action", "next_action"),
                    ("details", "detail_json"),
                ):
                    if key in value:
                        setattr(row, target, value[key])

    async def connections(self) -> list[ConnectionView]:
        return await asyncio.to_thread(self._connections)

    async def operator_snapshot(self, surface: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._operator_snapshot, surface)

    def _operator_snapshot(self, surface: str) -> dict[str, Any]:
        if surface not in {
            "readiness",
            "readiness-all",
            "readiness-core",
            "workers",
            "providers",
            "channel",
            "temporal",
        }:
            raise ValueError("operator snapshot surface is not allowlisted")
        now = datetime.now(UTC)
        with self.sessions() as session:
            connections = session.scalars(
                select(ConnectionSnapshot).order_by(ConnectionSnapshot.component)
            ).all()
            heartbeats = session.scalars(
                select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_kind)
            ).all()
            events = session.scalars(
                select(DashboardEvent)
                .where(DashboardEvent.source == "hermes")
                .order_by(desc(DashboardEvent.occurred_at))
                .limit(500)
            ).all()
            quotas = {
                provider: session.scalar(
                    select(QuotaSnapshot)
                    .where(QuotaSnapshot.provider == provider)
                    .order_by(desc(QuotaSnapshot.captured_at))
                    .limit(1)
                )
                for provider in ("codex", "claude")
            }
            runs = session.scalars(select(Run).order_by(desc(Run.updated_at)).limit(1000)).all()
        if surface == "workers":
            return {
                "status": "READY"
                if heartbeats
                and all((now - _utc(row.observed_at)).total_seconds() <= 180 for row in heartbeats)
                else "DEGRADED",
                "workers": [
                    {
                        "identity": row.identity,
                        "hostname": row.hostname,
                        "worker_kind": row.worker_kind,
                        "observed_at": row.observed_at.isoformat(),
                        "freshness_seconds": max(
                            0, int((now - _utc(row.observed_at)).total_seconds())
                        ),
                    }
                    for row in heartbeats
                ],
            }
        if surface == "providers":
            return {
                "status": "READY"
                if all(
                    value is not None
                    and value.authenticated is True
                    and value.cli_present is True
                    and not value.rate_limited
                    for value in quotas.values()
                )
                else "DEGRADED",
                "providers": {
                    provider: (
                        {
                            "provider_id": f"{provider}_cli",
                            "authenticated": row.authenticated,
                            "cli_present": row.cli_present,
                            "rate_limited": row.rate_limited,
                            "running_jobs": row.running_jobs,
                            "status": row.status,
                            "captured_at": row.captured_at.isoformat(),
                            "source": row.source,
                        }
                        if row is not None
                        else {"provider_id": f"{provider}_cli", "status": "NOT_REPORTED"}
                    )
                    for provider, row in quotas.items()
                },
                "permitted_provider_ids": ["codex_cli", "claude_cli"],
            }
        if surface == "channel":
            latest: dict[str, DashboardEvent] = {}
            for row in events:
                latest.setdefault(row.event_type, row)

            def event_time(name: str) -> str | None:
                row = latest.get(name)
                return row.occurred_at.isoformat() if row is not None else None

            outbound = latest.get("telegram.response_delivered")
            inbound = latest.get("telegram.command_received")
            latency_ms = None
            if (
                outbound is not None
                and inbound is not None
                and outbound.occurred_at >= inbound.occurred_at
            ):
                latency_ms = int(
                    (_utc(outbound.occurred_at) - _utc(inbound.occurred_at)).total_seconds() * 1000
                )
            last_error = latest.get("telegram.response_delivery_failed")
            return {
                "status": "CONNECTED" if outbound is not None else "STALE",
                "gateway_state": "RUNNING" if events else "NOT_REPORTED",
                "transport_mode": "telegram",
                "last_inbound_update": event_time("telegram.command_received"),
                "last_recognized_command": event_time("telegram.command_recognized"),
                "last_api_request": event_time("engineering.api_request_started"),
                "last_outbound_reply": event_time("telegram.response_delivered"),
                "latency_ms": latency_ms,
                "hook_profile": "engineering-control-fixed-commands",
                "last_safe_error": last_error.message if last_error is not None else None,
            }
        if surface == "temporal":
            temporal = next(
                (row for row in connections if row.component.lower() == "temporal"), None
            )
            active = [
                row
                for row in runs
                if row.status not in {"COMPLETE", "COMPLETED", "SUCCESS", "FAILED", "CANCELLED"}
            ]
            return {
                "status": temporal.status if temporal is not None else "NOT_REPORTED",
                "target": temporal.logical_target if temporal is not None else None,
                "checked_at": temporal.checked_at.isoformat() if temporal is not None else None,
                "active_workflow_projections": len(active),
                "recent_workflow_projections": len(runs),
                "worker_heartbeat_count": len(heartbeats),
            }
        required_connections = [
            row
            for row in connections
            if row.component != "worker-controller"
            and (surface != "readiness-core" or not row.component.startswith("project-"))
        ]
        statuses = [row.status for row in required_connections]
        return {
            "status": "READY"
            if statuses and all(value == "CONNECTED" for value in statuses)
            else "DEGRADED",
            "connection_count": len(required_connections),
            "connected_count": sum(value == "CONNECTED" for value in statuses),
            "worker_count": len(heartbeats),
            "checked_at": now.isoformat(),
        }

    async def create_export(
        self,
        operation_id: UUID,
        command: EvidenceExportCampaignCommand | EvidenceExportWorkflowCommand,
    ) -> None:
        await asyncio.to_thread(self._create_export, operation_id, command)

    def _create_export(
        self,
        operation_id: UUID,
        command: EvidenceExportCampaignCommand | EvidenceExportWorkflowCommand,
    ) -> None:
        now = datetime.now(UTC)
        content = (
            self._campaign_export(command.campaign_id, now)
            if isinstance(command, EvidenceExportCampaignCommand)
            else self._workflow_export(command.workflow_id, now)
        )
        report = self.redactor.redact(content)
        if not isinstance(report.value, dict):
            raise ValueError("operation export did not produce an object")
        export_value = report.value
        if isinstance(command, EvidenceExportCampaignCommand):
            export_value = self._bounded_campaign_export(export_value)
        encoded = self._encode_export(export_value)
        if not encoded or len(encoded) > self.export_max_bytes:
            raise ValueError("operation export exceeds the configured size bound")
        digest = hashlib.sha256(encoded).hexdigest()
        export_kind = (
            "campaign" if isinstance(command, EvidenceExportCampaignCommand) else "workflow"
        )
        target_id = (
            str(command.campaign_id)
            if isinstance(command, EvidenceExportCampaignCommand)
            else command.workflow_id
        )
        with self.sessions.begin() as session:
            operation = session.get(OperationRequest, operation_id, with_for_update=True)
            if (
                operation is None
                or operation.status != "DISPATCHING"
                or operation.action != command.action
                or operation.target_id != target_id
            ):
                raise ValueError("operation export binding changed before persistence")
            existing = session.scalar(
                select(OperationExport).where(OperationExport.operation_id == operation_id)
            )
            if existing is not None:
                if existing.content_sha256 != digest:
                    raise ValueError("operation export idempotency conflict")
                return
            session.add(
                OperationExport(
                    operation_id=operation_id,
                    export_kind=export_kind,
                    target_id=target_id,
                    content_json=export_value,
                    content_sha256=digest,
                    size_bytes=len(encoded),
                    created_at=now,
                    expires_at=now + timedelta(days=self.export_retention_days),
                )
            )

    def _workflow_export(self, workflow_id: str, now: datetime) -> dict[str, Any]:
        workflow = self.observability.workflow(workflow_id)
        if workflow is None:
            raise ValueError("workflow not found")
        return {
            "schema_version": 1,
            "export_kind": "workflow-summary",
            "generated_at": now.isoformat(),
            "retention_days": self.export_retention_days,
            "source": "redacted-dashboard-workflow-projection",
            "workflow": workflow,
            "exclusions": [
                "raw prompts",
                "raw model responses",
                "agent transcript content",
                "journal content",
                "credential material",
                "raw artifact content",
            ],
        }

    def _campaign_export(self, campaign_id: UUID, now: datetime) -> dict[str, Any]:
        with self.sessions() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise ValueError("campaign not found")
            status_counts = {
                str(status): int(count)
                for status, count in session.execute(
                    select(CoverageCell.status, func.count(CoverageCell.id))
                    .where(CoverageCell.campaign_id == campaign_id)
                    .group_by(CoverageCell.status)
                ).all()
            }
            finding_rows = list(
                session.scalars(
                    select(Finding)
                    .where(Finding.campaign_id == campaign_id)
                    .order_by(desc(Finding.updated_at))
                    .limit(501)
                ).all()
            )
            finding_total = int(
                session.scalar(
                    select(func.count(Finding.id)).where(Finding.campaign_id == campaign_id)
                )
                or 0
            )
            run_rows = list(
                session.scalars(
                    select(Run)
                    .where(Run.campaign_id == campaign_id)
                    .order_by(desc(Run.updated_at))
                    .limit(501)
                ).all()
            )
            run_total = int(
                session.scalar(select(func.count(Run.id)).where(Run.campaign_id == campaign_id))
                or 0
            )
            artifact_rows = list(
                session.scalars(
                    select(Artifact)
                    .where(Artifact.campaign_id == campaign_id)
                    .order_by(desc(Artifact.created_at))
                    .limit(501)
                ).all()
            )
            artifact_total = int(
                session.scalar(
                    select(func.count(Artifact.id)).where(Artifact.campaign_id == campaign_id)
                )
                or 0
            )
            metadata = campaign.metadata_json if isinstance(campaign.metadata_json, dict) else {}
            raw_blockers = metadata.get("root_blockers")
            blockers = raw_blockers if isinstance(raw_blockers, dict) else {}
            return {
                "schema_version": 1,
                "export_kind": "campaign-summary",
                "generated_at": now.isoformat(),
                "retention_days": self.export_retention_days,
                "source": "bounded-campaign-metadata-projection",
                "campaign": {
                    "campaign_id": str(campaign.id),
                    "project": campaign.project,
                    "name": campaign.name,
                    "status": campaign.status,
                    "source_sha": campaign.source_sha,
                    "deployment_sha": campaign.deployment_sha,
                    "required_cells": campaign.required_cells,
                    "created_at": _utc(campaign.created_at).isoformat(),
                    "updated_at": _utc(campaign.updated_at).isoformat(),
                },
                "coverage": {
                    "total": sum(status_counts.values()),
                    "status_counts": status_counts,
                    "repository_surfaces": int(
                        session.scalar(
                            select(
                                func.count(func.distinct(CoverageCell.repository_surface))
                            ).where(CoverageCell.campaign_id == campaign_id)
                        )
                        or 0
                    ),
                    "domains": int(
                        session.scalar(
                            select(func.count(func.distinct(CoverageCell.domain))).where(
                                CoverageCell.campaign_id == campaign_id
                            )
                        )
                        or 0
                    ),
                    "personas": int(
                        session.scalar(
                            select(func.count(func.distinct(CoverageCell.persona))).where(
                                CoverageCell.campaign_id == campaign_id
                            )
                        )
                        or 0
                    ),
                },
                "root_blockers": [
                    {
                        "blocker_id": str(identity)[:120],
                        "code": str(value.get("code") or identity)[:120],
                        "classification": str(value.get("classification") or "")[:80],
                        "summary": str(value.get("summary") or "")[:500],
                        "resolution_state": str(value.get("resolution_state") or "open")[:80],
                    }
                    for identity, raw in sorted(blockers.items(), key=lambda item: str(item[0]))[
                        :200
                    ]
                    for value in [raw if isinstance(raw, dict) else {}]
                ],
                "findings": [
                    {
                        "finding_id": str(row.id),
                        "fingerprint": row.fingerprint,
                        "domain": row.domain,
                        "persona": row.persona,
                        "severity": row.severity,
                        "risk_class": row.risk_class,
                        "disposition": row.disposition,
                        "affected_component": row.affected_component,
                        "recommended_action": row.recommended_action[:1000],
                        "updated_at": _utc(row.updated_at).isoformat(),
                    }
                    for row in finding_rows[:500]
                ],
                "runs": [
                    {
                        "run_id": str(row.id),
                        "workflow_id": row.workflow_id,
                        "run_type": row.run_type,
                        "status": row.status,
                        "worker": row.worker,
                        "error_code": row.error_code,
                        "summary": row.summary[:1000] if row.summary else None,
                        "started_at": _utc(row.started_at).isoformat() if row.started_at else None,
                        "finished_at": _utc(row.finished_at).isoformat()
                        if row.finished_at
                        else None,
                        "updated_at": _utc(row.updated_at).isoformat(),
                    }
                    for row in run_rows[:500]
                ],
                "artifacts": [
                    {
                        "artifact_id": str(row.id),
                        "sha256": row.sha256,
                        "content_type": row.content_type,
                        "producer": row.producer,
                        "redaction_status": row.redaction_status,
                        "size_bytes": row.size_bytes,
                        "retention_class": row.retention_class,
                        "purged": row.purged_at is not None,
                    }
                    for row in artifact_rows[:500]
                ],
                "truncated": {
                    "root_blockers": len(blockers) > 200,
                    "findings": finding_total > 500,
                    "runs": run_total > 500,
                    "artifacts": artifact_total > 500,
                },
                "collection_counts": {
                    "root_blockers": {
                        "available": len(blockers),
                        "included": min(len(blockers), 200),
                    },
                    "findings": {
                        "available": finding_total,
                        "included": min(finding_total, 500),
                    },
                    "runs": {
                        "available": run_total,
                        "included": min(run_total, 500),
                    },
                    "artifacts": {
                        "available": artifact_total,
                        "included": min(artifact_total, 500),
                    },
                },
                "exclusions": [
                    "raw evidence content",
                    "raw coverage results",
                    "raw prompts and model responses",
                    "agent transcript content",
                    "journal content",
                    "credential material",
                    "internal filesystem paths",
                ],
            }

    async def export_content(self, operation_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._export_content, operation_id)

    def _export_content(self, operation_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            operation = session.get(OperationRequest, operation_id)
            if (
                operation is None
                or operation.status != "COMPLETED"
                or operation.action not in {"evidence_export_campaign", "evidence_export_workflow"}
            ):
                return None
            export = session.scalar(
                select(OperationExport).where(OperationExport.operation_id == operation_id)
            )
            if not self._valid_export(export, datetime.now(UTC)):
                return None
            assert export is not None
            return export.content_json

    @staticmethod
    def _encode_export(value: dict[str, Any]) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _bounded_campaign_export(self, value: dict[str, Any]) -> dict[str, Any]:
        """Proportionally retain collection prefixes until the redacted export fits."""
        if len(self._encode_export(value)) <= self.export_max_bytes:
            return value
        collections = {
            key: list(value.get(key, []))
            for key in _CAMPAIGN_EXPORT_COLLECTIONS
            if isinstance(value.get(key), list)
        }
        counts = value.get("collection_counts")
        if not isinstance(counts, dict):
            counts = {}
            value["collection_counts"] = counts
        truncated = value.get("truncated")
        if not isinstance(truncated, dict):
            truncated = {}
            value["truncated"] = truncated
        available: dict[str, int] = {}
        for key, rows in collections.items():
            raw = counts.get(key)
            count = raw.get("available") if isinstance(raw, dict) else None
            available[key] = count if isinstance(count, int) and count >= len(rows) else len(rows)

        def apply_ratio(parts_per_million: int) -> None:
            for key, rows in collections.items():
                included = (len(rows) * parts_per_million) // 1_000_000
                value[key] = rows[:included]
                counts[key] = {"available": available[key], "included": included}
                truncated[key] = available[key] > included

        low = 0
        high = 1_000_000
        best: int | None = None
        while low <= high:
            midpoint = (low + high) // 2
            apply_ratio(midpoint)
            if len(self._encode_export(value)) <= self.export_max_bytes:
                best = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        apply_ratio(best if best is not None else 0)
        return value

    @classmethod
    def _valid_export(cls, export: OperationExport | None, now: datetime) -> bool:
        if export is None or _utc(export.expires_at) <= now:
            return False
        try:
            encoded = cls._encode_export(export.content_json)
        except (TypeError, ValueError):
            return False
        return export.size_bytes == len(encoded) and hmac.compare_digest(
            export.content_sha256,
            hashlib.sha256(encoded).hexdigest(),
        )

    def _connections(self) -> list[ConnectionView]:
        now = datetime.now(UTC)
        with self.sessions() as session:
            rows = session.scalars(
                select(ConnectionSnapshot).order_by(ConnectionSnapshot.component)
            ).all()
            return [
                ConnectionView(
                    component=row.component,
                    logical_target=row.logical_target,
                    status=cast(Any, row.status),
                    checked_at=row.checked_at,
                    last_success_at=row.last_success_at,
                    last_failure_at=row.last_failure_at,
                    latency_ms=row.latency_ms,
                    freshness_seconds=max(0, int((now - _utc(row.checked_at)).total_seconds())),
                    safe_error=row.safe_error,
                    source=row.source,
                    next_action=row.next_action,
                    details=row.detail_json,
                )
                for row in rows
            ]

    @staticmethod
    def _operation_view(
        row: OperationRequest,
        *,
        result_available: bool = False,
    ) -> OperationView:
        return OperationView(
            operation_id=row.id,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            source_channel=cast(SourceChannel, row.source_channel),
            status=row.status,
            workflow_id=row.workflow_id,
            temporal_run_id=row.temporal_run_id,
            binding_hash=row.binding_hash,
            safe_error=row.safe_error,
            created_at=row.created_at,
            result_available=result_available,
        )

    def health_sources(self) -> dict[str, Any]:
        with self.sessions() as session:
            heartbeats = session.scalars(select(WorkerHeartbeat)).all()
            events = session.scalars(
                select(DashboardEvent).order_by(desc(DashboardEvent.occurred_at)).limit(1000)
            ).all()
            quotas = {
                provider: session.scalar(
                    select(QuotaSnapshot)
                    .where(QuotaSnapshot.provider == provider)
                    .order_by(desc(QuotaSnapshot.captured_at))
                    .limit(1)
                )
                for provider in ("codex", "claude")
            }
            projects = {
                project: session.scalar(
                    select(ProjectSnapshot)
                    .where(ProjectSnapshot.project == project)
                    .order_by(desc(ProjectSnapshot.updated_at))
                    .limit(1)
                )
                for project in ("falcone", "musematic", "llmwiki")
            }
            return {
                "heartbeats": heartbeats,
                "events": events,
                "quotas": quotas,
                "projects": projects,
            }

    async def campaign_reconciliation_candidates(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._campaign_reconciliation_candidates, limit, offset)

    def _campaign_reconciliation_candidates(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 201 or not 0 <= offset <= 10_000:
            raise ValueError("campaign reconciliation page is outside its safety bound")
        with self.sessions() as session:
            campaigns = session.scalars(
                select(Campaign)
                .order_by(desc(Campaign.updated_at), desc(Campaign.id))
                .offset(offset)
                .limit(limit)
            ).all()
            values: list[dict[str, Any]] = []
            now = datetime.now(UTC)
            for campaign in campaigns:
                metadata = (
                    campaign.metadata_json if isinstance(campaign.metadata_json, dict) else {}
                )
                run = session.scalar(
                    select(Run)
                    .where(Run.campaign_id == campaign.id)
                    .order_by(desc(Run.updated_at))
                    .limit(1)
                )
                workflow_id = metadata.get("workflow_id")
                if not isinstance(workflow_id, str) or not workflow_id:
                    workflow_id = run.workflow_id if run is not None else None
                values.append(
                    {
                        "campaign_id": str(campaign.id),
                        "project": campaign.project,
                        "name": campaign.name,
                        "status": campaign.status,
                        "workflow_id": workflow_id,
                        "run_type": run.run_type if run is not None else None,
                        "run_status": run.status if run is not None else None,
                        "blocker": metadata.get("blocker"),
                        "age_seconds": max(
                            0,
                            int((now - _utc(campaign.updated_at)).total_seconds()),
                        ),
                        "updated_at": campaign.updated_at.isoformat(),
                    }
                )
            return values

    async def campaign_detail(self, campaign_id: UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._campaign_detail, campaign_id)

    def _campaign_detail(self, campaign_id: UUID) -> dict[str, Any] | None:
        with self.sessions() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                return None
            metadata = campaign.metadata_json if isinstance(campaign.metadata_json, dict) else {}
            return {
                "campaign_id": str(campaign.id),
                "project": campaign.project,
                "name": campaign.name,
                "status": campaign.status,
                "workflow_id": metadata.get("workflow_id"),
                "run_id": metadata.get("temporal_run_id"),
                "plan_hash": metadata.get("plan_hash"),
                "current_issue": metadata.get("current_issue"),
                "completed_count": len(metadata.get("completed_issues", [])),
                "blocked_count": len(metadata.get("blocked_issues", [])),
                "skipped_count": len(metadata.get("skipped_issues", [])),
                "updated_at": campaign.updated_at.isoformat(),
            }

    async def workflow_tail(self, workflow_id: str, limit: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._workflow_tail, workflow_id, limit)

    def _workflow_tail(self, workflow_id: str, limit: int) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("workflow tail limit is invalid")
        with self.sessions() as session:
            rows = session.scalars(
                select(DashboardEvent)
                .where(DashboardEvent.workflow_id == workflow_id)
                .order_by(desc(DashboardEvent.occurred_at), desc(DashboardEvent.id))
                .limit(limit)
            ).all()
            return [
                {
                    "occurred_at": row.occurred_at.isoformat(),
                    "level": row.level,
                    "event_type": row.event_type,
                    "phase": row.current_phase,
                    "message": row.message,
                }
                for row in reversed(rows)
            ]

    async def reconcile_terminal_campaign(
        self, campaign_id: UUID, workflow_id: str, temporal_status: str
    ) -> bool:
        return await asyncio.to_thread(
            self._reconcile_terminal_campaign, campaign_id, workflow_id, temporal_status
        )

    def _reconcile_terminal_campaign(
        self, campaign_id: UUID, workflow_id: str, temporal_status: str
    ) -> bool:
        terminal = {
            "COMPLETED": "COMPLETE",
            "FAILED": "FAILED",
            "CANCELED": "CANCELLED",
            "TERMINATED": "FAILED",
            "TIMED_OUT": "FAILED",
        }.get(temporal_status)
        if terminal is None:
            return False
        with self.sessions.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                return False
            metadata = campaign.metadata_json if isinstance(campaign.metadata_json, dict) else {}
            run = session.scalar(
                select(Run)
                .where(Run.campaign_id == campaign_id, Run.workflow_id == workflow_id)
                .limit(1)
            )
            bound_workflow = metadata.get("workflow_id") or (run.workflow_id if run else None)
            if bound_workflow != workflow_id:
                raise ValueError("campaign workflow binding changed during reconciliation")
            changed = campaign.status != terminal
            campaign.status = terminal
            if run is not None:
                changed = changed or run.status != terminal
                run.status = terminal
                run.finished_at = run.finished_at or datetime.now(UTC)
            return changed
