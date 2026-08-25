from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from temporalio.client import Client

from engineering_control.api.auth import bearer_auth
from engineering_control.api.schemas import (
    AcceptanceLifecycleStart,
    ApprovalRecord,
    AuditPlanRequest,
    CampaignStart,
    DeliveryReconcile,
    DeliveryStart,
    DeploymentPrepare,
    EnhancementApproval,
    EnhancementDiscover,
    EnhancementRejection,
    EnhancementRun,
    FindingVerificationStart,
    IssueLoopStart,
    LLMWikiBootstrapExecuteRequest,
    LLMWikiBootstrapPlanRequest,
    OperationAccepted,
    RecordList,
    RollbackRequest,
    RuntimeVerificationStart,
)
from engineering_control.api.service import ControlService, TemporalControlService
from engineering_control.config import Settings, get_settings
from engineering_control.db.repository import (
    EmptyMetadataRepository,
    MetadataRepository,
    SQLMetadataRepository,
)
from engineering_control.logging import configure_logging
from engineering_control.models.enums import Project
from engineering_control.observability.metrics import ControlPlaneMetrics


def create_app(
    *,
    settings: Settings | None = None,
    service: ControlService | None = None,
    repository: MetadataRepository | None = None,
    token: str | None = None,
) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_repository = repository
        if resolved_repository is None:
            resolved_repository = (
                EmptyMetadataRepository()
                if service is not None
                else SQLMetadataRepository(resolved.database_url.get_secret_value())
            )
        app.state.metadata_repository = resolved_repository
        if service is None:
            client = await Client.connect(
                resolved.temporal_target, namespace=resolved.temporal_namespace
            )
            app.state.control_service = TemporalControlService(
                client, resolved.temporal_task_queue, resolved_repository
            )
        else:
            app.state.control_service = service
        yield

    app = FastAPI(
        title="Engineering Control API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    expected_token = token if token is not None else resolved.read_api_token()
    authenticated = bearer_auth(expected_token)
    auth_dependency = Depends(authenticated)

    def control(request: Request) -> ControlService:
        return cast(ControlService, request.app.state.control_service)

    def metadata(request: Request) -> MetadataRepository:
        return cast(MetadataRepository, request.app.state.metadata_repository)

    control_dependency = Depends(control)
    metadata_dependency = Depends(metadata)
    control_metrics = ControlPlaneMetrics()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request) -> dict[str, str]:
        if not hasattr(request.app.state, "control_service"):
            raise HTTPException(status_code=503, detail="control service unavailable")
        return {"status": "ready"}

    @app.post("/v1/campaigns", response_model=OperationAccepted, status_code=202)
    async def campaign_start(
        body: CampaignStart,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        return await service.start_campaign(body)

    @app.post("/v1/audit-plan")
    async def audit_plan(
        body: AuditPlanRequest,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, Any]:
        try:
            return await service.plan_campaign(body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/bootstrap-plan", response_model=OperationAccepted, status_code=202)
    async def llmwiki_bootstrap_plan(
        body: LLMWikiBootstrapPlanRequest,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        return await service.start_llmwiki_bootstrap_plan(body)

    @app.post("/v1/bootstrap", response_model=OperationAccepted, status_code=202)
    async def llmwiki_bootstrap(
        body: LLMWikiBootstrapExecuteRequest,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        return await service.start_llmwiki_bootstrap(body)

    @app.get("/v1/campaigns", response_model=RecordList)
    async def campaign_list(
        _subject: str = auth_dependency,
        limit: int = Query(default=50, ge=1, le=200),
        repository: MetadataRepository = metadata_dependency,
    ) -> RecordList:
        return RecordList(items=await repository.list_campaigns(limit), next_cursor=None)

    @app.get("/v1/campaigns/{workflow_id}")
    async def campaign_get(
        workflow_id: str,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, Any]:
        return await service.get_workflow(workflow_id)

    @app.post("/v1/campaigns/{workflow_id}/{action}", status_code=202)
    async def campaign_control(
        workflow_id: str,
        action: str,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, str]:
        if action not in {"pause", "resume", "cancel"}:
            raise HTTPException(status_code=404, detail="unknown campaign action")
        await service.signal(workflow_id, action)
        return {"status": "ACCEPTED"}

    @app.get("/v1/findings", response_model=RecordList)
    async def finding_list(
        _subject: str = auth_dependency,
        limit: int = Query(default=50, ge=1, le=200),
        repository: MetadataRepository = metadata_dependency,
    ) -> RecordList:
        return RecordList(items=await repository.list_findings(limit), next_cursor=None)

    @app.post("/v1/loops", response_model=OperationAccepted, status_code=202)
    async def issue_loop_start(
        body: IssueLoopStart,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        return await service.start_issue_loop(body)

    @app.get("/v1/loops/{workflow_id}")
    async def issue_loop_get(
        workflow_id: str,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, Any]:
        return await service.get_workflow(workflow_id)

    @app.post("/v1/loops/{workflow_id}/{action}", status_code=202)
    async def issue_loop_control(
        workflow_id: str,
        action: str,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, str]:
        if action not in {"pause", "resume", "cancel"}:
            raise HTTPException(status_code=404, detail="unknown loop action")
        await service.signal(workflow_id, action)
        return {"status": "ACCEPTED"}

    @app.post("/v1/enhancements/discover", response_model=OperationAccepted, status_code=202)
    async def enhancement_discover(
        body: EnhancementDiscover,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        return await service.start_enhancement_discovery(body)

    @app.get("/v1/enhancements", response_model=RecordList)
    async def enhancement_list(
        project: Project,
        status: str | None = Query(default=None, pattern=r"^[A-Z_]{3,64}$"),
        limit: int = Query(default=50, ge=1, le=200),
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> RecordList:
        return RecordList(
            items=await repository.list_enhancements(project, status, limit),
            next_cursor=None,
        )

    @app.get("/v1/enhancements/{enhancement_id}")
    async def enhancement_get(
        enhancement_id: str,
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        value = await repository.get_enhancement(enhancement_id)
        if value is None:
            raise HTTPException(status_code=404, detail="enhancement not found")
        return value

    @app.post("/v1/enhancements/{enhancement_id}/approve")
    async def enhancement_approve(
        enhancement_id: str,
        body: EnhancementApproval,
        subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        try:
            return await repository.approve_enhancement(
                enhancement_id, body.approval_binding, subject
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/enhancements/{enhancement_id}/reject")
    async def enhancement_reject(
        enhancement_id: str,
        body: EnhancementRejection,
        subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        try:
            return await repository.reject_enhancement(enhancement_id, body.reason, subject)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/v1/enhancements/{enhancement_id}/run",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def enhancement_run(
        enhancement_id: str,
        body: EnhancementRun,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        return await service.start_enhancement_solution(enhancement_id, body)

    @app.get("/v1/findings/{finding_id}")
    async def finding_get(
        finding_id: UUID,
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        finding = await repository.get_finding(finding_id)
        if finding is None:
            raise HTTPException(status_code=404, detail="finding not found")
        return finding

    @app.post("/v1/findings/verify", response_model=OperationAccepted, status_code=202)
    async def finding_verify(
        body: FindingVerificationStart,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        try:
            return await service.start_finding_verification(body.finding_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/deliveries", response_model=OperationAccepted, status_code=202)
    async def delivery_start(
        body: DeliveryStart,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        return await service.start_delivery(body)

    @app.post("/v1/deliveries/reconcile", response_model=OperationAccepted, status_code=202)
    async def delivery_reconcile(
        body: DeliveryReconcile,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        try:
            return await service.reconcile_delivery(body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/runs/{workflow_id}")
    async def run_get(
        workflow_id: str,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, Any]:
        return await service.get_workflow(workflow_id)

    @app.post("/v1/runs/{workflow_id}/cancel", status_code=202)
    async def run_cancel(
        workflow_id: str,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, str]:
        await service.signal(workflow_id, "cancel")
        return {"status": "ACCEPTED"}

    @app.post("/v1/approvals", status_code=202)
    async def approval_record(
        body: ApprovalRecord,
        subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, str]:
        try:
            await service.record_approval(body, subject)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"status": "ACCEPTED"}

    @app.get("/v1/approvals/{change_set_id}/candidate")
    async def approval_candidate_get(
        change_set_id: UUID,
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        candidate = await repository.current_approval_candidate(change_set_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="approval candidate not found")
        return candidate

    @app.post("/v1/deployments/prepare", response_model=OperationAccepted, status_code=202)
    async def deployment_prepare(
        body: DeploymentPrepare,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        try:
            return await service.prepare_deployment(body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/deployments/rollback", response_model=OperationAccepted, status_code=202)
    async def deployment_rollback(
        body: RollbackRequest,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        try:
            return await service.rollback(body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/v1/deployments/verify-runtime",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def runtime_verification_start(
        body: RuntimeVerificationStart,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        try:
            return await service.start_runtime_verification(body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/deployments/{deployment_id}")
    async def deployment_get(
        deployment_id: UUID,
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        deployment = await repository.get_deployment(deployment_id)
        if deployment is None:
            raise HTTPException(status_code=404, detail="deployment not found")
        return deployment

    @app.get("/v1/usage")
    async def usage_get(
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        snapshots = await repository.latest_quota_snapshots()
        history = await repository.provider_usage_history(days=7)
        response: dict[str, Any] = {}
        for provider in ("codex", "claude"):
            value = dict(
                snapshots.get(
                    provider,
                    {
                        "provider": provider,
                        "remaining_capacity": None,
                        "total_capacity": None,
                        "reset_at": None,
                        "running_jobs": 0,
                        "rate_limited": False,
                        "authenticated": None,
                        "cli_present": None,
                        "status": "NOT_REPORTED",
                        "source": "not-reported",
                    },
                )
            )
            provider_history = history.get(
                provider, {"window_days": 7, "running_jobs": 0, "roles": {}}
            )
            value["running_jobs"] = max(
                int(value.get("running_jobs") or 0), int(provider_history["running_jobs"])
            )
            value["history"] = provider_history
            response[provider] = value
        return response

    @app.get("/v1/evidence/{artifact_id}")
    async def evidence_get(
        artifact_id: UUID,
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        artifact = await repository.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return artifact

    @app.post("/v1/validation/lifecycle", response_model=OperationAccepted, status_code=202)
    async def validation_lifecycle(
        body: AcceptanceLifecycleStart,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        if not resolved.acceptance_enabled:
            raise HTTPException(status_code=404, detail="synthetic validation is disabled")
        return await service.start_acceptance(body, str(resolved.acceptance_root))

    @app.get("/internal/metrics", include_in_schema=False)
    async def internal_metrics(
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> Response:
        payload, content_type = control_metrics.render(
            await repository.metrics_snapshot(),
            heartbeat_ttl_seconds=resolved.worker_heartbeat_seconds * 3,
        )
        return Response(content=payload, media_type=content_type)

    return app


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.api_host,
        port=settings.api_port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    asyncio.run(asyncio.to_thread(run))
