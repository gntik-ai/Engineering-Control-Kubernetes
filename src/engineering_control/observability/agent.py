from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.config import Settings
from engineering_control.db.models import AgentExecution, AgentLogChunk, DashboardEvent
from engineering_control.security.redaction import SecretRedactor

TranscriptStream = Literal["system", "progress", "assistant", "tool", "test", "stderr"]
AgentRole = Literal["maker", "checker", "auditor", "reviewer", "verifier"]


def normalize_agent_role(value: str) -> AgentRole:
    role = value.strip().lower().replace("_", "-")
    if role in {"maker", "implementer", "product-spec-architect"}:
        return "maker"
    if role in {"checker", "code-reviewer"}:
        return "checker"
    if role in {"auditor", "domain-auditor"}:
        return "auditor"
    if role in {"verifier", "finding-verifier", "runtime-verifier"}:
        return "verifier"
    if role in {"reviewer", "release-plan-reviewer"}:
        return "reviewer"
    return "reviewer"


class ObservabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionStartInput(ObservabilityModel):
    workflow_id: str = Field(min_length=1, max_length=240)
    temporal_run_id: str | None = Field(default=None, max_length=240)
    activity_id: str | None = Field(default=None, max_length=240)
    provider: Literal["codex", "claude"]
    role: AgentRole
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    worker_kind: Literal["codex", "claude"]
    task_queue: str = Field(min_length=1, max_length=120)
    phase: str = Field(min_length=1, max_length=120)
    selection_reason: str = Field(min_length=1, max_length=500)
    cli_path_identifier: Literal["codex", "claude"]
    cli_version: str | None = Field(default=None, max_length=120)
    attempt: int = Field(default=1, ge=1, le=100)
    max_attempts: int = Field(default=1, ge=1, le=100)
    started_at: datetime


class ExecutionHeartbeatInput(ObservabilityModel):
    observed_at: datetime
    phase: str | None = Field(default=None, max_length=120)


class LogChunkInput(ObservabilityModel):
    sequence: int = Field(ge=1)
    stream: TranscriptStream
    created_at: datetime
    redacted_text: str = Field(min_length=1, max_length=65_536)
    byte_count: int = Field(ge=1, le=65_536)
    redaction_count: int = Field(default=0, ge=0, le=100_000)
    is_truncated: bool = False


class LogBatchInput(ObservabilityModel):
    chunks: list[LogChunkInput] = Field(min_length=1, max_length=64)
    output_truncated: bool = False
    transcript_incomplete: bool = False


class ExecutionFinishInput(ObservabilityModel):
    status: str = Field(min_length=1, max_length=64)
    phase: str | None = Field(default=None, max_length=120)
    ended_at: datetime
    exit_code: int | None = Field(default=None, ge=-2_147_483_648, le=2_147_483_647)
    retryable: bool | None = None
    blocker_code: str | None = Field(default=None, max_length=120)
    error_summary: str | None = Field(default=None, max_length=4000)
    output_truncated: bool = False
    transcript_incomplete: bool = False


class GatewayEventInput(ObservabilityModel):
    event_type: Literal[
        "telegram.command_received",
        "telegram.command_recognized",
        "telegram.command_rejected",
        "engineering.request_accepted",
        "engineering.api_request_started",
        "engineering.api_response_completed",
        "engineering.workflow_returned",
        "engineering.request_failed",
        "telegram.response_prepared",
        "telegram.response_delivered",
        "telegram.response_delivery_failed",
    ]
    command: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9-]*$")
    outcome: str | None = Field(default=None, max_length=40, pattern=r"^[A-Z0-9_]+$")
    project: Literal["falcone", "musematic", "llmwiki"] | None = None
    issue_number: int | None = Field(default=None, ge=1)
    mode: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9-]+$")
    workflow_id: str | None = Field(default=None, max_length=240)
    platform_update_id: int | None = Field(default=None, strict=True, ge=0, le=2**53 - 1)
    delivery_correlation_id: UUID | None = None
    delivery_status: (
        Literal[
            "ACKNOWLEDGED", "UNACKNOWLEDGED", "UNKNOWN", "UNAVAILABLE", "EMPTY", "REJECTED_OVERSIZE"
        ]
        | None
    ) = None
    acknowledged_message_ids: list[Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]] = Field(
        default_factory=list, max_length=64
    )

    @model_validator(mode="after")
    def validate_delivery_receipt(self) -> Self:
        if self.event_type == "telegram.response_delivered":
            if (
                self.delivery_status != "ACKNOWLEDGED"
                or self.delivery_correlation_id is None
                or self.platform_update_id is None
                or not self.acknowledged_message_ids
                or len(set(self.acknowledged_message_ids)) != len(self.acknowledged_message_ids)
            ):
                raise ValueError(
                    "delivery success requires a correlated complete transport receipt"
                )
        elif self.delivery_status == "ACKNOWLEDGED" or self.acknowledged_message_ids:
            raise ValueError("only a delivery-success event may carry acknowledgement IDs")
        return self


@dataclass(frozen=True)
class NormalizedTranscriptLine:
    stream: TranscriptStream
    text: str
    redaction_count: int
    created_at: datetime


class TranscriptNormalizer:
    """Convert only visibly emitted provider events into bounded operational text."""

    def __init__(self, *, maximum_line_bytes: int = 16 * 1024) -> None:
        self.maximum_line_bytes = maximum_line_bytes
        self.redactor = SecretRedactor()

    def normalize(
        self, provider: Literal["codex", "claude"], event: dict[str, Any]
    ) -> list[NormalizedTranscriptLine]:
        now = datetime.now(UTC)
        event_type = str(event.get("type", "provider.event"))[:120]
        if event_type in {"user", "prompt", "input", "thinking", "reasoning"}:
            return []
        if event_type == "provider.stderr":
            return self._line("stderr", event.get("text"), now)
        if provider == "codex":
            return self._codex(event_type, event, now)
        return self._claude(event_type, event, now)

    def _codex(
        self, event_type: str, event: dict[str, Any], now: datetime
    ) -> list[NormalizedTranscriptLine]:
        if event_type in {"thread.started", "session.started"}:
            return self._line("system", "Codex CLI session started", now)
        if event_type in {"turn.started", "turn.completed"}:
            return self._line("progress", event_type.replace(".", " "), now)
        item = event.get("item")
        if isinstance(item, dict):
            item_type = str(item.get("type", "item"))
            if item_type in {"reasoning", "thinking"}:
                return []
            if item_type in {"agent_message", "message"}:
                return self._line("assistant", item.get("text") or item.get("content"), now)
            if item_type in {"command_execution", "tool_call", "mcp_tool_call"}:
                name = self._safe_name(item.get("name") or item.get("tool") or item_type)
                status = self._safe_name(item.get("status") or event_type)
                exit_code = item.get("exit_code")
                suffix = f" (exit {exit_code})" if isinstance(exit_code, int) else ""
                stream: TranscriptStream = "test" if "test" in name.lower() else "tool"
                return self._line(stream, f"{name}: {status}{suffix}", now)
            if item_type in {"file_change", "file_write"}:
                status = self._safe_name(item.get("status"))
                return self._line("tool", f"File change: {status}", now)
        if event_type.endswith("failed") or event_type in {"error", "turn.failed"}:
            return self._line("stderr", self._error_text(event, event_type), now)
        if event_type.startswith(("item.", "turn.")):
            return self._line("progress", event_type.replace(".", " "), now)
        return []

    def _claude(
        self, event_type: str, event: dict[str, Any], now: datetime
    ) -> list[NormalizedTranscriptLine]:
        if event_type == "system":
            return self._line("system", "Claude Code CLI session started", now)
        if event_type == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            values = content if isinstance(content, list) else []
            lines: list[NormalizedTranscriptLine] = []
            for block in values:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type", ""))
                if block_type == "text":
                    lines.extend(self._line("assistant", block.get("text"), now))
                elif block_type == "tool_use":
                    name = self._safe_name(block.get("name") or "tool")
                    stream: TranscriptStream = "test" if "test" in name.lower() else "tool"
                    lines.extend(self._line(stream, f"{name}: started", now))
            return lines
        if event_type in {"tool", "tool_result"}:
            status = "failed" if event.get("is_error") is True else "completed"
            return self._line("tool", f"Tool result: {status}", now)
        if event_type == "result":
            return self._line("assistant", event.get("result"), now)
        if event_type in {"error", "failed"} or event_type.endswith("failed"):
            return self._line("stderr", self._error_text(event, event_type), now)
        return []

    def _line(
        self, stream: TranscriptStream, value: Any, created_at: datetime
    ) -> list[NormalizedTranscriptLine]:
        if not isinstance(value, str) or not value.strip():
            return []
        report = self.redactor.redact_text(value.strip())
        text = self._truncate_utf8(str(report.value), self.maximum_line_bytes)
        if not text:
            return []
        return [NormalizedTranscriptLine(stream, text, report.replacements, created_at)]

    @staticmethod
    def _safe_name(value: Any) -> str:
        text = str(value or "operation")
        return "".join(
            character for character in text if character.isalnum() or character in " ._:-"
        )[:120]

    @staticmethod
    def _error_text(event: dict[str, Any], event_type: str) -> str:
        value = event.get("error") or event.get("message") or event.get("detail")
        if isinstance(value, dict):
            code = value.get("code") or value.get("type") or "provider_error"
            message = value.get("message")
            return f"{code}: {message}" if isinstance(message, str) else str(code)
        return f"{event_type}: {value}" if isinstance(value, str) else event_type

    @staticmethod
    def _truncate_utf8(value: str, maximum: int) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= maximum:
            return value
        return encoded[:maximum].decode("utf-8", errors="ignore")


class AgentObservabilityRepository:
    """Typed persistence used only by the private identity-bound ingest."""

    def __init__(self, database_url: str, *, maximum_transcript_bytes: int) -> None:
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.maximum_transcript_bytes = maximum_transcript_bytes
        self.redactor = SecretRedactor()

    async def start(
        self, subject: str, execution_id: UUID, values: ExecutionStartInput
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._start, subject, execution_id, values)

    def _start(
        self, subject: str, execution_id: UUID, values: ExecutionStartInput
    ) -> dict[str, Any]:
        self._authorize(subject, values.provider)
        with self.sessions.begin() as session:
            row = session.get(AgentExecution, execution_id)
            if row is None:
                raise ValueError("execution must be registered by the controller before spawn")
            if (row.provider, row.role, row.workflow_id) != (
                values.provider,
                values.role,
                values.workflow_id,
            ):
                raise ValueError("execution start conflicts with controller registration")
            row.temporal_run_id = values.temporal_run_id
            row.activity_id = values.activity_id
            row.host = values.host
            row.worker_kind = values.worker_kind
            row.task_queue = values.task_queue
            row.status = "RUNNING"
            row.phase = values.phase
            row.selection_reason = str(self.redactor.redact_text(values.selection_reason).value)
            row.cli_path_identifier = values.cli_path_identifier
            row.cli_version = (
                str(self.redactor.redact_text(values.cli_version).value)
                if values.cli_version
                else None
            )
            row.attempt = values.attempt
            row.max_attempts = values.max_attempts
            row.started_at = values.started_at
            row.last_heartbeat_at = values.started_at
            self._event(
                session,
                row,
                event_type="provider.execution_started",
                level="INFO",
                message=f"{values.provider} {values.role} execution started",
            )
            return {"status": "ACCEPTED", "execution_id": str(row.id)}

    async def heartbeat(
        self, subject: str, execution_id: UUID, values: ExecutionHeartbeatInput
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._heartbeat, subject, execution_id, values)

    def _heartbeat(
        self, subject: str, execution_id: UUID, values: ExecutionHeartbeatInput
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = self._execution(session, subject, execution_id)
            if row.ended_at is None:
                row.last_heartbeat_at = values.observed_at
                if values.phase:
                    row.phase = values.phase
            return {"status": "ACCEPTED", "execution_id": str(row.id)}

    async def append_logs(
        self, subject: str, execution_id: UUID, values: LogBatchInput
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._append_logs, subject, execution_id, values)

    def _append_logs(
        self, subject: str, execution_id: UUID, values: LogBatchInput
    ) -> dict[str, Any]:
        inserted = 0
        with self.sessions.begin() as session:
            row = self._execution(session, subject, execution_id)
            was_truncated = row.output_truncated
            for value in values.chunks:
                existing = session.scalar(
                    select(AgentLogChunk).where(
                        AgentLogChunk.execution_id == execution_id,
                        AgentLogChunk.sequence == value.sequence,
                    )
                )
                report = self.redactor.redact_text(value.redacted_text)
                text = str(report.value)
                byte_count = len(text.encode("utf-8"))
                if byte_count > 65_536:
                    raise ValueError("transcript chunk exceeds the ingest bound")
                if existing is not None:
                    if existing.redacted_text != text or existing.stream != value.stream:
                        raise ValueError("transcript sequence conflicts with stored content")
                    continue
                if row.total_log_bytes + byte_count > self.maximum_transcript_bytes:
                    row.output_truncated = True
                    continue
                session.add(
                    AgentLogChunk(
                        execution_id=execution_id,
                        sequence=value.sequence,
                        stream=value.stream,
                        created_at=value.created_at,
                        redacted_text=text,
                        byte_count=byte_count,
                        redaction_count=value.redaction_count + report.replacements,
                        is_truncated=value.is_truncated,
                    )
                )
                row.total_log_bytes += byte_count
                row.redaction_count += value.redaction_count + report.replacements
                inserted += 1
            row.output_truncated = row.output_truncated or values.output_truncated
            row.transcript_incomplete = row.transcript_incomplete or values.transcript_incomplete
            row.last_heartbeat_at = datetime.now(UTC)
            if row.output_truncated and not was_truncated:
                self._event(
                    session,
                    row,
                    event_type="provider.transcript_truncated",
                    level="WARNING",
                    message="Provider transcript reached its persistence limit",
                )
            return {
                "status": "ACCEPTED",
                "execution_id": str(row.id),
                "inserted": inserted,
                "output_truncated": row.output_truncated,
            }

    async def finish(
        self, subject: str, execution_id: UUID, values: ExecutionFinishInput
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._finish, subject, execution_id, values)

    def _finish(
        self, subject: str, execution_id: UUID, values: ExecutionFinishInput
    ) -> dict[str, Any]:
        with self.sessions.begin() as session:
            row = self._execution(session, subject, execution_id)
            row.status = values.status
            row.phase = values.phase or row.phase
            row.last_heartbeat_at = values.ended_at
            row.ended_at = values.ended_at
            row.exit_code = values.exit_code
            row.retryable = values.retryable
            row.blocker_code = values.blocker_code
            row.error_summary = (
                str(self.redactor.redact_text(values.error_summary).value)[:4000]
                if values.error_summary
                else None
            )
            row.output_truncated = row.output_truncated or values.output_truncated
            row.transcript_incomplete = row.transcript_incomplete or values.transcript_incomplete
            successful = values.status.upper() in {"SUCCESS", "COMPLETE", "COMPLETED"}
            self._event(
                session,
                row,
                event_type=(
                    "provider.execution_completed" if successful else "provider.execution_failed"
                ),
                level="INFO" if successful else "ERROR",
                message=(
                    f"{row.provider} {row.role} execution completed"
                    if successful
                    else f"{row.provider} {row.role} execution failed"
                ),
            )
            return {"status": "ACCEPTED", "execution_id": str(row.id)}

    async def cleanup(
        self, *, log_retention_days: int, execution_retention_days: int, limit: int = 10_000
    ) -> dict[str, int]:
        return await asyncio.to_thread(
            self._cleanup, log_retention_days, execution_retention_days, limit
        )

    async def gateway_event(self, subject: str, values: GatewayEventInput) -> dict[str, object]:
        return await asyncio.to_thread(self._gateway_event, subject, values)

    def _gateway_event(self, subject: str, values: GatewayEventInput) -> dict[str, object]:
        if subject != "hermes-gateway":
            raise ValueError("gateway event identity is not authorized")
        now = datetime.now(UTC)
        labels = {
            "telegram.command_received": "Telegram command received",
            "telegram.command_recognized": "Telegram command route recognized",
            "telegram.command_rejected": "Telegram command rejected",
            "engineering.request_accepted": "Engineering Control request accepted",
            "engineering.api_request_started": "Engineering Control API request started",
            "engineering.api_response_completed": "Engineering Control API response completed",
            "engineering.workflow_returned": "Engineering Control workflow identifier returned",
            "engineering.request_failed": "Engineering Control request failed",
            "telegram.response_prepared": "Telegram response prepared; delivery not acknowledged",
            "telegram.response_delivered": "Telegram response delivered",
            "telegram.response_delivery_failed": "Telegram response delivery failed",
        }
        with self.sessions.begin() as session:
            row = DashboardEvent(
                occurred_at=now,
                level=(
                    "WARNING"
                    if values.event_type
                    in {
                        "telegram.command_rejected",
                        "engineering.request_failed",
                        "telegram.response_delivery_failed",
                    }
                    else "INFO"
                ),
                event_type=values.event_type,
                source="hermes",
                project=values.project,
                workflow_id=values.workflow_id,
                issue_number=values.issue_number,
                entity_type="gateway_command",
                entity_id=values.command,
                current_phase="gateway",
                message=labels[values.event_type],
                metadata_json={
                    "command": values.command,
                    "mode": values.mode,
                    "outcome": values.outcome,
                    "gateway": "hermes-gateway",
                    **(
                        {"platform_update_id": values.platform_update_id}
                        if values.platform_update_id is not None
                        else {}
                    ),
                    **(
                        {"delivery_correlation_id": str(values.delivery_correlation_id)}
                        if values.delivery_correlation_id
                        else {}
                    ),
                    **(
                        {
                            "delivery_status": values.delivery_status,
                            "acknowledged_message_ids": values.acknowledged_message_ids,
                        }
                        if values.delivery_status is not None
                        else {}
                    ),
                },
            )
            session.add(row)
            session.flush()
            return {"status": "ACCEPTED", "event_id": row.id}

    def _cleanup(self, log_days: int, execution_days: int, limit: int) -> dict[str, int]:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            log_ids = session.scalars(
                select(AgentLogChunk.id)
                .where(AgentLogChunk.created_at < now - timedelta(days=log_days))
                .order_by(AgentLogChunk.id)
                .limit(limit)
            ).all()
            if log_ids:
                session.execute(delete(AgentLogChunk).where(AgentLogChunk.id.in_(log_ids)))
            execution_ids = session.scalars(
                select(AgentExecution.id)
                .where(
                    AgentExecution.ended_at < now - timedelta(days=execution_days),
                    ~select(AgentLogChunk.id)
                    .where(AgentLogChunk.execution_id == AgentExecution.id)
                    .exists(),
                )
                .order_by(AgentExecution.ended_at)
                .limit(limit)
            ).all()
            if execution_ids:
                session.execute(delete(AgentExecution).where(AgentExecution.id.in_(execution_ids)))
            return {"log_chunks": len(log_ids), "executions": len(execution_ids)}

    def _execution(self, session: Session, subject: str, execution_id: UUID) -> AgentExecution:
        row = session.get(AgentExecution, execution_id)
        if row is None:
            raise ValueError("execution does not exist")
        self._authorize(subject, row.provider)
        return row

    @staticmethod
    def _event(
        session: Session,
        row: AgentExecution,
        *,
        event_type: str,
        level: str,
        message: str,
    ) -> None:
        session.add(
            DashboardEvent(
                occurred_at=datetime.now(UTC),
                level=level,
                event_type=event_type,
                source="worker",
                host=row.host,
                worker_kind=row.worker_kind,
                project=row.project,
                workflow_id=row.workflow_id,
                temporal_run_id=row.temporal_run_id,
                issue_number=row.issue_number,
                campaign_id=str(row.campaign_id) if row.campaign_id else None,
                entity_type="agent_execution",
                entity_id=str(row.id),
                current_phase=row.phase,
                message=message[:500],
                metadata_json={
                    "provider": row.provider,
                    "role": row.role,
                    "status": row.status,
                    "attempt": row.attempt,
                    "task_queue": row.task_queue,
                },
            )
        )

    @staticmethod
    def _authorize(subject: str, provider: str) -> None:
        if subject != f"{provider}-runner" or provider not in {"codex", "claude"}:
            raise ValueError("worker identity does not match execution provider")


class AgentObservabilityClient:
    """Best-effort private worker client; no failure can change provider work."""

    def __init__(
        self,
        settings: Settings,
        *,
        identity: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.identity = identity
        self.transport = transport
        self.base_url = self._base_url(settings.evidence_ingest_url)

    @property
    def configured(self) -> bool:
        return self.base_url is not None

    async def start(self, execution_id: UUID, values: ExecutionStartInput) -> bool:
        return await self._post(execution_id, "start", values)

    async def heartbeat(self, execution_id: UUID, values: ExecutionHeartbeatInput) -> bool:
        return await self._post(execution_id, "heartbeat", values)

    async def logs(self, execution_id: UUID, values: LogBatchInput) -> bool:
        return await self._post(execution_id, "logs", values)

    async def finish(self, execution_id: UUID, values: ExecutionFinishInput) -> bool:
        return await self._post(execution_id, "finish", values)

    async def _post(self, execution_id: UUID, action: str, values: ObservabilityModel) -> bool:
        if self.base_url is None:
            return self.settings.environment != "production"
        try:
            token = self.settings.read_evidence_ingest_token()
            actor = (
                f"{values.provider}-runner"
                if isinstance(values, ExecutionStartInput)
                else self.identity
            )
            if actor not in {"codex-runner", "claude-runner"}:
                return False
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/v1/observability/executions/{execution_id}/{action}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Engineering-Actor": actor,
                    },
                    content=values.model_dump_json(),
                )
                return response.status_code == 202
        except (OSError, ValueError, httpx.HTTPError):
            return False

    @staticmethod
    def _base_url(value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as error:
            raise ValueError("observability ingest requires an explicit private IP") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not (address.is_private or address.is_loopback)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/v1/evidence"
        ):
            raise ValueError("observability ingest must share the bounded private evidence origin")
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


class AgentTranscriptRecorder:
    def __init__(
        self,
        settings: Settings,
        execution_id: UUID,
        provider: Literal["codex", "claude"],
        *,
        client: AgentObservabilityClient | None = None,
    ) -> None:
        self.settings = settings
        self.execution_id = execution_id
        self.provider = provider
        self.client = client or AgentObservabilityClient(settings, identity=f"{provider}-runner")
        self.normalizer = TranscriptNormalizer(
            maximum_line_bytes=settings.dashboard_transcript_line_max_bytes
        )
        self.buffer: list[LogChunkInput] = []
        self.buffer_bytes = 0
        self.total_bytes = 0
        self.sequence = 0
        self.output_truncated = False
        self.transcript_incomplete = False
        self.truncation_emitted = False
        self._event_lock = asyncio.Lock()

    async def start(self, values: ExecutionStartInput) -> None:
        if not await self.client.start(self.execution_id, values):
            self.transcript_incomplete = True

    async def event(self, event: dict[str, Any]) -> None:
        async with self._event_lock:
            for line in self.normalizer.normalize(self.provider, event):
                encoded = line.text.encode("utf-8")
                if self.total_bytes + len(encoded) > self.settings.dashboard_transcript_max_bytes:
                    self.output_truncated = True
                    if not self.truncation_emitted:
                        self.truncation_emitted = True
                        message = "Transcript persistence limit reached"
                        if (
                            self.total_bytes + len(message.encode("utf-8"))
                            <= self.settings.dashboard_transcript_max_bytes
                        ):
                            await self._append(
                                NormalizedTranscriptLine("system", message, 0, datetime.now(UTC)),
                                truncated=True,
                            )
                    continue
                await self._append(line)
            if (
                self.buffer_bytes >= self.settings.dashboard_transcript_chunk_bytes
                or len(self.buffer) >= 64
            ):
                await self.flush()

    async def _append(self, line: NormalizedTranscriptLine, *, truncated: bool = False) -> None:
        self.sequence += 1
        byte_count = len(line.text.encode("utf-8"))
        self.total_bytes += byte_count
        self.buffer_bytes += byte_count
        self.buffer.append(
            LogChunkInput(
                sequence=self.sequence,
                stream=line.stream,
                created_at=line.created_at,
                redacted_text=line.text,
                byte_count=byte_count,
                redaction_count=line.redaction_count,
                is_truncated=truncated,
            )
        )

    async def heartbeat(self, phase: str) -> None:
        await self.flush()
        accepted = await self.client.heartbeat(
            self.execution_id,
            ExecutionHeartbeatInput(observed_at=datetime.now(UTC), phase=phase[:120]),
        )
        if not accepted:
            self.transcript_incomplete = True

    async def flush(self) -> None:
        if not self.buffer:
            return
        pending = self.buffer
        self.buffer = []
        self.buffer_bytes = 0
        accepted = await self.client.logs(
            self.execution_id,
            LogBatchInput(
                chunks=pending,
                output_truncated=self.output_truncated,
                transcript_incomplete=self.transcript_incomplete,
            ),
        )
        if not accepted:
            self.transcript_incomplete = True

    async def finish(self, values: ExecutionFinishInput) -> None:
        await self.flush()
        final = values.model_copy(
            update={
                "output_truncated": values.output_truncated or self.output_truncated,
                "transcript_incomplete": values.transcript_incomplete or self.transcript_incomplete,
            }
        )
        if not await self.client.finish(self.execution_id, final):
            self.transcript_incomplete = True


def local_worker_host() -> str:
    return socket.gethostname()[:253]
