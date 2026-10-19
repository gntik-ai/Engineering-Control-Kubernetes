from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TypeVar, cast
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from engineering_control.api.auth import identity_bearer_auth
from engineering_control.config import Settings, get_settings
from engineering_control.db.repository import MetadataRepository, SQLMetadataRepository
from engineering_control.evidence.store import LocalEvidenceStore
from engineering_control.evidence.transfer import EvidenceEnvelope, EvidenceIngestService
from engineering_control.logging import configure_logging
from engineering_control.models.publication_authority import (
    PublicationEffectClaim,
    PublicationEffectReceipt,
)
from engineering_control.observability.agent import (
    AgentObservabilityRepository,
    ExecutionFinishInput,
    ExecutionHeartbeatInput,
    ExecutionStartInput,
    GatewayEventInput,
    LogBatchInput,
    ObservabilityModel,
)
from engineering_control.operations.issue_delivery_approval import CampaignApprovalLapse
from engineering_control.operations.publication_authority import (
    PublicationAuthorityRepository,
    PublicationEffectUnresolved,
)

ObservabilityValue = TypeVar("ObservabilityValue", bound=ObservabilityModel)


def create_evidence_ingest_app(
    *,
    settings: Settings | None = None,
    repository: MetadataRepository | None = None,
    observability_repository: AgentObservabilityRepository | None = None,
    tokens: dict[str, str] | None = None,
    publication_authority: PublicationAuthorityRepository | None = None,
) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.metadata_repository = repository or SQLMetadataRepository(
            resolved.database_url.get_secret_value()
        )
        app.state.observability_repository = (
            observability_repository
            or AgentObservabilityRepository(
                resolved.database_url.get_secret_value(),
                maximum_transcript_bytes=resolved.dashboard_transcript_max_bytes,
            )
        )
        authority = publication_authority or PublicationAuthorityRepository(
            resolved.database_url.get_secret_value()
        )
        app.state.publication_authority = authority
        try:
            yield
        finally:
            if publication_authority is None:
                authority.engine.dispose()

    app = FastAPI(
        title="Engineering Evidence Ingest",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    expected_tokens = tokens if tokens is not None else resolved.read_evidence_ingest_tokens()
    authenticated = identity_bearer_auth(expected_tokens)

    async def publication_body(request: Request, subject: str) -> bytes:
        if subject not in {"delivery-runner", "github-runner"}:
            raise HTTPException(status_code=403, detail="publication identity denied")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise HTTPException(status_code=413, detail="publication request exceeds bound")
        return bytes(body)

    @app.post("/v1/publications/claim")
    async def claim_publication(
        request: Request, subject: str = Depends(authenticated)
    ) -> dict[str, Any]:
        body = await publication_body(request, subject)
        try:
            values = PublicationEffectClaim.model_validate_json(body)
            authority = cast(
                PublicationAuthorityRepository, request.app.state.publication_authority
            )
            return await asyncio.to_thread(authority.claim, subject, values)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail="invalid publication request") from error
        except PublicationEffectUnresolved as error:
            raise HTTPException(
                status_code=409, detail={"code": "PUBLICATION_EFFECT_UNRESOLVED"}
            ) from error
        except CampaignApprovalLapse as error:
            # NEW-229: denied like any refusal, but named (its remedy is a re-issue).
            raise HTTPException(status_code=409, detail={"code": error.blocker}) from error
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(
                status_code=409, detail="publication authority denied or unresolved"
            ) from error
        except SQLAlchemyError as error:
            raise HTTPException(
                status_code=503, detail="publication authority unavailable"
            ) from error

    @app.post("/v1/publications/receipt")
    async def receipt_publication(
        request: Request, subject: str = Depends(authenticated)
    ) -> dict[str, str]:
        body = await publication_body(request, subject)
        try:
            values = PublicationEffectReceipt.model_validate_json(body)
            authority = cast(
                PublicationAuthorityRepository, request.app.state.publication_authority
            )
            return await asyncio.to_thread(authority.receipt, subject, values)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail="invalid publication receipt") from error
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(status_code=409, detail="publication receipt denied") from error
        except SQLAlchemyError as error:
            raise HTTPException(
                status_code=503, detail="publication authority unavailable"
            ) from error

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/evidence", status_code=201)
    async def ingest(
        request: Request,
        subject: str = Depends(authenticated),
    ) -> dict[str, object]:
        try:
            envelope = EvidenceEnvelope.from_headers(request.headers)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail="invalid evidence envelope") from error
        if envelope.producer != subject:
            raise HTTPException(status_code=403, detail="producer identity does not match token")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > resolved.evidence_max_bytes:
                raise HTTPException(status_code=413, detail="evidence body exceeds size bound")
        repository_value = cast(MetadataRepository, request.app.state.metadata_repository)
        service = EvidenceIngestService(
            repository_value,
            LocalEvidenceStore(resolved.evidence_root),
            max_bytes=resolved.evidence_max_bytes,
        )
        try:
            result = await service.ingest(envelope, bytes(body))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return result

    @app.post("/v1/workers/heartbeat", status_code=202)
    async def heartbeat(
        request: Request,
        subject: str = Depends(authenticated),
    ) -> dict[str, object]:
        hostname = str(request.headers.get("x-worker-hostname", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", hostname):
            raise HTTPException(status_code=422, detail="invalid worker hostname")
        repository_value = cast(MetadataRepository, request.app.state.metadata_repository)
        try:
            stored = await repository_value.persist_worker_heartbeat(subject, hostname)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "status": "ACCEPTED",
            "identity": stored["identity"],
            "observed_at": stored["observed_at"],
        }

    def execution_repository(request: Request) -> AgentObservabilityRepository:
        return cast(AgentObservabilityRepository, request.app.state.observability_repository)

    async def observability_body(
        request: Request, model: type[ObservabilityValue]
    ) -> ObservabilityValue:
        body = await request.body()
        if not body or len(body) > 512 * 1024:
            raise HTTPException(status_code=413, detail="observability body exceeds size bound")
        try:
            return model.model_validate_json(body)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail="invalid observability payload") from error

    async def execute_observability(
        operation: Any,
    ) -> dict[str, object]:
        try:
            result = await operation
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return cast(dict[str, object], result)

    @app.post("/v1/observability/executions/{execution_id}/start", status_code=202)
    async def execution_start(
        execution_id: UUID,
        request: Request,
        subject: str = Depends(authenticated),
    ) -> dict[str, object]:
        values = await observability_body(request, ExecutionStartInput)
        return await execute_observability(
            execution_repository(request).start(subject, execution_id, values)
        )

    @app.post("/v1/observability/executions/{execution_id}/heartbeat", status_code=202)
    async def execution_heartbeat(
        execution_id: UUID,
        request: Request,
        subject: str = Depends(authenticated),
    ) -> dict[str, object]:
        values = await observability_body(request, ExecutionHeartbeatInput)
        return await execute_observability(
            execution_repository(request).heartbeat(subject, execution_id, values)
        )

    @app.post("/v1/observability/executions/{execution_id}/logs", status_code=202)
    async def execution_logs(
        execution_id: UUID,
        request: Request,
        subject: str = Depends(authenticated),
    ) -> dict[str, object]:
        values = await observability_body(request, LogBatchInput)
        return await execute_observability(
            execution_repository(request).append_logs(subject, execution_id, values)
        )

    @app.post("/v1/observability/executions/{execution_id}/finish", status_code=202)
    async def execution_finish(
        execution_id: UUID,
        request: Request,
        subject: str = Depends(authenticated),
    ) -> dict[str, object]:
        values = await observability_body(request, ExecutionFinishInput)
        return await execute_observability(
            execution_repository(request).finish(subject, execution_id, values)
        )

    @app.post("/v1/observability/gateway-events", status_code=202)
    async def gateway_event(
        request: Request,
        subject: str = Depends(authenticated),
    ) -> dict[str, object]:
        values = await observability_body(request, GatewayEventInput)
        return await execute_observability(
            execution_repository(request).gateway_event(subject, values)
        )

    return app


def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_evidence_ingest_app(settings=settings),
        host=settings.evidence_ingest_host,
        port=settings.evidence_ingest_port,
        access_log=False,
        server_header=False,
    )
