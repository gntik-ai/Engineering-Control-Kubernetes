from __future__ import annotations

import hmac
from typing import Any, Protocol
from uuid import UUID, uuid4

from temporalio.client import Client

from engineering_control.api.schemas import (
    AcceptanceLifecycleStart,
    ApprovalRecord,
    AuditPlanRequest,
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
    OperationAccepted,
    RollbackRequest,
    RuntimeVerificationStart,
)
from engineering_control.db.repository import MetadataRepository
from engineering_control.engineering.models import CampaignPlan
from engineering_control.policy.approvals import ApprovalService
from engineering_control.workflows.inputs import (
    AcceptanceLifecycleInput,
    AuditCampaignInput,
    DeliveryReconciliationInput,
    EnhancementDiscoveryInput,
    EnhancementSolutionLoopInput,
    ExistingGitHubBacklogLoopInput,
    FindingVerificationInput,
    GitHubBacklogImportInput,
    IssueDeliveryInput,
    IssueSolutionLoopInput,
    LLMWikiStagingBootstrapInput,
    PreviewDeploymentInput,
    RecoveryInput,
    RuntimeVerificationInput,
    StagingDeploymentInput,
)
from engineering_control.workflows.names import WorkflowName


class ControlService(Protocol):
    async def plan_campaign(self, request: AuditPlanRequest) -> dict[str, Any]: ...
    async def start_campaign(self, request: CampaignStart) -> OperationAccepted: ...
    async def start_delivery(self, request: DeliveryStart) -> OperationAccepted: ...
    async def start_finding_verification(self, finding_id: UUID) -> OperationAccepted: ...
    async def reconcile_delivery(self, request: DeliveryReconcile) -> OperationAccepted: ...
    async def signal(self, workflow_id: str, signal: str, argument: Any = None) -> None: ...
    async def prepare_deployment(self, request: DeploymentPrepare) -> OperationAccepted: ...
    async def rollback(self, request: RollbackRequest) -> OperationAccepted: ...
    async def start_runtime_verification(
        self, request: RuntimeVerificationStart
    ) -> OperationAccepted: ...
    async def get_workflow(self, workflow_id: str) -> dict[str, Any]: ...
    async def record_approval(self, request: ApprovalRecord, approver_subject: str) -> None: ...
    async def start_acceptance(
        self, request: AcceptanceLifecycleStart, validation_root: str
    ) -> OperationAccepted: ...
    async def start_issue_loop(self, request: IssueLoopStart) -> OperationAccepted: ...
    async def import_github_backlog(
        self, request: GitHubBacklogImportRequest
    ) -> BacklogImportAccepted: ...
    async def start_existing_backlog_loop(
        self, request: ExistingBacklogLoopStart
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

    async def start_campaign(self, request: CampaignStart) -> OperationAccepted:
        campaign_id = uuid4()
        workflow_id = f"audit-{request.project.value}-{campaign_id}"
        await self.repository.create_campaign(campaign_id, workflow_id, request)
        value = request.model_dump(mode="json")
        value["preset"] = request.preset or "static-full"
        value["environment"] = request.environment or request.environments[0]
        handle = await self.client.start_workflow(
            WorkflowName.AUDIT_CAMPAIGN.value,
            AuditCampaignInput(**value, campaign_id=str(campaign_id)),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_delivery(self, request: DeliveryStart) -> OperationAccepted:
        workflow_id = f"delivery-{request.finding_id}-{uuid4()}"
        await self.repository.create_delivery_run(workflow_id, request)
        handle = await self.client.start_workflow(
            WorkflowName.ISSUE_DELIVERY.value,
            IssueDeliveryInput(**request.model_dump()),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_finding_verification(self, finding_id: UUID) -> OperationAccepted:
        workflow_id = f"finding-verify-{finding_id}-{uuid4()}"
        project = await self.repository.create_finding_verification_run(workflow_id, finding_id)
        handle = await self.client.start_workflow(
            WorkflowName.FINDING_VERIFICATION.value,
            FindingVerificationInput(
                finding_id=str(finding_id),
                project=project.value,
                fresh_snapshot=True,
            ),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def reconcile_delivery(self, request: DeliveryReconcile) -> OperationAccepted:
        workflow_id = f"reconcile-{request.change_set_id}-{uuid4()}"
        await self.repository.create_delivery_reconciliation_run(workflow_id, request)
        handle = await self.client.start_workflow(
            WorkflowName.DELIVERY_RECONCILIATION.value,
            DeliveryReconciliationInput(**request.model_dump(mode="json")),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def signal(self, workflow_id: str, signal: str, argument: Any = None) -> None:
        allowed = {"pause", "resume", "cancel", "approval", "skip", "retry"}
        if signal not in allowed:
            raise ValueError("unsupported control signal")
        handle = self.client.get_workflow_handle(workflow_id)
        await handle.signal(signal, argument)

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
        project = await self.repository.create_deployment_run(workflow_id, request)
        if request.environment == "preview":
            handle = await self.client.start_workflow(
                WorkflowName.PREVIEW_DEPLOYMENT.value,
                PreviewDeploymentInput(
                    change_set_id=str(request.change_set_id),
                    project=project.value,
                    dry_run=request.dry_run,
                ),
                id=workflow_id,
                task_queue=self.task_queue,
            )
        else:
            handle = await self.client.start_workflow(
                WorkflowName.STAGING_DEPLOYMENT.value,
                StagingDeploymentInput(**request.model_dump()),
                id=workflow_id,
                task_queue=self.task_queue,
            )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def rollback(self, request: RollbackRequest) -> OperationAccepted:
        workflow_id = f"recovery-{request.deployment_id}-{uuid4()}"
        await self.repository.create_recovery_run(workflow_id, request)
        handle = await self.client.start_workflow(
            WorkflowName.RECOVERY.value,
            RecoveryInput(**request.model_dump()),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

    async def start_runtime_verification(
        self, request: RuntimeVerificationStart
    ) -> OperationAccepted:
        workflow_id = f"runtime-verify-{request.deployment_id}-{uuid4()}"
        project = await self.repository.create_runtime_verification_run(workflow_id, request)
        handle = await self.client.start_workflow(
            WorkflowName.RUNTIME_VERIFICATION.value,
            RuntimeVerificationInput(
                deployment_id=str(request.deployment_id),
                project=project.value,
            ),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

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
        campaign = await self.repository.get_backlog_campaign(request.backlog_campaign_id)
        if campaign is None or campaign.get("project") != request.project:
            raise ValueError("existing backlog campaign does not exist")
        if campaign.get("status") not in {"APPROVED_POLICY_PENDING", "APPROVED"}:
            raise ValueError("existing backlog campaign requires its exact Telegram approval")
        if campaign.get("maximum_risk") != request.maximum_risk.value:
            raise ValueError("existing backlog maximum risk changed after approval")
        if campaign.get("delivery_mode") != request.delivery_mode:
            raise ValueError("existing backlog delivery mode changed after approval")
        if request.maximum_items > int(campaign.get("maximum_items") or 0):
            raise ValueError("existing backlog item bound exceeds its approval")
        workflow_id = f"backlog-loop-{request.project}-{request.backlog_campaign_id}-{uuid4()}"
        handle = await self.client.start_workflow(
            WorkflowName.EXISTING_GITHUB_BACKLOG_LOOP.value,
            ExistingGitHubBacklogLoopInput(**request.model_dump(mode="json")),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )

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
