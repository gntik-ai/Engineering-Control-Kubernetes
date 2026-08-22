from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import ValidationError

from engineering_control.api.auth import identity_bearer_auth
from engineering_control.config import Settings, get_settings
from engineering_control.db.repository import MetadataRepository, SQLMetadataRepository
from engineering_control.evidence.store import LocalEvidenceStore
from engineering_control.evidence.transfer import EvidenceEnvelope, EvidenceIngestService
from engineering_control.logging import configure_logging


def create_evidence_ingest_app(
    *,
    settings: Settings | None = None,
    repository: MetadataRepository | None = None,
    tokens: dict[str, str] | None = None,
) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.metadata_repository = repository or SQLMetadataRepository(
            resolved.database_url.get_secret_value()
        )
        yield

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
