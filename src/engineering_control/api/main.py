from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from engineering_control.api.admission import MaintenanceAdmission
from engineering_control.api.auth import bearer_auth
from engineering_control.api.schemas import (
    AcceptanceLifecycleStart,
    ApprovalRecord,
    AuditPlanRequest,
    BacklogApprovalCandidateRequest,
    BacklogApprovalRequest,
    BacklogDeliveryApprovalRequest,
    BacklogImportAccepted,
    BacklogIssueAction,
    CampaignStart,
    DeliveryReconcile,
    DeliveryStart,
    DeploymentPrepare,
    EnhancementApproval,
    EnhancementDiscover,
    EnhancementRejection,
    EnhancementRun,
    ExistingBacklogLoopStart,
    FindingVerificationStart,
    GitHubBacklogImportRequest,
    IssueLoopStart,
    LLMWikiBootstrapExecuteRequest,
    LLMWikiBootstrapPlanRequest,
    LoopPlanDispatch,
    LoopStartDispatch,
    ObservabilityAcceptanceStart,
    OperationAccepted,
    R3IssueApprovalRequest,
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
from engineering_control.engineering.backlog import canonical_operator_approval_actor
from engineering_control.logging import configure_logging
from engineering_control.models.enums import Project, RiskClass
from engineering_control.observability.agent import (
    AgentObservabilityRepository,
    GatewayEventConflict,
    GatewayEventInput,
)
from engineering_control.observability.metrics import ControlPlaneMetrics
from engineering_control.operations.issue_start import IssueStartUnconfirmedError
from engineering_control.operations.models import parse_command
from engineering_control.operations.repository import OperationsRepository
from engineering_control.operations.service import TypedCommandBus, operation_dispatch_loop


def create_app(
    *,
    settings: Settings | None = None,
    service: ControlService | None = None,
    repository: MetadataRepository | None = None,
    token: str | None = None,
    gateway_repository: AgentObservabilityRepository | None = None,
    operations_repository: OperationsRepository | None = None,
    command_bus: TypedCommandBus | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    admission_mode = resolved.api_admission_mode

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_repository = repository
        if resolved_repository is None:
            resolved_repository = (
                EmptyMetadataRepository()
                if service is not None
                else SQLMetadataRepository(
                    resolved.database_url.get_secret_value(),
                    github_policy_request_root=(resolved.state_root / "github-policy-requests"),
                )
            )
        app.state.metadata_repository = resolved_repository
        app.state.gateway_repository = gateway_repository or AgentObservabilityRepository(
            resolved.database_url.get_secret_value(),
            maximum_transcript_bytes=resolved.dashboard_transcript_max_bytes,
        )
        if service is None:
            client = await Client.connect(
                resolved.temporal_target, namespace=resolved.temporal_namespace
            )
            app.state.control_service = TemporalControlService(
                client, resolved.temporal_task_queue, resolved_repository
            )
        else:
            app.state.control_service = service
        resolved_operations = operations_repository
        if resolved_operations is None and (service is None or command_bus is not None):
            resolved_operations = OperationsRepository(
                resolved.database_url.get_secret_value(),
                export_retention_days=resolved.dashboard_operation_export_retention_days,
                export_max_bytes=resolved.dashboard_operation_export_max_bytes,
                project_config_root=resolved.project_config_root,
            )
        app.state.operations_repository = resolved_operations
        resolved_bus = command_bus
        if resolved_bus is None and resolved_operations is not None:
            resolved_bus = TypedCommandBus(
                cast(ControlService, app.state.control_service),
                resolved_repository,
                resolved_operations,
            )
        app.state.command_bus = resolved_bus
        stop = asyncio.Event()
        dispatcher = (
            asyncio.create_task(operation_dispatch_loop(resolved_bus, stop))
            if resolved_bus is not None and admission_mode == "normal"
            else None
        )
        app.state.operation_dispatcher = dispatcher
        yield
        stop.set()
        if dispatcher is not None:
            await dispatcher
        if gateway_repository is None:
            cast(AgentObservabilityRepository, app.state.gateway_repository).engine.dispose()
        if operations_repository is None and resolved_operations is not None:
            resolved_operations.engine.dispose()

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
    app.add_middleware(MaintenanceAdmission, mode=admission_mode, authenticate=authenticated)

    @app.exception_handler(IssueStartUnconfirmedError)
    async def issue_start_unconfirmed(
        _request: Request, error: IssueStartUnconfirmedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "status": "START_UNCONFIRMED",
                "operation_id": str(error.operation_id),
                "expected_workflow_id": error.workflow_id,
                "detail": (
                    "Start acknowledgement is unconfirmed; preserve this operation and inspect "
                    "its status. Do not start a replacement issue run."
                ),
            },
        )

    def control(request: Request) -> ControlService:
        return cast(ControlService, request.app.state.control_service)

    def metadata(request: Request) -> MetadataRepository:
        return cast(MetadataRepository, request.app.state.metadata_repository)

    def gateway_events(request: Request) -> AgentObservabilityRepository:
        return cast(AgentObservabilityRepository, request.app.state.gateway_repository)

    def operations(request: Request) -> OperationsRepository:
        value = request.app.state.operations_repository
        if value is None:
            raise HTTPException(status_code=503, detail="operation repository unavailable")
        return cast(OperationsRepository, value)

    def commands(request: Request) -> TypedCommandBus:
        value = request.app.state.command_bus
        if value is None:
            raise HTTPException(status_code=503, detail="typed command bus unavailable")
        return cast(TypedCommandBus, value)

    control_dependency = Depends(control)
    metadata_dependency = Depends(metadata)
    operations_dependency = Depends(operations)
    commands_dependency = Depends(commands)
    control_metrics = ControlPlaneMetrics()

    @app.post("/v1/commands", status_code=202)
    async def typed_command(
        body: dict[str, Any],
        subject: str = auth_dependency,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128),
        bus: TypedCommandBus = commands_dependency,
    ) -> dict[str, Any]:
        try:
            if subject != "engineering-ctl:hermes" and not subject.startswith("telegram:"):
                raise HTTPException(status_code=403, detail="typed command channel is not allowed")
            command = parse_command(body)
            source_channel = "telegram" if subject.startswith("telegram:") else "cli"
            result = await bus.submit(
                command,
                source_channel=source_channel,  # type: ignore[arg-type]
                operator_class=subject,
                idempotency_key=idempotency_key,
            )
            return result.model_dump(mode="json")
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/operations/{operation_id}")
    async def operation_get(
        operation_id: UUID,
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        value = await repository.operation(operation_id)
        if value is None:
            raise HTTPException(status_code=404, detail="operation not found")
        return value.model_dump(mode="json")

    @app.get("/v1/operations/{operation_id}/export")
    async def operation_export_get(
        operation_id: UUID,
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        value = await repository.export_content(operation_id)
        if value is None:
            raise HTTPException(status_code=404, detail="operation export not found")
        return value

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    def admission_snapshot(request: Request) -> dict[str, str | bool]:
        dispatcher = request.app.state.operation_dispatcher
        return {
            "scope": "private-api-only",
            "mode": admission_mode,
            "pending_operation_dispatcher_running": dispatcher is not None
            and not dispatcher.done(),
        }

    @app.get("/v1/admission")
    async def admission_get(
        request: Request,
        _subject: str = auth_dependency,
    ) -> dict[str, str | bool]:
        return admission_snapshot(request)

    @app.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        if not hasattr(request.app.state, "control_service"):
            raise HTTPException(status_code=503, detail="control service unavailable")
        # Value-free process state on the existing private health surface, not
        # approval or project readiness. The dashboard never needs the API token.
        return JSONResponse(
            {"status": "ready", "admission": admission_snapshot(request)},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/readiness/{scope}")
    async def readiness_get(
        scope: str,
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        if scope not in {"core", "all"}:
            raise HTTPException(status_code=404, detail="readiness scope not found")
        value = await repository.operator_snapshot(f"readiness-{scope}")
        return {**value, "scope": scope}

    @app.get("/v1/connections")
    async def connections_get(
        scope: str = Query(default="all", pattern=r"^all$"),
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        values = await repository.connections()
        return {
            "scope": scope,
            "items": [value.model_dump(mode="json") for value in values],
            "next_cursor": None,
        }

    @app.get("/v1/workers")
    async def workers_get(
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        return await repository.operator_snapshot("workers")

    @app.get("/v1/providers")
    async def providers_get(
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        return await repository.operator_snapshot("providers")

    @app.get("/v1/channel-check")
    async def channel_check_get(
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        return await repository.operator_snapshot("channel")

    @app.get("/v1/temporal/summary")
    async def temporal_summary_get(
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        return await repository.operator_snapshot("temporal")

    @app.post("/v1/campaigns/reconcile")
    async def campaigns_reconcile(
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=10_000),
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        terminal = {"COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}
        awaiting = {"AWAITING_APPROVAL", "AWAITING_HUMAN", "PAUSED"}
        external_wait = {"EXTERNAL_WAIT", "AWAITING_CI", "AWAITING_ARGO", "AWAITING_BUILD"}
        candidates = await repository.campaign_reconciliation_candidates(limit + 1, offset)
        has_more = len(candidates) > limit
        candidates = candidates[:limit]
        projected_terminal_statuses = {
            "COMPLETE",
            "COMPLETED",
            "FAILED",
            "CANCELED",
            "CANCELLED",
            "TERMINATED",
            "TIMED_OUT",
        }

        # Campaign names are the persisted operator-defined campaign identity. Per-run
        # uniqueness belongs to workflow_id, so project/name/run_type is the narrowest
        # stable identity available for legacy canaries.
        def canary_key(candidate: dict[str, Any]) -> tuple[str, str, str] | None:
            run_type = str(candidate.get("run_type") or "").strip().lower()
            name = str(candidate.get("name") or "").strip().lower()
            if "canary" not in name and "acceptance" not in run_type:
                return None
            return (str(candidate.get("project") or "").strip().lower(), name, run_type)

        def is_active_canary(candidate: dict[str, Any], temporal_status: str) -> bool:
            projection_status = str(candidate["status"])
            return (
                canary_key(candidate) is not None
                and projection_status not in projected_terminal_statuses
                and projection_status not in awaiting
                and projection_status not in external_wait
                and temporal_status in {"RUNNING", "CONTINUED_AS_NEW"}
            )

        observed_by_workflow: dict[str, dict[str, Any]] = {}
        missing_workflows: set[str] = set()
        workflow_ids = {
            workflow_id
            for candidate in candidates
            if isinstance((workflow_id := candidate.get("workflow_id")), str)
        }
        for workflow_id in workflow_ids:
            try:
                observed_by_workflow[workflow_id] = await service.get_workflow(workflow_id)
            except RPCError as error:
                if error.status == RPCStatusCode.NOT_FOUND:
                    missing_workflows.add(workflow_id)
                    continue
                raise HTTPException(
                    status_code=503, detail="Temporal visibility is temporarily unavailable"
                ) from error
            except Exception as error:  # noqa: BLE001 - boundary returns one fixed safe error
                raise HTTPException(
                    status_code=503, detail="Temporal visibility is temporarily unavailable"
                ) from error

        workflow_binding_counts: dict[str, int] = {}
        for candidate in candidates:
            workflow_id = candidate.get("workflow_id")
            if not isinstance(workflow_id, str) or workflow_id in missing_workflows:
                continue
            workflow_binding_counts[workflow_id] = workflow_binding_counts.get(workflow_id, 0) + 1

        active_canary_workflows: dict[tuple[str, str, str], set[str]] = {}
        for candidate in candidates:
            workflow_id = candidate.get("workflow_id")
            if (
                not isinstance(workflow_id, str)
                or workflow_id in missing_workflows
                or workflow_binding_counts.get(workflow_id, 0) != 1
            ):
                continue
            temporal_status = str(observed_by_workflow[workflow_id].get("status") or "UNKNOWN")
            if is_active_canary(candidate, temporal_status):
                key = canary_key(candidate)
                if key is not None:
                    active_canary_workflows.setdefault(key, set()).add(workflow_id)
        for candidate in candidates:
            workflow_id = candidate.get("workflow_id")
            if not isinstance(workflow_id, str):
                results.append({**candidate, "classification": "ORPHANED_CAMPAIGN"})
                continue
            if workflow_id in missing_workflows:
                results.append({**candidate, "classification": "TEMPORAL_WORKFLOW_MISSING"})
                continue
            observed = observed_by_workflow[workflow_id]
            temporal_status = str(observed.get("status") or "UNKNOWN")
            projected_terminal = candidate["status"] in projected_terminal_statuses
            candidate_canary_key = canary_key(candidate)
            if workflow_binding_counts.get(workflow_id, 0) > 1:
                changed = False
                classification = "INCONSISTENT_STATE"
            elif temporal_status in terminal and not projected_terminal:
                try:
                    changed = await repository.reconcile_terminal_campaign(
                        UUID(str(candidate["campaign_id"])), workflow_id, temporal_status
                    )
                except ValueError:
                    changed = False
                    classification = "INCONSISTENT_STATE"
                else:
                    classification = "TERMINAL_NOT_RECONCILED"
            elif temporal_status in {"RUNNING", "CONTINUED_AS_NEW"} and projected_terminal:
                changed = False
                classification = "INCONSISTENT_STATE"
            elif (
                is_active_canary(candidate, temporal_status)
                and candidate_canary_key is not None
                and len(active_canary_workflows.get(candidate_canary_key, set())) > 1
            ):
                changed = False
                classification = "DUPLICATE_CANARY"
            elif candidate["status"] in awaiting:
                changed = False
                classification = "AWAITING_HUMAN"
            elif candidate["status"] in external_wait:
                changed = False
                classification = "EXTERNAL_WAIT"
            elif candidate.get("blocker") == "WORKER_POLLER_MISSING":
                changed = False
                classification = "WORKER_POLLER_MISSING"
            elif (
                candidate["status"] in {"QUEUED", "PLANNED"}
                and int(candidate.get("age_seconds") or 0) > 900
            ):
                changed = False
                classification = "STALE_QUEUE"
            elif (
                temporal_status in {"RUNNING", "CONTINUED_AS_NEW"}
                and int(candidate.get("age_seconds") or 0) > 1800
            ):
                changed = False
                classification = "STALE_ACTIVITY"
            elif temporal_status in {"RUNNING", "CONTINUED_AS_NEW"}:
                changed = False
                classification = "RUNNING_HEALTHY"
            else:
                changed = False
                classification = "QUEUED_VALID"
            results.append(
                {
                    **candidate,
                    "temporal_status": temporal_status,
                    "classification": classification,
                    "projection_repaired": changed,
                }
            )
        return {
            "status": "COMPLETE",
            "items": results,
            "deleted": 0,
            "classification_scope": {
                "kind": "BOUNDED_PAGE",
                "offset": offset,
                "candidate_count": len(candidates),
                "canary_identity_fields": ["project", "name", "run_type"],
            },
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }

    @app.get("/v1/campaign-detail/{campaign_id}")
    async def campaign_detail_get(
        campaign_id: UUID,
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
        metadata_repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        backlog = await metadata_repository.get_backlog_campaign(campaign_id)
        if backlog is not None:
            return backlog
        campaign = await repository.campaign_detail(campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        return campaign

    @app.get("/v1/workflows/{workflow_id}/tail")
    async def workflow_tail_get(
        workflow_id: str,
        limit: int = Query(default=50, ge=1, le=100),
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        return {
            "workflow_id": workflow_id,
            "items": await repository.workflow_tail(workflow_id, limit),
            "next_cursor": None,
        }

    @app.get("/v1/backlogs/locate")
    async def backlog_issue_locate(
        project: str = Query(pattern=r"^(falcone|musematic|llmwiki)$"),
        surface: str = Query(pattern=r"^(source|deployment)$"),
        issue_number: int = Query(ge=1),
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        located = await repository.locate_backlog_issue(project, surface, issue_number)
        if located is not None:
            return located
        raise HTTPException(status_code=404, detail="issue is not in a current backlog inventory")

    @app.get("/v1/r3-issues/candidate")
    async def r3_issue_candidate_get(
        project: str = Query(pattern=r"^musematic$"),
        surface: str = Query(pattern=r"^source$"),
        issue_number: int = Query(ge=1119, le=1119),
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        try:
            return await repository.r3_issue_candidate_status(project, surface, issue_number)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/v1/r3-issues/candidate", status_code=201)
    async def r3_issue_candidate_create(
        project: str = Query(pattern=r"^musematic$"),
        surface: str = Query(pattern=r"^source$"),
        issue_number: int = Query(ge=1119, le=1119),
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        try:
            return await repository.r3_issue_candidate(project, surface, issue_number)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/r3-issues/approve")
    async def r3_issue_approve(
        body: R3IssueApprovalRequest,
        subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        try:
            return await repository.approve_r3_issue(
                body.project,
                body.surface,
                body.issue_number,
                body.binding,
                body.confirmation_phrase,
                canonical_operator_approval_actor(subject),
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

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

    @app.post("/v1/loop-plan")
    async def loop_plan_dispatch(
        body: LoopPlanDispatch,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any] | OperationAccepted:
        backlog = await repository.get_backlog_campaign(body.campaign_id)
        if backlog is not None:
            if backlog.get("project") != body.project.value:
                raise HTTPException(status_code=409, detail="backlog project binding changed")
            return backlog
        return await service.start_issue_loop(
            IssueLoopStart(
                project=body.project,
                campaign_id=body.campaign_id,
                maximum_risk=RiskClass.R1,
                maximum_items=1,
                execution_mode="plan-only",
                dry_run=True,
            )
        )

    @app.post("/v1/loop-start", response_model=OperationAccepted, status_code=202)
    async def loop_start_dispatch(
        body: LoopStartDispatch,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> OperationAccepted:
        backlog = await repository.get_backlog_campaign(body.campaign_id)
        if backlog is not None:
            if body.project.value not in {"falcone", "musematic", "llmwiki"}:
                raise HTTPException(status_code=409, detail="backlog project is not supported")
            if body.execution_mode not in {"source-pr", "full-delivery"}:
                raise HTTPException(status_code=409, detail="backlog delivery mode is invalid")
            return await service.start_existing_backlog_loop(
                ExistingBacklogLoopStart(
                    project=body.project.value,
                    backlog_campaign_id=body.campaign_id,
                    maximum_risk=body.maximum_risk,
                    delivery_mode=body.execution_mode,  # type: ignore[arg-type]
                    maximum_items=body.maximum_items,
                )
            )
        if body.execution_mode == "full-delivery":
            raise HTTPException(status_code=409, detail="full-delivery requires a backlog campaign")
        return await service.start_issue_loop(
            IssueLoopStart(
                project=body.project,
                campaign_id=body.campaign_id,
                maximum_risk=body.maximum_risk,
                maximum_items=body.maximum_items,
                execution_mode=body.execution_mode,
                dry_run=body.execution_mode == "plan-only",
            )
        )

    @app.post("/v1/backlogs/import", response_model=BacklogImportAccepted, status_code=202)
    async def backlog_import(
        body: GitHubBacklogImportRequest,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> BacklogImportAccepted:
        return await service.import_github_backlog(body)

    @app.get("/v1/backlogs", response_model=RecordList)
    async def backlog_list(
        project: str | None = Query(default=None, pattern=r"^(falcone|musematic|llmwiki)$"),
        limit: int = Query(default=20, ge=1, le=100),
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> RecordList:
        return RecordList(
            items=await repository.list_backlog_campaigns(project, limit), next_cursor=None
        )

    @app.get("/v1/backlogs/{campaign_id}")
    async def backlog_get(
        campaign_id: UUID,
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        value = await repository.get_backlog_campaign(campaign_id)
        if value is None:
            raise HTTPException(status_code=404, detail="backlog campaign not found")
        return value

    @app.get("/v1/backlogs/{campaign_id}/issues", response_model=RecordList)
    async def backlog_issue_list(
        campaign_id: UUID,
        limit: int = Query(default=20, ge=1, le=200),
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> RecordList:
        return RecordList(
            items=await repository.list_backlog_issues(campaign_id, limit), next_cursor=None
        )

    @app.get("/v1/backlogs/{campaign_id}/issues/{repository_short_name}/{issue_number}")
    async def backlog_issue_get(
        campaign_id: UUID,
        repository_short_name: str,
        issue_number: int,
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        if repository_short_name not in {
            "falcone",
            "falcone-charts",
            "musematic",
            "musematic-deploy",
            "llmwiki",
        }:
            raise HTTPException(status_code=404, detail="backlog repository not found")
        value = await repository.get_backlog_issue(
            campaign_id, f"gntik-ai/{repository_short_name}", issue_number
        )
        if value is None:
            raise HTTPException(status_code=404, detail="backlog issue not found")
        return value

    @app.post("/v1/backlogs/{campaign_id}/approval-candidate")
    async def backlog_approval_candidate(
        campaign_id: UUID,
        body: BacklogApprovalCandidateRequest,
        _subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        try:
            return await repository.prepare_backlog_approval(campaign_id, body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/backlogs/{campaign_id}/approve")
    async def backlog_approve(
        campaign_id: UUID,
        body: BacklogApprovalRequest,
        subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        try:
            return await repository.approve_backlog_campaign(
                campaign_id,
                body.project,
                body.approval_binding,
                canonical_operator_approval_actor(subject),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/v1/backlogs/{campaign_id}/start",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def backlog_loop_start(
        campaign_id: UUID,
        body: ExistingBacklogLoopStart,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        if body.backlog_campaign_id != campaign_id:
            raise HTTPException(status_code=409, detail="backlog campaign binding changed")
        try:
            return await service.start_existing_backlog_loop(body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/backlog-loops/{workflow_id}/{action}", status_code=202)
    async def backlog_loop_control(
        workflow_id: str,
        action: str,
        body: BacklogIssueAction | None = None,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, str]:
        if action in {"pause", "resume", "cancel"}:
            await service.signal(workflow_id, action)
            return {"status": "ACCEPTED"}
        if action == "skip" and body is not None and body.reason_code is not None:
            await service.signal(
                workflow_id,
                "skip",
                f"gntik-ai/{body.repository}#{body.issue_number}|{body.reason_code}",
            )
            return {"status": "ACCEPTED"}
        if action == "retry" and body is not None and body.reason_code is None:
            await service.signal(
                workflow_id,
                "retry",
                f"gntik-ai/{body.repository}#{body.issue_number}",
            )
            return {"status": "ACCEPTED"}
        raise HTTPException(status_code=404, detail="unknown backlog loop action")

    @app.post("/v1/backlog-loops/{workflow_id}/approve-delivery")
    async def backlog_delivery_approve(
        workflow_id: str,
        body: BacklogDeliveryApprovalRequest,
        _subject: str = auth_dependency,
    ) -> dict[str, str]:
        raise HTTPException(
            status_code=409,
            detail=(
                "staging approval candidate is not yet available; the campaign remains "
                f"bound to workflow {workflow_id}"
            ),
        )

    @app.get("/v1/backlog-loops/{workflow_id}/delivery-candidate")
    async def backlog_delivery_candidate(
        workflow_id: str,
        repository: str = Query(
            pattern=r"^(falcone|falcone-charts|musematic|musematic-deploy|llmwiki)$"
        ),
        issue_number: int = Query(ge=1),
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, Any]:
        state = await service.get_workflow(workflow_id)
        return {
            "status": "BLOCKED_BY_DELIVERY",
            "workflow_id": workflow_id,
            "repository": f"gntik-ai/{repository}",
            "issue_number": issue_number,
            "blocker": "ISSUE_HAS_NOT_REACHED_AWAITING_STAGING_DELIVERY",
            "workflow_state": state,
        }

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
                enhancement_id,
                body.approval_binding,
                canonical_operator_approval_actor(subject),
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
            return await repository.reject_enhancement(
                enhancement_id,
                body.reason,
                canonical_operator_approval_actor(subject),
            )
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
        try:
            return await service.start_delivery(body)
        except ValueError as error:
            raise HTTPException(
                status_code=409,
                detail="delivery approval or finding target is unavailable or changed",
            ) from error

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
            await service.record_approval(body, canonical_operator_approval_actor(subject))
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

    @app.post("/v1/observability/gateway-events", status_code=202)
    async def gateway_event(
        request: Request,
        body: GatewayEventInput,
        subject: str = auth_dependency,
    ) -> dict[str, object]:
        if subject != "hermes-gateway":
            raise HTTPException(status_code=403, detail="gateway event identity required")
        try:
            return await gateway_events(request).gateway_event(subject, body)
        except GatewayEventConflict as error:
            raise HTTPException(
                status_code=409, detail="gateway event identity conflict"
            ) from error

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

    @app.post(
        "/v1/validation/observability",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def validation_observability(
        body: ObservabilityAcceptanceStart,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        if not resolved.acceptance_enabled:
            raise HTTPException(status_code=404, detail="synthetic validation is disabled")
        try:
            return await service.start_observability_acceptance(body)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post(
        "/v1/validation/observability/reverse",
        response_model=OperationAccepted,
        status_code=202,
    )
    async def validation_observability_reverse(
        body: ObservabilityAcceptanceStart,
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
    ) -> OperationAccepted:
        if not resolved.acceptance_enabled:
            raise HTTPException(status_code=404, detail="synthetic validation is disabled")
        try:
            return await service.start_reverse_observability_acceptance(body)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

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
