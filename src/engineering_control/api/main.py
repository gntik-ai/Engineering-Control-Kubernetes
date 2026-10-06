from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
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
    BacklogImportAccepted,
    BacklogIssueAction,
    BacklogOwnershipReleaseRequest,
    CampaignReconciliationApply,
    CampaignStart,
    ChangeRequestAccepted,
    ChangeRequestApproval,
    ChangeRequestCreate,
    ChangeRequestPublication,
    ChangeRequestRejection,
    ChangeRequestRevision,
    CorrectionExtensionRequest,
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
    IssueAmendRequest,
    IssueLoopStart,
    IssueReapprovalRequest,
    LLMWikiBootstrapExecuteRequest,
    LLMWikiBootstrapPlanRequest,
    LoopPlanDispatch,
    LoopStartDispatch,
    ObservabilityAcceptanceStart,
    OperationAccepted,
    PublicationReconcileRequest,
    R3IssueApprovalRequest,
    RecordList,
    RollbackRequest,
    RuntimeVerificationStart,
    UiBlueprintApproval,
    UiPlanAccepted,
    UiPlanCreate,
)
from engineering_control.api.service import ControlService, TemporalControlService
from engineering_control.config import Settings, get_settings
from engineering_control.db.change_requests import (
    ChangeRequestNotFoundError,
    ChangeRequestRepository,
)
from engineering_control.db.repository import (
    EmptyMetadataRepository,
    MetadataRepository,
    SQLMetadataRepository,
)
from engineering_control.db.ui_blueprints import (
    UiBlueprintNotFoundError,
    UiBlueprintRepository,
)
from engineering_control.engineering.backlog import canonical_operator_approval_actor
from engineering_control.engineering.change_requests import prepare_intake
from engineering_control.logging import configure_logging
from engineering_control.models.enums import Project, RiskClass
from engineering_control.observability.agent import (
    AgentObservabilityRepository,
    GatewayEventConflict,
    GatewayEventInput,
)
from engineering_control.observability.metrics import ControlPlaneMetrics
from engineering_control.operations.connection_status import provider_observation_current
from engineering_control.operations.issue_start import IssueStartUnconfirmedError
from engineering_control.operations.models import WorkflowControlCommand, parse_command
from engineering_control.operations.reconciliation import decide_reconciliation, observation_token
from engineering_control.operations.recurring_reconciliation import RecurringRunReconciler
from engineering_control.operations.repository import OperationsRepository
from engineering_control.operations.run_start import RecordedRunStartUnconfirmedError
from engineering_control.operations.service import TypedCommandBus, operation_dispatch_loop

# NEW-123 review 2: how long a claimed but not yet started fan-out counts as starting.
UI_FANOUT_START_GRACE = timedelta(minutes=5)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def create_app(
    *,
    settings: Settings | None = None,
    service: ControlService | None = None,
    repository: MetadataRepository | None = None,
    token: str | None = None,
    gateway_repository: AgentObservabilityRepository | None = None,
    operations_repository: OperationsRepository | None = None,
    command_bus: TypedCommandBus | None = None,
    change_request_repository: ChangeRequestRepository | None = None,
    ui_blueprint_repository: UiBlueprintRepository | None = None,
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
                projection_reconciliation_enabled=resolved.projection_reconciliation_enabled,
                projection_reconciliation_interval_seconds=resolved.projection_reconciliation_interval_seconds,
                projection_reconciliation_cycle_seconds=resolved.projection_reconciliation_cycle_seconds,
                projection_reconciliation_max_sweep_age_seconds=resolved.projection_reconciliation_max_sweep_age_seconds,
            )
        app.state.operations_repository = resolved_operations
        resolved_bus = command_bus
        if resolved_bus is None and resolved_operations is not None:
            resolved_bus = TypedCommandBus(
                cast(ControlService, app.state.control_service),
                resolved_repository,
                resolved_operations,
                diagnostics_only=admission_mode == "diagnostics-only",
            )
        if resolved_bus is not None:
            # The startup boundary also applies to an explicitly supplied bus.
            resolved_bus.diagnostics_only = admission_mode == "diagnostics-only"
        app.state.command_bus = resolved_bus
        resolved_change_requests = change_request_repository
        if resolved_change_requests is None and service is None:
            resolved_change_requests = ChangeRequestRepository(
                resolved.database_url.get_secret_value()
            )
        app.state.change_request_repository = resolved_change_requests
        resolved_ui_blueprints = ui_blueprint_repository
        if resolved_ui_blueprints is None and service is None:
            resolved_ui_blueprints = UiBlueprintRepository(resolved.database_url.get_secret_value())
        app.state.ui_blueprint_repository = resolved_ui_blueprints
        stop = asyncio.Event()
        reconciler = None
        if resolved.projection_reconciliation_enabled and admission_mode in {
            "normal",
            "diagnostics-only",
        }:
            if resolved_operations is None:
                raise RuntimeError("enabled reconciliation requires an operations repository")
            reconciler = RecurringRunReconciler(
                app.state.control_service,
                resolved_operations,
                resolved.temporal_namespace,
                interval_seconds=resolved.projection_reconciliation_interval_seconds,
                batch_size=resolved.projection_reconciliation_batch_size,
                cycle_seconds=resolved.projection_reconciliation_cycle_seconds,
                max_sweep_age_seconds=resolved.projection_reconciliation_max_sweep_age_seconds,
            )
        dispatcher = (
            asyncio.create_task(operation_dispatch_loop(resolved_bus, stop))
            if resolved_bus is not None and admission_mode in {"normal", "diagnostics-only"}
            else None
        )
        app.state.operation_dispatcher = dispatcher
        reconciliation = (
            asyncio.create_task(reconciler.serve(stop), name="projection-reconciliation")
            if reconciler is not None
            else None
        )
        app.state.projection_reconciler = reconciliation
        try:
            yield
        finally:
            stop.set()
            # Every SQL operation is joined before the reconciler releases its
            # leadership lock; engines remain alive until all owned tasks stop.
            tasks = [task for task in (dispatcher, reconciliation) if task is not None]
            if tasks:
                await asyncio.gather(*tasks)
            if gateway_repository is None:
                cast(AgentObservabilityRepository, app.state.gateway_repository).engine.dispose()
            if operations_repository is None and resolved_operations is not None:
                resolved_operations.engine.dispose()
            if change_request_repository is None and resolved_change_requests is not None:
                resolved_change_requests.engine.dispose()
            if ui_blueprint_repository is None and resolved_ui_blueprints is not None:
                resolved_ui_blueprints.engine.dispose()

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

    @app.exception_handler(RecordedRunStartUnconfirmedError)
    async def recorded_run_start_unconfirmed(
        _request: Request, error: RecordedRunStartUnconfirmedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={
                "status": "START_UNCONFIRMED",
                "application_run_id": str(error.application_run_id),
                "expected_workflow_id": error.workflow_id,
                "detail": (
                    "Start acknowledgement is unconfirmed; preserve this Run and inspect "
                    "its status. Do not start a replacement workflow."
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

    def change_requests(request: Request) -> ChangeRequestRepository:
        value = request.app.state.change_request_repository
        if value is None:
            raise HTTPException(status_code=503, detail="change request repository unavailable")
        return cast(ChangeRequestRepository, value)

    def ui_blueprints(request: Request) -> UiBlueprintRepository:
        value = request.app.state.ui_blueprint_repository
        if value is None:
            raise HTTPException(status_code=503, detail="ui blueprint repository unavailable")
        return cast(UiBlueprintRepository, value)

    control_dependency = Depends(control)
    change_requests_dependency = Depends(change_requests)
    ui_blueprints_dependency = Depends(ui_blueprints)
    metadata_dependency = Depends(metadata)
    operations_dependency = Depends(operations)
    commands_dependency = Depends(commands)
    control_metrics = ControlPlaneMetrics()

    def approval_actor(subject: str) -> str:
        """Authorize a raw API channel subject and return its durable actor."""
        try:
            return canonical_operator_approval_actor(subject)
        except ValueError:
            raise HTTPException(status_code=403, detail="approval channel is not allowed") from None

    @app.post("/v1/commands", status_code=202)
    async def typed_command(
        body: dict[str, Any],
        subject: str = auth_dependency,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=16, max_length=128),
        bus: TypedCommandBus = commands_dependency,
    ) -> dict[str, Any]:
        # This private HTTP ingress is used by the fixed Telegram hook and the
        # hermes-owned CLI.  The dashboard invokes TypedCommandBus in-process
        # only after password reauthentication, so it cannot self-assert an
        # HTTP actor header here.
        durable_actor = approval_actor(subject)
        try:
            command = parse_command(body)
            source_channel = "telegram" if durable_actor.startswith("telegram:") else "cli"
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

    @app.get("/v1/campaigns/reconcile")
    @app.post("/v1/campaigns/reconcile")
    async def campaigns_reconcile(
        request: Request,
        confirmation: CampaignReconciliationApply | None = None,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=10_000),
        _subject: str = auth_dependency,
        service: ControlService = control_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        if request.method == "POST" and confirmation is None:
            raise HTTPException(
                status_code=400, detail="Preview and confirm exact reconciliation rows first"
            )
        selected = (
            {
                str(row.campaign_id): (row.snapshot_token, row.observation_token)
                for row in confirmation.rows
            }
            if request.method == "POST" and confirmation is not None
            else {}
        )
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
        deadline = asyncio.get_running_loop().time() + 20
        for workflow_id in workflow_ids:
            try:
                async with asyncio.timeout_at(deadline):
                    observed_by_workflow[
                        workflow_id
                    ] = await service.get_reconciliation_observation(workflow_id)
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
            elif temporal_status in terminal:
                decision = decide_reconciliation(candidate, observed)
                changed = False
                classification = decision.classification
                campaign_key = str(candidate["campaign_id"])
                if campaign_key in selected:
                    if selected[campaign_key] != (
                        candidate.get("snapshot_token"),
                        observation_token(observed),
                    ):
                        classification = "PREVIEW_STALE"
                    elif decision.classification == "TERMINAL_REPAIR_AVAILABLE":
                        try:
                            changed = await repository.reconcile_terminal_campaign(
                                UUID(campaign_key),
                                workflow_id,
                                temporal_status,
                                expected_snapshot=selected[campaign_key][0],
                                observed=observed,
                            )
                        except ValueError:
                            classification = "PREVIEW_STALE"
                        except SQLAlchemyError as error:
                            raise HTTPException(
                                status_code=503,
                                detail=(
                                    "Reconciliation transaction was not accepted; "
                                    "refresh the preview"
                                ),
                            ) from error
                        else:
                            classification = (
                                "TERMINAL_RECONCILED" if changed else "REPAIR_NOT_APPLIED"
                            )
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
                    "observation_token": observation_token(observed),
                    "observed_run_id": observed.get("run_id"),
                    "classification": classification,
                    "projection_repaired": changed,
                }
            )
        return {
            "status": "PREVIEW" if request.method == "GET" else "RECONCILIATION_EVALUATED",
            "items": results,
            "deleted": 0,
            "unobserved_targets": sorted(
                set(selected) - {str(row["campaign_id"]) for row in candidates}
            ),
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
        project: str = Query(pattern=r"^(falcone|musematic|llmwiki)$"),
        surface: str = Query(pattern=r"^(source|deployment)$"),
        issue_number: int = Query(ge=1),
        _subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
    ) -> dict[str, Any]:
        try:
            return await repository.r3_issue_candidate_status(project, surface, issue_number)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/v1/r3-issues/candidate", status_code=201)
    async def r3_issue_candidate_create(
        project: str = Query(pattern=r"^(falcone|musematic|llmwiki)$"),
        surface: str = Query(pattern=r"^(source|deployment)$"),
        issue_number: int = Query(ge=1),
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
                approval_actor(subject),
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/change-sets/extend-correction")
    async def change_set_extend_correction(
        body: CorrectionExtensionRequest,
        subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, Any]:
        """NEW-133: grant one extra correction attempt to a validation-blocked ChangeSet.

        Same trust model as /v1/r3-issues/approve: the bearer-authenticated channel asserts
        the actor, which must be a canonical operator. Every precondition is re-read from
        durable records; the operator then resumes the loop with backlog-retry."""
        actor = approval_actor(subject)
        # NEW-133 review 3: the loop's current Temporal run, which must still be open; the
        # pause the grant relies on must have been recorded by that run.
        current_run_id = await service.current_open_run_id(body.loop_workflow_id)
        try:
            return await repository.extend_change_set_correction(
                body.loop_workflow_id,
                body.project,
                body.issue_number,
                body.change_set_id,
                body.confirmation_phrase,
                actor,
                current_run_id,
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/issues/reapprove")
    async def issue_reapprove(
        body: IssueReapprovalRequest,
        subject: str = auth_dependency,
        bus: TypedCommandBus = commands_dependency,
    ) -> dict[str, Any]:
        """NEW-143: re-create the voided per-issue campaign approval of an issue loop.

        Same trust model as /v1/r3-issues/approve: the bearer-authenticated channel asserts
        the actor, which must be a canonical operator. The loop's request is rebuilt from
        durable records; the operator then resumes the loop with backlog-retry."""
        actor = approval_actor(subject)
        try:
            return await bus.reapprove_issue(
                body.loop_workflow_id,
                body.project,
                body.repository,
                body.issue_number,
                body.confirmation_phrase,
                actor,
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/publication-effects/reconcile")
    async def publication_effect_reconcile(
        body: PublicationReconcileRequest,
        subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
        service: ControlService = control_dependency,
    ) -> dict[str, Any]:
        """NEW-145: resolve an uncertain publication effect by authority.

        Same trust model as /v1/r3-issues/approve. The bound PRs' live heads are read by the
        GitHub worker and must equal the heads recorded at the last confirmed publication;
        every other precondition is re-read from durable records."""
        actor = approval_actor(subject)
        try:
            plan = await repository.plan_publication_reconcile(
                body.effect_id, body.confirmation_phrase, actor
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if plan["status"] == "ALREADY_RECONCILED":
            return plan
        try:
            observed = await service.observe_pull_request_heads(plan["pull_request_urls"])
        except Exception as error:  # noqa: BLE001 - one bounded operator-facing failure
            raise HTTPException(
                status_code=503,
                detail="PUBLICATION_RECONCILE_PR_HEADS_UNAVAILABLE: the pull requests could "
                "not be read; try again",
            ) from error
        try:
            return await repository.reconcile_publication_effect(
                body.effect_id, body.confirmation_phrase, actor, plan, observed
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/issues/amend")
    async def issue_amend(
        body: IssueAmendRequest,
        subject: str = auth_dependency,
        repository: OperationsRepository = operations_dependency,
        service: ControlService = control_dependency,
        bus: TypedCommandBus = commands_dependency,
    ) -> dict[str, Any]:
        """NEW-115: re-approve an issue body edited after delivery approval.

        Without ``confirm`` this is a read-only preview; with it the freshly read body hash
        must still start with the confirmation, then the approved binding is updated."""
        # Same trust model as /v1/r3-issues/approve and the other operator approvals: the
        # bearer-authenticated channel asserts the actor, which must be a canonical operator.
        actor = approval_actor(subject)
        try:
            projected = await repository.issue_amend_precheck(
                body.project, body.repository, body.issue_number
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        # Runs projected RUNNING (e.g. stopped at the body refresh before NEW-115, which
        # release_leases never terminalized) block unless Temporal proves them closed.
        closed: set[Any] = set()
        for run_id, workflow_id in projected:
            try:
                if await service.workflow_id_closed(workflow_id):
                    closed.add(run_id)
                    continue
            except Exception:  # noqa: BLE001,S110 - unverifiable stays active (fail closed)
                pass
            raise HTTPException(
                status_code=409,
                detail="ISSUE_AMEND_DELIVERY_ACTIVE: a delivery of this issue is running "
                f"({workflow_id}); amend it after it stops",
            )
        try:
            observation = await service.observe_issue_body(
                f"gntik-ai/{body.repository}", body.issue_number
            )
        except Exception as error:  # noqa: BLE001 - one bounded operator-facing failure
            raise HTTPException(
                status_code=503,
                detail="ISSUE_AMEND_BODY_UNAVAILABLE: the GitHub issue could not be read; "
                "try again",
            ) from error
        try:
            result = await repository.amend_issue(
                body.project,
                body.repository,
                body.issue_number,
                observation,
                body.confirm,
                actor,
                frozenset(closed),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        # The amendment and its audit event are committed before the retry is sent. A rerun
        # of the same confirmation after an unknown outcome reports NO_CHANGE for the
        # recorded amendment and still sends the requested (validated) retry.
        if body.retry and (
            result.get("status") == "AMENDED"
            or (result.get("status") == "NO_CHANGE" and "retry_workflow_id" in result)
        ):
            result["retry"] = await _amend_retry(bus, body, result, subject, actor)
        return result

    async def _amend_retry(
        bus: TypedCommandBus,
        body: IssueAmendRequest,
        result: dict[str, Any],
        subject: str,
        actor: str,
    ) -> dict[str, Any]:
        """Send the existing typed backlog retry (with its preflight) for the amended issue."""
        workflow_id = result.get("retry_workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id:
            return {
                "status": "RETRY_NOT_SENT",
                "blocker": "BACKLOG_LOOP_NOT_FOUND",
                "detail": "no backlog loop delivery of this issue is recorded; retry it with "
                "/loop retry <workflow-id> <repository> <issue>",
            }
        try:
            command = WorkflowControlCommand(
                action="retry",
                workflow_id=workflow_id,
                repository=body.repository,
                issue_number=body.issue_number,
            )
            view = await bus.submit(
                command,
                source_channel="telegram" if actor.startswith("telegram:") else "cli",
                operator_class=subject,
                # One key per request: a rerun may need to resend, and the loop's own
                # preflight refuses a retry for an issue it is no longer waiting on.
                idempotency_key=(
                    f"issue-amend-retry-{str(result.get('retry_key') or '')[:32]}-"
                    f"{uuid4().hex[:16]}"
                ),
            )
        except ValueError as error:
            return {"status": "RETRY_NOT_SENT", "detail": str(error)[:500]}
        return view.model_dump(mode="json")

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
                approval_actor(subject),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/backlogs/{campaign_id}/release-ownership")
    async def backlog_release_ownership(
        campaign_id: UUID,
        body: BacklogOwnershipReleaseRequest,
        subject: str = auth_dependency,
        repository: MetadataRepository = metadata_dependency,
    ) -> dict[str, Any]:
        try:
            return await repository.release_backlog_ownership(
                campaign_id,
                body.project,
                body.typed_confirmation,
                approval_actor(subject),
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
        if action in {"skip", "retry"} and body is not None:
            # NEW-114: an issue the loop is not waiting on is refused, not silently ignored.
            try:
                await service.validate_backlog_issue_signal(
                    workflow_id, f"gntik-ai/{body.repository}#{body.issue_number}"
                )
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
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
                approval_actor(subject),
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
                approval_actor(subject),
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

    @app.post("/v1/change-requests", response_model=ChangeRequestAccepted, status_code=202)
    async def change_request_create(
        body: ChangeRequestCreate,
        subject: str = auth_dependency,
        service: ControlService = control_dependency,
        repository: ChangeRequestRepository = change_requests_dependency,
    ) -> ChangeRequestAccepted:
        actor = approval_actor(subject)
        try:
            intake = prepare_intake(body.project, body.description, body.target_repository)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        created = await repository.create(intake, actor)
        change_request_id = UUID(str(created["id"]))
        try:
            accepted = await service.start_change_request(change_request_id, 1)
        except Exception as error:  # noqa: BLE001 - surfaced as a durable FAILED row
            await repository.record_start_failure(change_request_id, 1)
            raise HTTPException(
                status_code=503, detail="change request workflow could not be started"
            ) from error
        return ChangeRequestAccepted(
            workflow_id=accepted.workflow_id,
            run_id=accepted.run_id,
            change_request_id=change_request_id,
            request_key=str(created["request_key"]),
            revision=1,
            redacted=intake.redacted,
        )

    # NEW-123: UI migration flow A -- the blueprint, its approval and the draft fan-out.
    @app.post("/v1/ui-blueprints", response_model=UiPlanAccepted, status_code=202)
    async def ui_plan_create(
        body: UiPlanCreate,
        subject: str = auth_dependency,
        service: ControlService = control_dependency,
        repository: UiBlueprintRepository = ui_blueprints_dependency,
    ) -> UiPlanAccepted:
        actor = approval_actor(subject)
        try:
            created = await repository.create(body.project, actor, body.source_sha)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        blueprint_id = UUID(str(created["id"]))
        try:
            accepted = await service.start_ui_plan(
                blueprint_id, body.project, actor, body.source_sha
            )
        except Exception as error:  # noqa: BLE001 - surfaced as a durable FAILED row
            await repository.record_start_failure(blueprint_id)
            raise HTTPException(
                status_code=503, detail="ui plan workflow could not be started"
            ) from error
        return UiPlanAccepted(
            workflow_id=accepted.workflow_id, run_id=accepted.run_id, blueprint_id=blueprint_id
        )

    @app.get("/v1/ui-blueprints", response_model=RecordList)
    async def ui_blueprint_list(
        project: str | None = Query(default=None, pattern=r"^(falcone|musematic|llmwiki)$"),
        status: str | None = Query(
            default=None,
            pattern=r"^(DRAFTING|BLOCKED|FAILED|CANDIDATE|APPROVED|SUPERSEDED)$",
        ),
        limit: int = Query(default=20, ge=1, le=200),
        _subject: str = auth_dependency,
        repository: UiBlueprintRepository = ui_blueprints_dependency,
    ) -> RecordList:
        return RecordList(
            items=await repository.list_blueprints(project, status, limit), next_cursor=None
        )

    @app.get("/v1/ui-blueprints/{blueprint_id}")
    async def ui_blueprint_get(
        blueprint_id: UUID,
        _subject: str = auth_dependency,
        repository: UiBlueprintRepository = ui_blueprints_dependency,
    ) -> dict[str, Any]:
        value = await repository.get(blueprint_id)
        if value is None:
            raise HTTPException(status_code=404, detail="ui blueprint not found")
        return value

    @app.get("/v1/ui-blueprints/{blueprint_id}/markdown")
    async def ui_blueprint_markdown(
        blueprint_id: UUID,
        _subject: str = auth_dependency,
        repository: UiBlueprintRepository = ui_blueprints_dependency,
    ) -> dict[str, Any]:
        try:
            markdown = await repository.markdown(blueprint_id)
        except UiBlueprintNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if markdown is None:
            raise HTTPException(status_code=409, detail="ui blueprint has no draft yet")
        view = await repository.get(blueprint_id)
        # NEW-131: the per-call token usage of the batched drafting, as totals.
        usage = (view or {}).get("usage") or {}
        return {"blueprint_id": str(blueprint_id), "markdown": markdown, "usage": usage}

    @app.post("/v1/ui-blueprints/{blueprint_id}/approve")
    async def ui_blueprint_approve(
        blueprint_id: UUID,
        body: UiBlueprintApproval,
        subject: str = auth_dependency,
        service: ControlService = control_dependency,
        repository: UiBlueprintRepository = ui_blueprints_dependency,
    ) -> dict[str, Any]:
        actor = approval_actor(subject)
        try:
            approved = await repository.approve(
                blueprint_id, body.approval_binding, body.typed_confirmation, actor
            )
        except UiBlueprintNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        limit = resolved.ui_blueprint_fanout_max_items
        max_items = min(body.max_items or limit, limit)
        try:
            accepted, joined = await ui_fanout_start_or_join(
                blueprint_id, max_items, service, repository
            )
        except Exception as error:  # noqa: BLE001 - the approval stands; a replay resumes
            raise HTTPException(
                status_code=503,
                detail="ui blueprint approved, but its fan-out could not be started; "
                "repeat the approval to resume",
            ) from error
        # Hermes drafts the change requests only; the operator publishes each one.
        return {
            **approved,
            "fanout": {
                "workflow_id": accepted.workflow_id,
                "run_id": accepted.run_id,
                "max_items": max_items,
                "joined": joined,
            },
        }

    async def ui_fanout_start_or_join(
        blueprint_id: UUID,
        max_items: int,
        service: ControlService,
        repository: UiBlueprintRepository,
    ) -> tuple[OperationAccepted, bool]:
        """One fan-out per blueprint: join the active one, or win the claim and start.

        A marker whose workflow is no longer running (it ended without clearing it) is
        released once and the claim retried; a failed start keeps its marker, so the next
        approval checks that workflow before anything else starts."""
        for _ in range(2):
            claim = await repository.claim_fanout(blueprint_id)
            if claim["action"] == "START":
                started = await service.start_ui_fanout(
                    blueprint_id, max_items, str(claim["workflow_id"])
                )
                return started, False
            state, running = await service.ui_fanout_state(str(claim["workflow_id"]))
            if state == "RUNNING" and running is not None:
                return running, True
            # NEW-123 review 2/3: a marker whose workflow Temporal does not know yet is a
            # start in progress (another approval won the claim and is starting it), not an
            # ended fan-out: join it during a grace period; only a stale claim (a start that
            # crashed) or a CLOSED execution is released.
            claimed_at = claim.get("claimed_at")
            if (
                state == "NOT_FOUND"
                and isinstance(claimed_at, datetime)
                and datetime.now(UTC) - _as_utc(claimed_at) < UI_FANOUT_START_GRACE
            ):
                return OperationAccepted(workflow_id=str(claim["workflow_id"]), run_id=""), True
            await repository.release_fanout(blueprint_id, str(claim["workflow_id"]))
        raise RuntimeError("ui fan-out claim did not settle")

    @app.get("/v1/change-requests", response_model=RecordList)
    async def change_request_list(
        project: str | None = Query(default=None, pattern=r"^(falcone|musematic|llmwiki)$"),
        status: str | None = Query(
            default=None,
            pattern=r"^(DRAFTING|CANDIDATE|APPROVED|PUBLISHED|REJECTED|FAILED)$",
        ),
        limit: int = Query(default=20, ge=1, le=200),
        _subject: str = auth_dependency,
        repository: ChangeRequestRepository = change_requests_dependency,
    ) -> RecordList:
        return RecordList(
            items=await repository.list_requests(project, status, limit), next_cursor=None
        )

    @app.get("/v1/change-requests/{change_request_id}")
    async def change_request_get(
        change_request_id: UUID,
        _subject: str = auth_dependency,
        repository: ChangeRequestRepository = change_requests_dependency,
    ) -> dict[str, Any]:
        value = await repository.get(change_request_id)
        if value is None:
            raise HTTPException(status_code=404, detail="change request not found")
        return value

    @app.post("/v1/change-requests/{change_request_id}/approve")
    async def change_request_approve(
        change_request_id: UUID,
        body: ChangeRequestApproval,
        subject: str = auth_dependency,
        repository: ChangeRequestRepository = change_requests_dependency,
    ) -> dict[str, Any]:
        actor = approval_actor(subject)
        try:
            approved = await repository.approve(
                change_request_id, body.approval_binding, body.typed_confirmation, actor
            )
        except ChangeRequestNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        # Hermes never publishes: the operator runs `engineering-ctl change-publish` next.
        return approved

    @app.post(
        "/v1/change-requests/{change_request_id}/revise",
        response_model=ChangeRequestAccepted,
        status_code=202,
    )
    async def change_request_revise(
        change_request_id: UUID,
        body: ChangeRequestRevision,
        subject: str = auth_dependency,
        service: ControlService = control_dependency,
        repository: ChangeRequestRepository = change_requests_dependency,
    ) -> ChangeRequestAccepted:
        actor = approval_actor(subject)
        try:
            revised = await repository.revise(change_request_id, body.note, actor)
        except ChangeRequestNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        revision = int(revised["revision"])
        try:
            accepted = await service.start_change_request(change_request_id, revision)
        except Exception as error:  # noqa: BLE001 - surfaced as a durable FAILED row
            await repository.record_start_failure(change_request_id, revision)
            raise HTTPException(
                status_code=503, detail="change request workflow could not be started"
            ) from error
        return ChangeRequestAccepted(
            workflow_id=accepted.workflow_id,
            run_id=accepted.run_id,
            change_request_id=change_request_id,
            request_key=str(revised["request_key"]),
            revision=revision,
            redacted=bool(revised["redacted"]),
        )

    @app.post("/v1/change-requests/{change_request_id}/reject")
    async def change_request_reject(
        change_request_id: UUID,
        body: ChangeRequestRejection,
        subject: str = auth_dependency,
        repository: ChangeRequestRepository = change_requests_dependency,
    ) -> dict[str, Any]:
        actor = approval_actor(subject)
        try:
            return await repository.reject(change_request_id, body.reason, actor)
        except ChangeRequestNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/change-requests/{change_request_id}/published")
    async def change_request_published(
        change_request_id: UUID,
        body: ChangeRequestPublication,
        subject: str = auth_dependency,
        repository: ChangeRequestRepository = change_requests_dependency,
    ) -> dict[str, Any]:
        actor = approval_actor(subject)
        try:
            return await repository.mark_published(
                change_request_id,
                body.issue_url,
                body.approval_binding,
                body.decision_sequence,
                actor,
            )
        except ChangeRequestNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

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
            await service.record_approval(body, approval_actor(subject))
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
            value["raw_status"] = value["status"]
            observed = value.get("captured_at")
            try:
                captured = (
                    observed
                    if isinstance(observed, datetime)
                    else datetime.fromisoformat(str(observed))
                )
                current = provider_observation_current(captured, datetime.now(UTC))
            except (TypeError, ValueError, OverflowError):
                current = False
            if provider in snapshots and not current:
                value["status"] = "STALE"
            value["capacity_status"] = (
                "UNKNOWN"
                if value.get("total_capacity") is None
                else "STALE"
                if not current
                else value["raw_status"]
            )
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
