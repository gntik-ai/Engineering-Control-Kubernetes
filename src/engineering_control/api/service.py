from __future__ import annotations

import hmac
from typing import Any, Protocol
from uuid import UUID, uuid4

from temporalio.client import Client

from engineering_control.api.schemas import (
    AcceptanceLifecycleStart,
    ApprovalRecord,
    CampaignStart,
    DeliveryReconcile,
    DeliveryStart,
    DeploymentPrepare,
    OperationAccepted,
    RollbackRequest,
    RuntimeVerificationStart,
)
from engineering_control.db.repository import MetadataRepository
from engineering_control.policy.approvals import ApprovalService
from engineering_control.workflows.inputs import (
    AcceptanceLifecycleInput,
    AuditCampaignInput,
    DeliveryReconciliationInput,
    FindingVerificationInput,
    IssueDeliveryInput,
    PreviewDeploymentInput,
    RecoveryInput,
    RuntimeVerificationInput,
    StagingDeploymentInput,
)
from engineering_control.workflows.names import WorkflowName


class ControlService(Protocol):
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


class TemporalControlService:
    def __init__(self, client: Client, task_queue: str, repository: MetadataRepository) -> None:
        self.client = client
        self.task_queue = task_queue
        self.repository = repository

    async def start_campaign(self, request: CampaignStart) -> OperationAccepted:
        campaign_id = uuid4()
        workflow_id = f"audit-{request.project.value}-{campaign_id}"
        await self.repository.create_campaign(campaign_id, workflow_id, request)
        handle = await self.client.start_workflow(
            WorkflowName.AUDIT_CAMPAIGN.value,
            AuditCampaignInput(**request.model_dump(), campaign_id=str(campaign_id)),
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
        allowed = {"pause", "resume", "cancel", "approval"}
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
            ),
            id=workflow_id,
            task_queue=self.task_queue,
        )
        return OperationAccepted(
            workflow_id=workflow_id, run_id=handle.first_execution_run_id or ""
        )
