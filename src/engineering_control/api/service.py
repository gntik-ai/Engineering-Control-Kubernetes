from __future__ import annotations

import asyncio
import hmac
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import QueryRejectCondition, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from engineering_control.api.schemas import (
    AcceptanceLifecycleStart,
    ApprovalRecord,
    AuditPlanRequest,
    AutonomousBacklogStart,
    BacklogImportAccepted,
    CampaignStart,
    DeliveryReconcile,
    DeliveryStart,
    DeploymentPrepare,
    EnhancementDiscover,
    EnhancementRun,
    ExistingBacklogLoopStart,
    GitHubBacklogImportRequest,
    IssueLoopStart,
    LLMWikiBootstrapExecuteRequest,
    LLMWikiBootstrapPlanRequest,
    MultiProjectAuditStart,
    ObservabilityAcceptanceStart,
    OperationAccepted,
    RollbackRequest,
    RuntimeVerificationStart,
)
from engineering_control.db.repository import MetadataRepository
from engineering_control.engineering.change_requests import change_request_workflow_id
from engineering_control.engineering.models import CampaignPlan
from engineering_control.engineering.ui_blueprint import (
    ui_plan_workflow_id,
)
from engineering_control.engineering.ui_gap_consolidation import consolidation_workflow_id
from engineering_control.models.enums import Project
from engineering_control.operations.issue_start import IssueStartUnconfirmedError, issue_workflow_id
from engineering_control.operations.run_start import RecordedRunStartUnconfirmedError
from engineering_control.policy.approvals import ApprovalService
from engineering_control.projects import GLOBAL_PROJECTS
from engineering_control.workflow_outcome import derive_workflow_application_projection
from engineering_control.workflows.inputs import (
    AcceptanceLifecycleInput,
    AuditCampaignInput,
    AutonomousBacklogCampaignInput,
    ChangeRequestInput,
    DeliveryReconciliationInput,
    EnhancementDiscoveryInput,
    EnhancementSolutionLoopInput,
    ExistingGitHubBacklogLoopInput,
    FindingVerificationInput,
    GitHubBacklogImportInput,
    IssueAmendObservationInput,
    IssueDeliveryInput,
    IssueSolutionLoopInput,
    LLMWikiStagingBootstrapInput,
    MultiProjectAuditInput,
    ObservabilityAcceptanceInput,
    PreviewDeploymentInput,
    PublicationHeadsObservationInput,
    RecoveryInput,
    RuntimeVerificationInput,
    StagingDeploymentInput,
    UiBlueprintFanOutInput,
    UiGapConsolidationPlanInput,
    UiMigrationPlanInput,
)
from engineering_control.workflows.names import WorkflowName


class ControlService(Protocol):
    async def plan_campaign(self, request: AuditPlanRequest) -> dict[str, Any]: ...
    async def start_campaign(self, request: CampaignStart) -> OperationAccepted: ...
    async def start_all_project_audits(
        self, request: MultiProjectAuditStart
    ) -> OperationAccepted: ...
    async def start_delivery(self, request: DeliveryStart) -> OperationAccepted: ...
    async def start_finding_verification(self, finding_id: UUID) -> OperationAccepted: ...
    async def reconcile_delivery(self, request: DeliveryReconcile) -> OperationAccepted: ...
    async def signal(self, workflow_id: str, signal: str, argument: Any = None) -> None: ...
    async def current_open_run_id(self, workflow_id: str) -> str | None: ...

    async def publisher_settlement(self, subject: str) -> dict[str, Any] | None: ...
    async def awaited_operator_issue(self, workflow_id: str, run_id: str) -> str | None: ...

    async def validate_backlog_issue_signal(self, workflow_id: str, issue_ref: str) -> None: ...
    async def observe_issue_body(self, repository: str, issue_number: int) -> dict[str, Any]: ...
    async def observe_pull_request_heads(
        self, urls: list[str], branch_listings: list[dict[str, str]] | None = None
    ) -> dict[str, Any]: ...
    async def prepare_deployment(self, request: DeploymentPrepare) -> OperationAccepted: ...
    async def rollback(self, request: RollbackRequest) -> OperationAccepted: ...
    async def start_runtime_verification(
        self, request: RuntimeVerificationStart
    ) -> OperationAccepted: ...
    async def get_workflow(self, workflow_id: str) -> dict[str, Any]: ...
    async def workflow_run_status(self, workflow_id: str, run_id: str) -> str: ...
    async def workflow_id_closed(self, workflow_id: str) -> bool: ...
    async def get_reconciliation_observation(self, workflow_id: str) -> dict[str, Any]: ...
    async def record_approval(self, request: ApprovalRecord, approver_subject: str) -> None: ...
    async def start_acceptance(
        self, request: AcceptanceLifecycleStart, validation_root: str
    ) -> OperationAccepted: ...
    async def start_observability_acceptance(
        self, request: ObservabilityAcceptanceStart
    ) -> OperationAccepted: ...
    async def start_reverse_observability_acceptance(
        self, request: ObservabilityAcceptanceStart
    ) -> OperationAccepted: ...
    async def start_issue_loop(self, request: IssueLoopStart) -> OperationAccepted: ...
    async def import_github_backlog(
        self, request: GitHubBacklogImportRequest
    ) -> BacklogImportAccepted: ...
    async def start_existing_backlog_loop(
        self, request: ExistingBacklogLoopStart
    ) -> OperationAccepted: ...
    async def observe_issue_start(self, request: ExistingBacklogLoopStart) -> OperationAccepted: ...
    async def start_autonomous_backlog(
        self, request: AutonomousBacklogStart
    ) -> OperationAccepted: ...
    async def start_enhancement_discovery(
        self, request: EnhancementDiscover
    ) -> OperationAccepted: ...
    async def start_enhancement_solution(
        self, enhancement_id: str, request: EnhancementRun
    ) -> OperationAccepted: ...
    async def start_llmwiki_bootstrap_plan(
        self, request: LLMWikiBootstrapPlanRequest
    ) -> OperationAccepted: ...
    async def start_llmwiki_bootstrap(
        self, request: LLMWikiBootstrapExecuteRequest
    ) -> OperationAccepted: ...
    async def start_change_request(
        self, change_request_id: UUID, revision: int
    ) -> OperationAccepted: ...
    async def start_ui_plan(
        self, blueprint_id: UUID, project: str, requested_by: str, source_sha: str | None
    ) -> OperationAccepted: ...
    async def start_ui_fanout(
        self, blueprint_id: UUID, max_items: int, workflow_id: str
    ) -> OperationAccepted: ...
    async def start_ui_gap_consolidation(
        self, consolidation_id: UUID, workflow_id: str
    ) -> OperationAccepted: ...
    async def ui_fanout_state(self, workflow_id: str) -> tuple[str, OperationAccepted | None]: ...


class TemporalControlService:
    def __init__(
        self,
        client: Client,
        task_queue: str,
        repository: MetadataRepository,
        *,
        acceptance_activity_task_queue: str = "engineering-acceptance",
    ) -> None:
        self.client = client
        self.task_queue = task_queue
        self.repository = repository
        self.acceptance_activity_task_queue = acceptance_activity_task_queue

    async def plan_campaign(self, request: AuditPlanRequest) -> dict[str, Any]:
        value = await self.client.execute_workflow(
            WorkflowName.AUDIT_CAMPAIGN.value,
            AuditCampaignInput(
                project=request.project.value,
                name=f"{request.preset}-{request.environment}-plan",
                preset=request.preset,
                environment=request.environment,
                dry_run=True,
                plan_only=True,
            ),
            id=f"audit-plan-{request.project.value}-{uuid4()}",
            task_queue=self.task_queue,
        )
        if not isinstance(value, dict):
            raise RuntimeError("audit plan workflow returned an invalid result")
        fields = CampaignPlan.model_fields
        return CampaignPlan.model_validate({field: value[field] for field in fields}).model_dump(
            mode="json"
        )

    async def observe_issue_body(self, repository: str, issue_number: int) -> dict[str, Any]:
        """NEW-115: read one issue's current body through the GitHub worker (read-only)."""
        value = await self.client.execute_workflow(
            WorkflowName.ISSUE_AMEND_OBSERVATION.value,
            IssueAmendObservationInput(issue_repository=repository, issue_number=issue_number),
            id=(
                f"issue-amend-observe-{repository.removeprefix('gntik-ai/')}-"
                f"{issue_number}-{uuid4()}"
            ),
            task_queue=self.task_queue,
            execution_timeout=timedelta(seconds=90),
        )
        if not isinstance(value, dict):
            raise RuntimeError("issue body observation returned an invalid result")
        return value

    async def observe_pull_request_heads(
        self, urls: list[str], branch_listings: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        """NEW-145: live head and state of each pull request, read by the GitHub worker;
        NEW-145b: with ``branch_listings``, also every PR whose head is each branch."""
        value = await self.client.execute_workflow(
            WorkflowName.PUBLICATION_HEADS_OBSERVATION.value,
            PublicationHeadsObservationInput(
                pull_request_urls=list(urls), branch_listings=branch_listings
            ),
            id=f"publication-heads-observe-{uuid4()}",
            task_queue=self.task_queue,
            execution_timeout=timedelta(seconds=90),
        )
        heads = value.get("heads") if isinstance(value, dict) else None
        listings = value.get("listings", []) if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("status") != "OK"
            or not isinstance(heads, list)
            or not isinstance(listings, list)
            or (branch_listings is not None and len(listings) != len(branch_listings))
        ):
            raise RuntimeError("pull request head observation returned an invalid result")
        return {
            "heads": [item for item in heads if isinstance(item, dict)],
            "listings": [item for item in listings if isinstance(item, dict)],
        }

    async def _start_recorded_workflow(
        self,
        application_run_id: UUID,
        workflow_id: str,
        workflow_name: str,
        workflow_input: Any,
        *,
        campaign_id: UUID | None = None,
    ) -> OperationAccepted:
        try:
            async with asyncio.timeout(15):
                handle = await self.client.start_workflow(
                    workflow_name,
                    workflow_input,
                    id=workflow_id,
                    task_queue=self.task_queue,
                    rpc_timeout=timedelta(seconds=5),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    memo={
                        "engineering_application_run_id": str(application_run_id),
                        "engineering_binding_version": 1,
                    },
                )
                first_run_id = handle.first_execution_run_id
                try:
                    parsed = UUID(first_run_id)
                except (TypeError, ValueError):
                    raise ValueError("invalid first_execution_run_id") from None
                if str(parsed) != first_run_id or parsed.int == 0:
                    raise ValueError("invalid first_execution_run_id")
                await self.repository.bind_recorded_run(
                    application_run_id, workflow_id, first_run_id
                )
                if campaign_id is not None:
                    await self.repository.bind_campaign_execution(
                        campaign_id, workflow_id, first_run_id
                    )
        except Exception:
            raise RecordedRunStartUnconfirmedError(workflow_id, application_run_id) from None
        return OperationAccepted(workflow_id=workflow_id, run_id=first_run_id)

    async def start_campaign(self, request: CampaignStart) -> OperationAccepted:
        campaign_id = uuid4()
        workflow_id = f"audit-{request.project.value}-{campaign_id}"
        application_run_id = uuid4()
        await self.repository.create_campaign(
            campaign_id, workflow_id, request, application_run_id=application_run_id
        )
        value = request.model_dump(mode="json")
        value["preset"] = request.preset or "static-full"
        value["environment"] = request.environment or request.environments[0]
        return await self._start_recorded_workflow(
            application_run_id,
            workflow_id,
            WorkflowName.AUDIT_CAMPAIGN.value,
            AuditCampaignInput(**value, campaign_id=str(campaign_id)),
            campaign_id=campaign_id,
        )

    async def start_all_project_audits(self, request: MultiProjectAuditStart) -> OperationAccepted:
        group_id = uuid4()
        workflow_id = f"audit-all-{group_id}"
        projects = tuple(Project(project) for project in GLOBAL_PROJECTS)
        campaigns = {
            project: (
                uuid4(),
                f"{workflow_id}-{project.value}",
            )
            for project in projects
        }
        application_run_id = uuid4()
        await self.repository.create_multi_project_audit(
            workflow_id, campaigns, request, application_run_id=application_run_id
        )
        return await self._start_recorded_workflow(
            application_run_id,
            workflow_id,
            WorkflowName.MULTI_PROJECT_AUDIT.value,
            MultiProjectAuditInput(
                preset=request.preset,
                environment=request.environment,
                publish_findings=request.publish_findings,
                campaign_ids={project.value: str(campaigns[project][0]) for project in projects},
                child_workflow_ids={project.value: campaigns[project][1] for project in projects},
                application_run_id=str(application_run_id),
            ),
        )

    async def start_delivery(self, request: DeliveryStart) -> OperationAccepted:
        # Also validate internally constructed/model_copy callers. A real direct
        # entrypoint adopts the approved single-issue operation, not a second
        # unbound child Run with its own random workflow ID.
        request = DeliveryStart.model_validate(request.model_dump(mode="json"))
        if not request.dry_run:
            await self.repository.validate_delivery_issue_request(request)
            assert request.approved_issue is not None
            accepted = await self.start_existing_backlog_loop(request.approved_issue)
            try:
                await self.repository.record_delivery_issue_start(request, accepted)
            except Exception as error:
                # Temporal already accepted or identified the canonical execution.
                # A missing receipt is not a pre-start denial, even if approval
                # changed during persistence. Preserve its owner for observation;
                # do not retry the start or try another write to an unavailable DB.
                raise IssueStartUnconfirmedError(request.approved_issue) from error
            return accepted
        workflow_id = f"delivery-{request.finding_id}-{uuid4()}"
        application_run_id = uuid4()
        await self.repository.create_delivery_run(
            workflow_id, request, application_run_id=application_run_id
        )
        return await self._start_recorded_workflow(
            application_run_id,
            workflow_id,
            WorkflowName.ISSUE_DELIVERY.value,
            IssueDeliveryInput(
                finding_id=str(request.finding_id),
                risk_class=request.risk_class.value,
                dry_run=True,
            ).payload(),
        )

    async def start_finding_verification(self, finding_id: UUID) -> OperationAccepted:
        workflow_id = f"finding-verify-{finding_id}-{uuid4()}"
        application_run_id = uuid4()
        project = await self.repository.create_finding_verification_run(
            workflow_id, finding_id, application_run_id=application_run_id
        )
        return await self._start_recorded_workflow(
            application_run_id,
            workflow_id,
            WorkflowName.FINDING_VERIFICATION.value,
            FindingVerificationInput(
                finding_id=str(finding_id),
                project=project.value,
                fresh_snapshot=True,
            ),
        )

    async def reconcile_delivery(self, request: DeliveryReconcile) -> OperationAccepted:
        workflow_id = f"reconcile-{request.change_set_id}-{uuid4()}"
        application_run_id = uuid4()
        await self.repository.create_delivery_reconciliation_run(
            workflow_id, request, application_run_id=application_run_id
        )
        return await self._start_recorded_workflow(
            application_run_id,
            workflow_id,
            WorkflowName.DELIVERY_RECONCILIATION.value,
            DeliveryReconciliationInput(**request.model_dump(mode="json")),
        )

    async def signal(self, workflow_id: str, signal: str, argument: Any = None) -> None:
        # NEW-30: pause_after_current/stop_after_current are real workflow signals the
        # command bus forwards, but were missing here, so both were always refused.
        allowed = {
            "pause",
            "pause_after_current",
            "resume",
            "stop_after_current",
            "cancel",
            "approval",
            "skip",
            "retry",
        }
        if signal not in allowed:
            raise ValueError("unsupported control signal")
        handle = self.client.get_workflow_handle(workflow_id)
        await handle.signal(signal, argument)

    # NEW-145 review 2: publishers whose systemd service runs on this (controller) host.
    _PUBLISHER_UNITS = {"github-runner": "engineering-github-worker.service"}

    async def publisher_settlement(self, subject: str) -> dict[str, Any] | None:
        """The publishing service's current start time and KillMode, read from systemd on
        this host; None when the publisher is not a local unit or systemd cannot be read."""
        unit = self._PUBLISHER_UNITS.get(subject)
        if unit is None:
            return None
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [
                    "systemctl",
                    "show",
                    unit,
                    "--property=ExecMainStartTimestamp,KillMode,ActiveState",
                    "--timestamp=unix",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        values = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
        stamp = values.get("ExecMainStartTimestamp", "")
        if values.get("ActiveState") != "active" or not stamp.startswith("@"):
            return None
        try:
            started = datetime.fromtimestamp(int(stamp[1:]), UTC)
        except ValueError:
            return None
        return {
            "unit": unit,
            "started_at": started.isoformat(),
            "kill_mode": values.get("KillMode", ""),
        }

    async def current_open_run_id(self, workflow_id: str) -> str | None:
        """NEW-133 review 3: the run id of the workflow's current (latest) execution if it is
        still open, else None (also when Temporal cannot be read, so callers fail closed)."""
        try:
            described = await self.client.get_workflow_handle(workflow_id).describe()
        except Exception:  # noqa: BLE001 - unreadable means no proof of the current run
            return None
        if described.status != WorkflowExecutionStatus.RUNNING:
            return None
        return str(described.run_id) if described.run_id else None

    async def awaited_operator_issue(self, workflow_id: str, run_id: str) -> str | None:
        """NEW-143: the issue the open loop run waits on (AWAITING_OPERATOR:<issue>:...),
        read from that exact run's status query, which Temporal refuses once the run is
        closed (a closed run's last phase is no proof); None when it waits on nothing or
        cannot be read."""
        try:
            handle = self.client.get_workflow_handle(workflow_id, run_id=run_id)
            state = await handle.query("status", reject_condition=QueryRejectCondition.NOT_OPEN)
        except Exception:  # noqa: BLE001 - unreadable/closed means no proof (fail closed)
            return None
        phase = str(state.get("phase", "")) if isinstance(state, dict) else ""
        if not phase.startswith("AWAITING_OPERATOR:"):
            return None
        return phase.split(":", 2)[1] or None

    async def validate_backlog_issue_signal(self, workflow_id: str, issue_ref: str) -> None:
        """NEW-114: refuse an issue-bound retry/skip the loop would silently ignore.

        A backlog loop waiting for the operator reports AWAITING_OPERATOR:<issue>:<blocker>
        in its status query; a signal naming any other issue is rejected before it is
        sent. When the loop cannot be queried the durable checks alone apply."""
        try:
            state = await self.client.get_workflow_handle(workflow_id).query("status")
        except Exception:  # noqa: BLE001 - the durable preflight still applies
            return
        phase = str(state.get("phase", "")) if isinstance(state, dict) else ""
        if not phase.startswith("AWAITING_OPERATOR:"):
            raise ValueError(
                f"backlog loop {workflow_id} is not waiting on an issue (phase {phase[:120]}); "
                f"the signal for {issue_ref} was not sent"
            )
        awaited = phase.split(":", 2)[1]
        if awaited != issue_ref:
            raise ValueError(
                f"backlog loop {workflow_id} is waiting on {awaited}, not {issue_ref}; "
                "the signal was not sent"
            )

    async def record_approval(self, request: ApprovalRecord, approver_subject: str) -> None:
        candidate = await self.repository.current_approval_candidate(request.change_set_id)
        if candidate is None:
            raise ValueError("approval candidate is missing or invalidated")
        expected_digest = str(candidate["artifact_binding_sha256"])
        if not hmac.compare_digest(expected_digest, request.artifact_binding_sha256):
            raise ValueError("approval artifact binding changed")
        if candidate["risk_class"] == "R3" and request.decision == "approve":
            expected_confirmation = ApprovalService.typed_confirmation(
                str(request.change_set_id), expected_digest
            )
            if request.typed_confirmation != expected_confirmation:
                raise ValueError("R3 typed confirmation does not bind to immutable artifacts")
        await self.repository.persist_approval(request, approver_subject)
        workflow_id = str(candidate["workflow_id"])
        await self.signal(
            workflow_id,
            "approval",
            {**request.model_dump(mode="json"), "approver_subject": approver_subject},
        )

    async def prepare_deployment(self, request: DeploymentPrepare) -> OperationAccepted:
        workflow_id = f"deploy-{request.environment}-{request.change_set_id}-{uuid4()}"
        application_run_id = uuid4()
        project = await self.repository.create_deployment_run(
            workflow_id, request, application_run_id=application_run_id
        )
        if request.environment == "preview":
            return await self._start_recorded_workflow(
                application_run_id,
                workflow_id,
                WorkflowName.PREVIEW_DEPLOYMENT.value,
                PreviewDeploymentInput(
                    change_set_id=str(request.change_set_id),
                    project=project.value,
                    dry_run=request.dry_run,
                ),
            )
        return await self._start_recorded_workflow(
            application_run_id,
            workflow_id,
            WorkflowName.STAGING_DEPLOYMENT.value,
            StagingDeploymentInput(**request.model_dump()),
        )

    async def rollback(self, request: RollbackRequest) -> OperationAccepted:
        workflow_id = f"recovery-{request.deployment_id}-{uuid4()}"
        application_run_id = uuid4()
        await self.repository.create_recovery_run(
            workflow_id, request, application_run_id=application_run_id
        )
        return await self._start_recorded_workflow(
            application_run_id,
            workflow_id,
            WorkflowName.RECOVERY.value,
            RecoveryInput(**request.model_dump()),
        )

    async def start_runtime_verification(
        self, request: RuntimeVerificationStart
    ) -> OperationAccepted:
        workflow_id = f"runtime-verify-{request.deployment_id}-{uuid4()}"
        application_run_id = uuid4()
        project = await self.repository.create_runtime_verification_run(
            workflow_id, request, application_run_id=application_run_id
        )
        return await self._start_recorded_workflow(
            application_run_id,
            workflow_id,
            WorkflowName.RUNTIME_VERIFICATION.value,
            RuntimeVerificationInput(
                deployment_id=str(request.deployment_id),
                project=project.value,
            ),
        )

    async def workflow_run_status(self, workflow_id: str, run_id: str) -> str:
        """Status of one execution, not of whatever later run reused the workflow ID."""
        description = await self.client.get_workflow_handle(workflow_id, run_id=run_id).describe()
        return description.status.name if description.status is not None else "UNKNOWN"

    async def workflow_id_closed(self, workflow_id: str) -> bool:
        """NEW-115: no execution of this workflow ID is running.

        Temporal runs at most one execution per workflow ID at a time, so the latest
        execution being closed (or the ID being unknown after retention) proves that no
        execution of it runs now. Any other failure propagates (fail closed)."""
        from temporalio.service import RPCError, RPCStatusCode

        async with asyncio.timeout(10):
            try:
                description = await self.client.get_workflow_handle(workflow_id).describe()
            except RPCError as error:
                if error.status == RPCStatusCode.NOT_FOUND:
                    return True
                raise
        return description.status is not None and description.status.name != "RUNNING"

    async def get_reconciliation_observation(self, workflow_id: str) -> dict[str, Any]:
        """Bounded, pinned result read; never follow a different execution implicitly."""
        async with asyncio.timeout(10):
            description = await self.client.get_workflow_handle(workflow_id).describe()
            info = description.raw_description.workflow_execution_info
            value: dict[str, Any] = {
                "workflow_id": workflow_id,
                "run_id": description.run_id,
                "first_execution_run_id": info.first_run_id,
                "status": description.status.name if description.status else "UNKNOWN",
                "identity_verified": False,
                "business_status": None,
                "result_campaign_id": None,
            }
            if value["status"] == "COMPLETED":
                pinned = self.client.get_workflow_handle(workflow_id, run_id=description.run_id)
                result = await pinned.result(follow_runs=False, rpc_timeout=timedelta(seconds=5))
                if isinstance(result, dict):
                    status, campaign_id = result.get("status"), result.get("campaign_id")
                    from engineering_control.operations.reconciliation import TERMINAL_OUTCOMES

                    if isinstance(status, str) and status in TERMINAL_OUTCOMES:
                        value["business_status"] = status
                    if isinstance(campaign_id, str) and len(campaign_id) <= 36:
                        value["result_campaign_id"] = campaign_id
            latest = await self.client.get_workflow_handle(workflow_id).describe()
            value["identity_verified"] = (
                bool(info.first_run_id)
                and latest.run_id == description.run_id
                and latest.raw_description.workflow_execution_info.first_run_id == info.first_run_id
                and latest.status == description.status
            )
            return value

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        handle = self.client.get_workflow_handle(workflow_id)
        description = await handle.describe()
        response: dict[str, Any] = {
            "workflow_id": workflow_id,
            "run_id": description.run_id,
            "status": description.status.name if description.status is not None else "UNKNOWN",
            "workflow_type": description.workflow_type,
        }
        if workflow_id.startswith("acceptance-") and response["status"] == "COMPLETED":
            result = await handle.result()
            if isinstance(result, dict):
                allowed = (
                    "status",
                    "change_set_state",
                    "maker_provider",
                    "checker_provider",
                    "evidence_sha256",
                    "redaction_status",
                )
                response["validation"] = {key: result.get(key) for key in allowed}
        try:
            async with asyncio.timeout(5):
                rows = await self.repository.get_workflow_projection(workflow_id)
            projection = derive_workflow_application_projection(rows)
            response["application"] = asdict(projection)
        except Exception:
            response["application"] = {
                "run_association": "UNAVAILABLE",
                "persisted_status": None,
                "business_status": None,
                "error_code": None,
                "run_projection": None,
                "observed_at": datetime.now(UTC).isoformat(),
                "updated_at": None,
                "started_at": None,
                "finished_at": None,
                "source": "APPLICATION_RUN_PROJECTION",
                "scope": "WORKFLOW_ID_ONLY",
            }
        return response

    async def start_acceptance(
        self, request: AcceptanceLifecycleStart, validation_root: str
    ) -> OperationAccepted:
        change_set_id = uuid4()
        workflow_id = f"acceptance-{change_set_id}"
        handle = await self.client.start_workflow(
            WorkflowName.ACCEPTANCE_LIFECYCLE.value,
            AcceptanceLifecycleInput(
                change_set_id=str(change_set_id),
                validation_root=validation_root,
                hold_seconds=request.hold_seconds,
                activity_task_queue=self.acceptance_activity_task_queue,
            ),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_observability_acceptance(
        self, request: ObservabilityAcceptanceStart
    ) -> OperationAccepted:
        if request.confirm_read_only is not True:
            raise ValueError("observability acceptance requires the read-only contract")
        workspace_id = uuid4()
        workflow_id = f"observability-acceptance-{workspace_id}"
        handle = await self.client.start_workflow(
            WorkflowName.OBSERVABILITY_ACCEPTANCE.value,
            ObservabilityAcceptanceInput(workspace_id=str(workspace_id)),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_reverse_observability_acceptance(
        self, request: ObservabilityAcceptanceStart
    ) -> OperationAccepted:
        if request.confirm_read_only is not True:
            raise ValueError("observability acceptance requires the read-only contract")
        workspace_id = uuid4()
        workflow_id = f"reverse-observability-acceptance-{workspace_id}"
        handle = await self.client.start_workflow(
            WorkflowName.REVERSE_OBSERVABILITY_ACCEPTANCE.value,
            ObservabilityAcceptanceInput(workspace_id=str(workspace_id)),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_issue_loop(self, request: IssueLoopStart) -> OperationAccepted:
        workflow_id = f"loop-{request.project.value}-{request.campaign_id}-{uuid4()}"
        handle = await self.client.start_workflow(
            WorkflowName.ISSUE_SOLUTION_LOOP.value,
            IssueSolutionLoopInput(**request.model_dump(mode="json")),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def import_github_backlog(
        self, request: GitHubBacklogImportRequest
    ) -> BacklogImportAccepted:
        campaign_id = uuid4()
        workflow_id = f"backlog-import-{request.project}-{campaign_id}"
        handle = await self.client.start_workflow(
            WorkflowName.GITHUB_BACKLOG_IMPORT.value,
            GitHubBacklogImportInput(
                backlog_campaign_id=str(campaign_id),
                **request.model_dump(mode="json"),
            ),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return BacklogImportAccepted(
            workflow_id=workflow_id,
            run_id=handle.first_execution_run_id or "",
            backlog_campaign_id=campaign_id,
        )

    async def start_existing_backlog_loop(
        self, request: ExistingBacklogLoopStart
    ) -> OperationAccepted:
        direct = request.target_repository is not None or request.issue_operation_id is not None
        if direct:
            await self.repository.validate_issue_operation_approval(request)
            if request.issue_operation_id is None:
                raise ValueError(
                    "approved global/R3 issue execution is owned by its durable parent; "
                    "direct API starts require an originating typed issue operation"
                )
        campaign = await self.repository.get_backlog_campaign(request.backlog_campaign_id)
        if campaign is None or campaign.get("project") != request.project:
            raise ValueError("existing backlog campaign does not exist")
        if campaign.get("status") not in {
            "APPROVED_POLICY_PENDING",
            "APPROVED",
            "APPROVED_INVENTORY",
        } and not (direct and campaign.get("status") == "RUNNING"):
            raise ValueError("existing backlog campaign requires its exact Telegram approval")
        if campaign.get("maximum_risk") != request.maximum_risk.value:
            raise ValueError("existing backlog maximum risk changed after approval")
        if campaign.get("delivery_mode") != request.delivery_mode:
            raise ValueError("existing backlog delivery mode changed after approval")
        if request.maximum_items > int(campaign.get("maximum_items") or 0):
            raise ValueError("existing backlog item bound exceeds its approval")
        if request.maximum_concurrency > int(campaign.get("maximum_concurrency") or 1):
            raise ValueError("existing backlog concurrency exceeds its approval")
        if direct:
            await self.repository.validate_issue_operation_approval(request)
        workflow_id = (
            issue_workflow_id(request)
            if request.issue_operation_id is not None
            else f"backlog-loop-{request.project}-{request.backlog_campaign_id}-{uuid4()}"
        )
        if campaign.get("status") == "RUNNING":
            # Only an exact approved direct operation can reach this branch.
            # Resolve its actual existing execution; missing/wrong history is
            # not permission to start another workflow from a running projection.
            try:
                return await self._existing_issue_workflow(workflow_id)
            except Exception as error:
                raise IssueStartUnconfirmedError(request) from error
        try:
            handle = await self.client.start_workflow(
                WorkflowName.EXISTING_GITHUB_BACKLOG_LOOP.value,
                ExistingGitHubBacklogLoopInput(**request.model_dump(mode="json")),
                id=workflow_id,
                task_queue=self.task_queue,
                id_reuse_policy=(
                    WorkflowIDReusePolicy.REJECT_DUPLICATE
                    if direct
                    else WorkflowIDReusePolicy.ALLOW_DUPLICATE
                ),
                rpc_timeout=timedelta(seconds=30),
            )
        except WorkflowAlreadyStartedError as error:
            if not direct:
                raise
            try:
                return await self._existing_issue_workflow(workflow_id, run_id=error.run_id)
            except Exception as lookup_error:
                raise IssueStartUnconfirmedError(request) from lookup_error
        except Exception as error:
            if direct:
                raise IssueStartUnconfirmedError(request) from error
            raise
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def _existing_issue_workflow(
        self,
        workflow_id: str,
        *,
        run_id: str | None = None,
        rpc_timeout: timedelta = timedelta(seconds=30),
    ) -> OperationAccepted:
        existing = self.client.get_workflow_handle(workflow_id, run_id=run_id)
        description = await existing.describe(rpc_timeout=rpc_timeout)
        if (
            description.id != workflow_id
            or description.workflow_type != WorkflowName.EXISTING_GITHUB_BACKLOG_LOOP.value
        ):
            raise ValueError("existing issue workflow identity does not match the operation")
        return OperationAccepted(
            workflow_id=workflow_id, run_id=description.run_id, status="EXISTING"
        )

    async def observe_issue_start(self, request: ExistingBacklogLoopStart) -> OperationAccepted:
        # Only describe an exact persisted attempt. No current-policy approval,
        # new start, signal, cancellation or external write occurs here.
        request = ExistingBacklogLoopStart.model_validate(request.model_dump(mode="json"))
        if request.issue_operation_id is None or request.issue_approval_binding is None:
            raise ValueError("issue observation requires an immutable operation reference")
        accepted = await self._existing_issue_workflow(
            issue_workflow_id(request), rpc_timeout=timedelta(seconds=5)
        )
        UUID(accepted.run_id)
        return accepted

    async def start_autonomous_backlog(self, request: AutonomousBacklogStart) -> OperationAccepted:
        for project, campaign_id in request.backlog_campaign_ids.items():
            campaign = await self.repository.get_backlog_campaign(campaign_id)
            if campaign is None or campaign.get("project") != project:
                raise ValueError("global backlog inventory binding is invalid")
        policy = await self.repository.get_backlog_campaign(request.policy_campaign_id)
        if policy is None or policy.get("status") not in {"APPROVED", "RUNNING"}:
            raise ValueError("global backlog GitHub write policy is not active")
        workflow_id = "autonomous-backlog-staging"
        workflow_input = AutonomousBacklogCampaignInput(
            campaign_id=str(request.campaign_id),
            backlog_campaign_ids={
                project: str(campaign_id)
                for project, campaign_id in request.backlog_campaign_ids.items()
            },
            policy_campaign_id=str(request.policy_campaign_id),
        )
        try:
            handle = await self.client.start_workflow(
                WorkflowName.AUTONOMOUS_BACKLOG_CAMPAIGN.value,
                workflow_input,
                id=workflow_id,
                task_queue=self.task_queue,
            )
            run_id = handle.first_execution_run_id or ""
        except WorkflowAlreadyStartedError:
            existing = self.client.get_workflow_handle(workflow_id)
            description = await existing.describe()
            run_id = description.run_id
        return OperationAccepted(workflow_id=workflow_id, run_id=run_id)

    async def start_enhancement_discovery(self, request: EnhancementDiscover) -> OperationAccepted:
        workflow_id = f"enhance-discover-{request.project.value}-{request.campaign_id}-{uuid4()}"
        handle = await self.client.start_workflow(
            WorkflowName.ENHANCEMENT_DISCOVERY.value,
            EnhancementDiscoveryInput(**request.model_dump(mode="json")),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_enhancement_solution(
        self, enhancement_id: str, request: EnhancementRun
    ) -> OperationAccepted:
        workflow_id = f"enhance-solution-{enhancement_id}-{uuid4()}"
        handle = await self.client.start_workflow(
            WorkflowName.ENHANCEMENT_SOLUTION_LOOP.value,
            EnhancementSolutionLoopInput(
                enhancement_id=enhancement_id,
                **request.model_dump(mode="json"),
            ),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_llmwiki_bootstrap_plan(
        self, request: LLMWikiBootstrapPlanRequest
    ) -> OperationAccepted:
        workflow_id = f"bootstrap-plan-llmwiki-staging-{uuid4()}"
        handle = await self.client.start_workflow(
            WorkflowName.LLMWIKI_STAGING_BOOTSTRAP.value,
            LLMWikiStagingBootstrapInput(**request.model_dump(mode="json")),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_llmwiki_bootstrap(
        self, request: LLMWikiBootstrapExecuteRequest
    ) -> OperationAccepted:
        workflow_id = f"bootstrap-llmwiki-staging-{request.approval_binding[:16]}"
        handle = await self.client.start_workflow(
            WorkflowName.LLMWIKI_STAGING_BOOTSTRAP.value,
            LLMWikiStagingBootstrapInput(**request.model_dump(mode="json")),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_change_request(
        self, change_request_id: UUID, revision: int
    ) -> OperationAccepted:
        """NEW-108: one deterministic draft workflow per change request revision."""
        return await self._start_once(
            WorkflowName.CHANGE_REQUEST.value,
            ChangeRequestInput(change_request_id=str(change_request_id), revision=revision),
            change_request_workflow_id(str(change_request_id), revision),
        )

    async def start_ui_plan(
        self, blueprint_id: UUID, project: str, requested_by: str, source_sha: str | None
    ) -> OperationAccepted:
        """NEW-123: one flow A blueprint workflow per blueprint row."""
        return await self._start_once(
            WorkflowName.UI_MIGRATION_PLAN.value,
            UiMigrationPlanInput(
                blueprint_id=str(blueprint_id),
                project=project,
                requested_by=requested_by,
                source_sha=source_sha,
            ),
            ui_plan_workflow_id(project, str(blueprint_id)),
        )

    async def start_ui_fanout(
        self, blueprint_id: UUID, max_items: int, workflow_id: str
    ) -> OperationAccepted:
        """NEW-123: start the fan-out execution the caller claimed (the blueprint's single
        active-fan-out marker). Every item is idempotent under its deterministic id, so a
        later execution resumes with the items not yet drafted."""
        if not workflow_id.startswith(f"ui-blueprint-fanout-{blueprint_id}-"):
            raise ValueError("ui fan-out workflow id does not belong to the blueprint")
        return await self._start_once(
            WorkflowName.UI_BLUEPRINT_FANOUT.value,
            UiBlueprintFanOutInput(blueprint_id=str(blueprint_id), max_items=max_items),
            workflow_id,
        )

    async def start_ui_gap_consolidation(
        self, consolidation_id: UUID, workflow_id: str
    ) -> OperationAccepted:
        """NEW-154: the plan workflow of one gap consolidation record (idempotent)."""
        if workflow_id != consolidation_workflow_id(str(consolidation_id)):
            raise ValueError("ui gap consolidation workflow id does not match its record")
        return await self._start_once(
            WorkflowName.UI_GAP_CONSOLIDATION_PLAN.value,
            UiGapConsolidationPlanInput(consolidation_id=str(consolidation_id)),
            workflow_id,
        )

    async def ui_fanout_state(self, workflow_id: str) -> tuple[str, OperationAccepted | None]:
        """One lookup: ("RUNNING", accepted), ("CLOSED", None) or ("NOT_FOUND", None).

        NEW-123 review 3: a single describe, so a start landing between two lookups can
        never make a running fan-out look ended."""
        try:
            description = await self.client.get_workflow_handle(workflow_id).describe()
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                return "NOT_FOUND", None
            raise
        if description.status == WorkflowExecutionStatus.RUNNING:
            return "RUNNING", OperationAccepted(workflow_id=workflow_id, run_id=description.run_id)
        return "CLOSED", None

    async def _start_once(
        self, workflow: str, argument: Any, workflow_id: str
    ) -> OperationAccepted:
        # A duplicate request joins the existing execution; only a failed one may rerun.
        try:
            handle = await self.client.start_workflow(
                workflow,
                argument,
                id=workflow_id,
                task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
            run_id = handle.first_execution_run_id or ""
        except WorkflowAlreadyStartedError:
            description = await self.client.get_workflow_handle(workflow_id).describe()
            run_id = description.run_id
        return OperationAccepted(workflow_id=workflow_id, run_id=run_id)
