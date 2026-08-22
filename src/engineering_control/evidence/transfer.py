from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from engineering_control.config import Settings
from engineering_control.evidence.store import LocalEvidenceStore
from engineering_control.models.core import ArtifactMetadata
from engineering_control.models.enums import Project, RedactionStatus
from engineering_control.security.redaction import SecretRedactor


class EvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID
    project: Project
    workflow_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")
    campaign_id: UUID | None = None
    change_set_id: UUID | None = None
    category: str = Field(pattern=r"^(snapshot|findings|changes)$")
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    content_type: str = Field(pattern=r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
    producer: str = Field(pattern=r"^[a-z][a-z0-9-]{0,119}$")
    redaction_status: RedactionStatus
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("redaction_status")
    @classmethod
    def safe_redaction_status(cls, value: RedactionStatus) -> RedactionStatus:
        if value is RedactionStatus.REJECTED:
            raise ValueError("rejected evidence cannot be uploaded")
        return value

    def headers(self) -> dict[str, str]:
        values = {
            "X-Evidence-Artifact-Id": str(self.artifact_id),
            "X-Evidence-Project": self.project.value,
            "X-Evidence-Workflow-Id": self.workflow_id,
            "X-Evidence-Category": self.category,
            "X-Evidence-Filename": self.filename,
            "X-Evidence-Producer": self.producer,
            "X-Evidence-Redaction-Status": self.redaction_status.value,
            "X-Evidence-Source-Sha256": self.source_sha256,
            "Content-Type": self.content_type,
        }
        if self.campaign_id is not None:
            values["X-Evidence-Campaign-Id"] = str(self.campaign_id)
        if self.change_set_id is not None:
            values["X-Evidence-Change-Set-Id"] = str(self.change_set_id)
        return values

    @classmethod
    def from_headers(cls, headers: Any) -> EvidenceEnvelope:
        return cls.model_validate(
            {
                "artifact_id": headers.get("x-evidence-artifact-id"),
                "project": headers.get("x-evidence-project"),
                "workflow_id": headers.get("x-evidence-workflow-id"),
                "campaign_id": headers.get("x-evidence-campaign-id"),
                "change_set_id": headers.get("x-evidence-change-set-id"),
                "category": headers.get("x-evidence-category"),
                "filename": headers.get("x-evidence-filename"),
                "content_type": headers.get("content-type"),
                "producer": headers.get("x-evidence-producer"),
                "redaction_status": headers.get("x-evidence-redaction-status"),
                "source_sha256": headers.get("x-evidence-source-sha256"),
            }
        )


class EvidenceBindingRepository(Protocol):
    async def resolve_evidence_binding(
        self,
        workflow_id: str,
        project: Project,
        campaign_id: UUID | None,
        change_set_id: UUID | None,
    ) -> dict[str, UUID | None]: ...

    async def persist_artifact(self, metadata: ArtifactMetadata) -> dict[str, Any]: ...


class EvidenceIngestService:
    """Authorize, verify, redact-check, atomically store, and index worker evidence."""

    _text_content = re.compile(r"^(?:text/|application/(?:json|[^/]+\+json)$)")

    def __init__(
        self,
        repository: EvidenceBindingRepository,
        store: LocalEvidenceStore,
        *,
        max_bytes: int,
    ) -> None:
        self.repository = repository
        self.store = store
        self.max_bytes = max_bytes
        self.redactor = SecretRedactor()

    async def ingest(self, envelope: EvidenceEnvelope, payload: bytes) -> dict[str, Any]:
        if not payload or len(payload) > self.max_bytes:
            raise ValueError("evidence body is empty or exceeds the configured size bound")
        source_sha = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(source_sha, envelope.source_sha256):
            raise ValueError("evidence content hash does not match its immutable envelope")
        self._require_pre_redacted(envelope, payload)
        binding = await self.repository.resolve_evidence_binding(
            envelope.workflow_id,
            envelope.project,
            envelope.campaign_id,
            envelope.change_set_id,
        )
        metadata = await asyncio.to_thread(
            self.store.put_bytes,
            artifact_id=envelope.artifact_id,
            project=envelope.project,
            campaign_id=binding["campaign_id"],
            change_set_id=binding["change_set_id"],
            category=envelope.category,
            filename=envelope.filename,
            payload=payload,
            run_id=self._uuid(binding["run_id"], "run_id"),
            producer=envelope.producer,
            content_type=envelope.content_type,
            redaction_status=envelope.redaction_status,
        )
        return await self.repository.persist_artifact(metadata)

    def _require_pre_redacted(self, envelope: EvidenceEnvelope, payload: bytes) -> None:
        if self._text_content.match(envelope.content_type):
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("text evidence must be valid UTF-8") from error
            if envelope.content_type == "application/json" or envelope.content_type.endswith(
                "+json"
            ):
                try:
                    value = json.loads(text)
                except json.JSONDecodeError as error:
                    raise ValueError("JSON evidence is invalid") from error
                report = self.redactor.redact(value)
            else:
                report = self.redactor.redact_text(text)
            if report.replacements:
                raise ValueError(
                    "central redaction rejected unredacted text evidence before persistence"
                )
            return
        if envelope.redaction_status is not RedactionStatus.REDACTED:
            raise ValueError("binary evidence requires an explicit REDACTED producer assertion")

    @staticmethod
    def _uuid(value: UUID | None, label: str) -> UUID:
        if value is None:
            raise ValueError(f"evidence binding is missing {label}")
        return value


class EvidencePublisher:
    """Upload one bounded, pre-redacted worker artifact to the narrow private ingest service."""

    def __init__(
        self,
        settings: Settings,
        bounded_root: Path,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.bounded_root = bounded_root.resolve()
        self.transport = transport
        self.url = settings.evidence_ingest_url
        if self.url is not None:
            self._validate_private_url(self.url)

    @property
    def configured(self) -> bool:
        return self.url is not None

    def publish(
        self,
        path: Path,
        *,
        project: Project,
        workflow_id: str,
        category: str,
        filename: str,
        producer: str,
        content_type: str,
        redaction_status: RedactionStatus,
        campaign_id: UUID | None = None,
        change_set_id: UUID | None = None,
    ) -> dict[str, Any]:
        if self.url is None:
            if self.settings.environment == "production":
                raise ValueError("production evidence ingest is not configured")
            return {"status": "LOCAL_DEVELOPMENT_ONLY", "path": str(path)}
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(self.bounded_root):
            raise ValueError("evidence upload escaped its bounded producer root")
        size_bytes = resolved.stat().st_size
        if size_bytes < 1 or size_bytes > self.settings.evidence_max_bytes:
            raise ValueError("evidence upload is empty or exceeds the configured size bound")
        payload = resolved.read_bytes()
        if len(payload) != size_bytes:
            raise ValueError("evidence upload changed while it was being read")
        source_sha = hashlib.sha256(payload).hexdigest()
        artifact_id = uuid5(
            NAMESPACE_URL,
            f"evidence:{workflow_id}:{producer}:{category}:{filename}:{source_sha}",
        )
        envelope = EvidenceEnvelope(
            artifact_id=artifact_id,
            project=project,
            workflow_id=workflow_id,
            campaign_id=campaign_id,
            change_set_id=change_set_id,
            category=category,
            filename=filename,
            content_type=content_type,
            producer=producer,
            redaction_status=redaction_status,
            source_sha256=source_sha,
        )
        token = self.settings.read_evidence_ingest_token()
        headers = {
            **envelope.headers(),
            "Authorization": f"Bearer {token}",
            "X-Engineering-Actor": producer,
        }
        with httpx.Client(
            timeout=60,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        ) as client:
            try:
                response = client.post(self.url, headers=headers, content=payload)
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise ValueError("evidence ingest request failed") from error
        if not isinstance(result, dict) or result.get("id") != str(artifact_id):
            raise ValueError("evidence ingest returned an invalid receipt")
        return result

    @staticmethod
    def _validate_private_url(value: str) -> None:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("evidence ingest URL must be an authenticated HTTP(S) origin")
        if parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/v1/evidence":
            raise ValueError("evidence ingest URL must target exactly /v1/evidence")
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as error:
            raise ValueError(
                "evidence ingest URL must use an explicit private IP address"
            ) from error
        if not (address.is_private or address.is_loopback):
            raise ValueError("evidence ingest URL cannot target a public address")
