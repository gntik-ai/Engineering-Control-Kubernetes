from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import create_engine, desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.db.models import (
    ActionCandidate,
    BacklogIssueRecord,
    ConnectionSnapshot,
    DashboardEvent,
    Enhancement,
    GitHubBacklogCampaign,
    NotificationOutbox,
    OperationRequest,
    ProjectSnapshot,
    QuotaSnapshot,
    ReauthenticationBinding,
    Run,
    SolutionLoop,
    WorkerHeartbeat,
)
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.operations.models import (
    AdvancedAuditCommand,
    AuditPlanCommand,
    AuditStartCommand,
    CandidateView,
    CapabilityResult,
    Command,
    ConnectionView,
    EnhancementApproveCommand,
    EnhancementDiscoverCommand,
    EnhancementRejectCommand,
    EnhancementRunCommand,
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

_ACTIVE_OPERATION_STATES = frozenset({"PENDING", "DISPATCHING", "STARTED", "RUNNING"})
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


def command_target(command: Command) -> tuple[str, str, str | None]:
    if isinstance(command, SolveIssueCommand):
        return "github_issue", f"{command.repository}#{command.issue_number}", command.project.value
    if isinstance(command, SolveBacklogCommand):
        return "backlog_campaign", str(command.backlog_campaign_id), command.project
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
    raise ValueError("command target is not registered")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class OperationsRepository:
    """Durable operation store. It never reads service or provider credentials."""

    def __init__(self, database_url: str) -> None:
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.redactor = SecretRedactor()

    async def candidate(self, command: Command, session_binding_hash: str) -> CandidateView:
        return await asyncio.to_thread(self._candidate, command, session_binding_hash)

    def _candidate(self, command: Command, session_binding_hash: str) -> CandidateView:
        if len(session_binding_hash) != 64:
            raise ValueError("session binding is invalid")
        target_type, target_id, project = command_target(command)
        now = datetime.now(UTC)
        payload = command.model_dump(mode="json")
        with self.sessions.begin() as session:
            capability, binding = self._capability(session, command, now)
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

        if isinstance(command, SolveIssueCommand):
            if command.maximum_risk.value == "R3":
                blockers.append("R3_OPERATOR_ACTION_REQUIRED")
            if command.delivery_mode == "full-delivery":
                blockers.append("FULL_DELIVERY_FINAL_CLOSURE_SERVICE_NOT_IMPLEMENTED")
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
                active = self._active_operation(
                    session,
                    "solve_issue",
                    f"{command.repository}#{command.issue_number}",
                    exclude_operation_id=exclude_operation_id,
                )
                if active is not None:
                    blockers.append("ISSUE_WORKFLOW_ALREADY_ACTIVE")
                    binding.update(
                        {
                            "existing_operation_id": str(active.id),
                            "existing_workflow_id": active.workflow_id,
                        }
                    )
            required_credentials.extend(["github-broker", "native-codex", "native-claude"])
            blockers.extend(self._provider_blockers(session))
        elif isinstance(command, SolveBacklogCommand):
            if command.maximum_risk.value == "R3":
                blockers.append("R3_OPERATOR_ACTION_REQUIRED")
            if command.delivery_mode == "full-delivery":
                blockers.append("FULL_DELIVERY_FINAL_CLOSURE_SERVICE_NOT_IMPLEMENTED")
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
                active = self._active_operation(
                    session,
                    "solve_backlog",
                    str(campaign.id),
                    exclude_operation_id=exclude_operation_id,
                )
                if active is not None:
                    blockers.append("BACKLOG_CAMPAIGN_ALREADY_ACTIVE")
                    binding.update(
                        {
                            "existing_operation_id": str(active.id),
                            "existing_workflow_id": active.workflow_id,
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
                    active = self._active_operation(
                        session,
                        command.action,
                        command.enhancement_id,
                        exclude_operation_id=exclude_operation_id,
                    )
                    if active is not None:
                        blockers.append("ENHANCEMENT_WORKFLOW_ALREADY_ACTIVE")
                        binding.update(
                            {
                                "existing_operation_id": str(active.id),
                                "existing_workflow_id": active.workflow_id,
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
            age = (datetime.now(UTC) - _utc(snapshot.captured_at)).total_seconds()
            if age > 300:
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
        for operation in session.scalars(statement):
            if operation.status == "STARTED" and operation.workflow_id:
                workflow_state = self._workflow_state(session, operation.workflow_id)
                if (
                    workflow_state is not None
                    and workflow_state.upper() in _TERMINAL_WORKFLOW_STATES
                ):
                    operation.status = "COMPLETED"
                    operation.completed_at = operation.completed_at or datetime.now(UTC)
                    continue
            return operation
        return None

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
            return self._operation_view(row) if row else None

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
                    workflow_id=workflow_id,
                    temporal_run_id=operation.temporal_run_id,
                    issue_number=(
                        int(operation.target_id.rsplit("#", 1)[1])
                        if operation.target_type == "github_issue"
                        and operation.target_id.rsplit("#", 1)[-1].isdigit()
                        else None
                    ),
                    campaign_id=(
                        operation.target_id
                        if operation.target_type in {"backlog_campaign", "audit"}
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
            binding_hash = canonical_sha256(payload)
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
    def _operation_view(row: OperationRequest) -> OperationView:
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
