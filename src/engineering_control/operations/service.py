from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any
from uuid import UUID

import structlog

from engineering_control.api.schemas import (
    AuditPlanRequest,
    AutonomousBacklogStart,
    BacklogApprovalCandidateRequest,
    CampaignStart,
    EnhancementDiscover,
    EnhancementRun,
    ExistingBacklogLoopStart,
    LLMWikiBootstrapPlanRequest,
    MultiProjectAuditStart,
    OperationAccepted,
)
from engineering_control.api.service import ControlService
from engineering_control.db.repository import MetadataRepository
from engineering_control.operations.connections import ConnectionHealthProjector
from engineering_control.operations.issue_start import IssueStartUnconfirmedError, issue_workflow_id
from engineering_control.operations.models import (
    AdvancedAuditCommand,
    AuditAllProjectsCommand,
    AuditPlanCommand,
    AuditStartCommand,
    AutonomousBacklogCommand,
    Command,
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
)
from engineering_control.operations.repository import OperationsRepository

logger = structlog.get_logger()


class TypedCommandBus:
    """One allowlisted application command path for dashboard, Telegram, and CLI."""

    def __init__(
        self,
        control: ControlService,
        metadata: MetadataRepository,
        operations: OperationsRepository,
    ) -> None:
        self.control = control
        self.metadata = metadata
        self.operations = operations
        self.connections = ConnectionHealthProjector(operations)
        self._next_issue_recovery_at = 0.0
        self._issue_recovery_cursor: UUID | None = None

    async def submit(
        self,
        command: Command,
        *,
        source_channel: SourceChannel,
        operator_class: str,
        idempotency_key: str,
    ) -> OperationView:
        operation = await self.operations.create_external(
            command,
            source_channel=source_channel,
            operator_class=operator_class,
            idempotency_key=idempotency_key,
        )
        if operation.status == "PENDING":
            claim = await self.operations.claim(operation.operation_id)
            if claim is not None:
                await self.dispatch_claim(claim)
        refreshed = await self.operations.operation(operation.operation_id)
        if refreshed is None:
            raise RuntimeError("operation disappeared after dispatch")
        return refreshed

    async def dispatch_pending(self) -> bool:
        if time.monotonic() >= self._next_issue_recovery_at:
            self._next_issue_recovery_at = time.monotonic() + 5.0
            await self.recover_issue_start_once()
        claim = await self.operations.claim_pending()
        if claim is None:
            return False
        await self.dispatch_claim(claim)
        return True

    async def recover_issue_start_once(self) -> None:
        candidate = await self.operations.next_issue_start_recovery(self._issue_recovery_cursor)
        if candidate is None:
            self._issue_recovery_cursor = None
            return
        operation_id, request = candidate
        self._issue_recovery_cursor = operation_id
        if request is None:
            logger.error("issue_start_recovery_invalid_binding", operation_id=str(operation_id))
            return
        try:
            accepted = await self.control.observe_issue_start(request)
        except Exception:  # noqa: BLE001 - observation failures never release or restart ownership
            await self.operations.mark_issue_start_unconfirmed(
                operation_id, issue_workflow_id(request)
            )
            return
        await self.operations.record_issue_start_recovery(request, accepted)

    async def dispatch_claim(self, claim: tuple[UUID, Command, SourceChannel, str]) -> None:
        operation_id, command, _source_channel, operator_class = claim
        try:
            capability = await self.operations.capability(
                command, exclude_operation_id=operation_id
            )
            if capability.status != "AVAILABLE":
                blocker = capability.blockers[0] if capability.blockers else capability.status
                raise ValueError(f"operation preflight changed: {blocker}")
            if isinstance(command, SolveIssueCommand):
                await self.operations.validate_issue_approval(operation_id, command)
            accepted = await self._execute(
                command,
                operator_class=operator_class,
                operation_id=operation_id,
            )
        except IssueStartUnconfirmedError as error:
            await self.operations.mark_issue_start_unconfirmed(operation_id, error.workflow_id)
        except ValueError as error:
            await self.operations.finish(operation_id, status="BLOCKED", safe_error=str(error))
        except Exception:  # noqa: BLE001 - safe error boundary must not expose provider details
            await self.operations.finish(
                operation_id,
                status="FAILED",
                safe_error="typed application operation failed",
            )
        else:
            if accepted is None:
                await self.operations.finish(operation_id, status="COMPLETED")
            else:
                await self.operations.finish(
                    operation_id,
                    status="STARTED",
                    workflow_id=accepted.workflow_id,
                    run_id=accepted.run_id,
                    linked=accepted.status == "EXISTING",
                )

    async def _execute(
        self,
        command: Command,
        *,
        operator_class: str,
        operation_id: UUID,
    ) -> OperationAccepted | None:
        if isinstance(command, AutonomousBacklogCommand):
            return await self.control.start_autonomous_backlog(
                AutonomousBacklogStart.model_validate(command.model_dump(mode="json"))
            )
        if isinstance(command, SolveIssueCommand):
            operation = await self.operations.operation(operation_id)
            if operation is None:
                raise ValueError("issue operation disappeared before durable handoff")
            request = ExistingBacklogLoopStart(
                project=command.project.value,
                backlog_campaign_id=command.backlog_campaign_id,
                maximum_risk=command.maximum_risk,
                delivery_mode=command.delivery_mode,
                maximum_items=1,
                target_repository=command.repository,
                target_issue_number=command.issue_number,
                continue_after_blocked_issue=False,
                correction_limit=command.correction_limit,
                issue_operation_id=operation_id,
                issue_approval_binding=operation.binding_hash,
            )
            candidate = await self.metadata.prepare_backlog_approval(
                command.backlog_campaign_id,
                BacklogApprovalCandidateRequest(
                    project=command.project.value,
                    delivery_mode=command.delivery_mode,
                    maximum_risk=command.maximum_risk,
                    maximum_items=1,
                ),
                issue_request=request,
            )
            await self.metadata.approve_backlog_campaign(
                command.backlog_campaign_id,
                command.project.value,
                str(candidate["approval_binding"]),
                self._approval_actor(operator_class),
            )
            return await self.control.start_existing_backlog_loop(request)
        if isinstance(command, SolveBacklogCommand):
            candidate = await self.metadata.prepare_backlog_approval(
                command.backlog_campaign_id,
                BacklogApprovalCandidateRequest(
                    project=command.project,
                    delivery_mode=command.delivery_mode,
                    maximum_risk=command.maximum_risk,
                    maximum_items=command.maximum_items,
                ),
            )
            await self.metadata.approve_backlog_campaign(
                command.backlog_campaign_id,
                command.project,
                str(candidate["approval_binding"]),
                self._approval_actor(operator_class),
            )
            return await self.control.start_existing_backlog_loop(
                ExistingBacklogLoopStart(
                    project=command.project,
                    backlog_campaign_id=command.backlog_campaign_id,
                    maximum_risk=command.maximum_risk,
                    delivery_mode=command.delivery_mode,
                    maximum_items=command.maximum_items,
                    continue_after_blocked_issue=command.continue_after_blocked_issue,
                    correction_limit=command.correction_limit,
                )
            )
        if isinstance(command, AuditPlanCommand):
            await self.control.plan_campaign(
                AuditPlanRequest(
                    project=command.project,
                    preset=self._audit_preset(command.preset),
                    environment=command.environment,
                )
            )
            return None
        if isinstance(command, AuditStartCommand):
            return await self.control.start_campaign(
                CampaignStart(
                    project=command.project,
                    name=f"{command.preset}-{command.environment}",
                    preset=self._audit_preset(command.preset),
                    environment=command.environment,
                    dry_run=False,
                    publish_findings=command.publish_findings,
                )
            )
        if isinstance(command, AuditAllProjectsCommand):
            if command.mode == "plan":
                for project in ("falcone", "musematic", "llmwiki"):
                    result = await self.control.plan_campaign(
                        AuditPlanRequest(
                            project=project,  # type: ignore[arg-type]
                            preset=self._audit_preset(command.preset),
                            environment=command.environment,
                        )
                    )
                    if result.get("blockers"):
                        raise ValueError(f"ALL_PROJECT_AUDIT_PLAN_BLOCKED_{project.upper()}")
                return None
            return await self.control.start_all_project_audits(
                MultiProjectAuditStart(
                    preset=self._audit_preset(command.preset),
                    environment=command.environment,
                    publish_findings=command.publish_findings,
                    production=False,
                )
            )
        if isinstance(command, AdvancedAuditCommand):
            return await self.control.start_campaign(
                CampaignStart(
                    project=command.project,
                    name=command.name,
                    source_sha=command.source_sha,
                    deployment_sha=command.deployment_sha,
                    domains=command.domains,
                    personas=command.personas,
                    lenses=command.lenses,
                    environments=command.environments,
                    dry_run=not command.execute,
                    publish_findings=False,
                )
            )
        if isinstance(command, WorkflowControlCommand) and command.action in {
            "pause",
            "pause_after_current",
            "resume",
            "stop_after_current",
            "cancel",
        }:
            await self.control.signal(command.workflow_id, command.action)
            return OperationAccepted(workflow_id=command.workflow_id, run_id="")
        if isinstance(command, WorkflowControlCommand) and command.action in {
            "nudge",
            "retry",
        }:
            issue = (
                f"gntik-ai/{command.repository}#{command.issue_number}"
                if command.repository and command.issue_number
                else "operator-nudge"
            )
            await self.control.signal(command.workflow_id, "retry", issue)
            return OperationAccepted(workflow_id=command.workflow_id, run_id="")
        if isinstance(command, WorkflowControlCommand) and command.action == "skip":
            if not command.repository or not command.issue_number or not command.reason_code:
                raise ValueError("skip requires an exact issue and reason")
            await self.control.signal(
                command.workflow_id,
                "skip",
                f"gntik-ai/{command.repository}#{command.issue_number}|{command.reason_code}",
            )
            return OperationAccepted(workflow_id=command.workflow_id, run_id="")
        if isinstance(command, RefreshConnectionsCommand):
            await self.connections.refresh(command.scope)
            return None
        if isinstance(command, EnhancementDiscoverCommand):
            return await self.control.start_enhancement_discovery(
                EnhancementDiscover(
                    project=command.project,
                    campaign_id=command.campaign_id,
                    dry_run=command.dry_run,
                )
            )
        if isinstance(command, EnhancementApproveCommand):
            enhancement = await self.metadata.get_enhancement(command.enhancement_id)
            binding = self._enhancement_binding(enhancement, command.approval_binding)
            await self.metadata.approve_enhancement(
                command.enhancement_id,
                binding,
                self._approval_actor(operator_class),
            )
            return None
        if isinstance(command, EnhancementRejectCommand):
            enhancement = await self.metadata.get_enhancement(command.enhancement_id)
            binding = self._enhancement_binding(enhancement, command.approval_binding)
            await self.metadata.reject_enhancement(
                command.enhancement_id,
                command.reason_code,
                self._approval_actor(operator_class),
                expected_binding=binding,
            )
            return None
        if isinstance(command, EnhancementRunCommand):
            enhancement = await self.metadata.get_enhancement(command.enhancement_id)
            binding = self._enhancement_binding(enhancement, command.approval_binding)
            return await self.control.start_enhancement_solution(
                command.enhancement_id,
                EnhancementRun(
                    approval_binding=binding,
                    risk=command.risk,
                    execution_mode=command.execution_mode,
                    dry_run=command.dry_run,
                ),
            )
        if isinstance(command, FindingVerifyCommand):
            return await self.control.start_finding_verification(command.finding_id)
        if isinstance(command, LLMWikiBootstrapPlanCommand):
            return await self.control.start_llmwiki_bootstrap_plan(LLMWikiBootstrapPlanRequest())
        if isinstance(command, (EvidenceExportCampaignCommand, EvidenceExportWorkflowCommand)):
            await self.operations.create_export(operation_id, command)
            return None
        raise ValueError("typed command is not registered")

    @staticmethod
    def _approval_actor(operator_class: str) -> str:
        if operator_class == "password-reauthenticated-operator":
            return "dashboard:reauthenticated:hermes"
        if operator_class.startswith("telegram:"):
            return operator_class
        if operator_class == "engineering-ctl:hermes":
            return "cli:engineering-ctl:hermes"
        raise ValueError("operation requires an allowlisted operator channel")

    @staticmethod
    def _enhancement_binding(
        enhancement: dict[str, Any] | None, expected_binding: str | None
    ) -> str:
        if enhancement is None:
            raise ValueError("enhancement not found")
        binding = enhancement.get("approval_binding_sha256")
        if (
            not isinstance(binding, str)
            or len(binding) != 64
            or any(character not in "0123456789abcdef" for character in binding)
        ):
            raise ValueError("enhancement approval binding is unavailable")
        if expected_binding is None or not secrets.compare_digest(binding, expected_binding):
            raise ValueError("enhancement approval binding changed")
        return binding

    @staticmethod
    def _audit_preset(value: str) -> Any:
        return "static-full" if value == "documentation" else value


async def operation_dispatch_loop(bus: TypedCommandBus, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            worked = await bus.dispatch_pending()
        except Exception as error:  # noqa: BLE001 - keep dispatcher alive without hiding cancellation
            logger.error("operation_dispatch_iteration_failed", error_type=type(error).__name__)
            worked = False
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except TimeoutError:
                pass
