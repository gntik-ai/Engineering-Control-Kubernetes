from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.adapters.process import subscription_only_environment
from engineering_control.agents.instructions import AgentInstructionPack, InstructionBundle
from engineering_control.config import Settings
from engineering_control.handoff.store import HandoffStore
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import Provider, RunStatus
from engineering_control.observability.agent import (
    AgentTranscriptRecorder,
    ExecutionFinishInput,
    ExecutionStartInput,
    local_worker_host,
    normalize_agent_role,
)
from engineering_control.security.redaction import SecretRedactor


class ProviderAdapter(Protocol):
    provider: Provider

    async def authenticated(self) -> bool: ...

    async def execute(
        self,
        request: AgentRequest,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentResult: ...


class InstructionPack(Protocol):
    def enrich(
        self, request: AgentRequest, step: str
    ) -> tuple[AgentRequest, InstructionBundle]: ...


class ProviderActivityHandler:
    """Execute a normalized native-CLI request within one assigned worktree."""

    _heartbeat_interval_seconds = 10.0

    _request_keys = {
        "audit.agent_cell": "agent_request",
        "finding.reproduce": "verifier_request",
        "delivery.invoke_spec": "spec_request",
        "delivery.invoke_maker": "maker_request",
        "delivery.invoke_deployment_maker": "deployment_maker_request",
        "delivery.invoke_checker": "checker_request",
        "reconcile.invoke_release_reviewer": "release_review_request",
        "runtime.independent_review": "runtime_review_request",
        "enhancement.discover": "discovery_request",
        "enhancement.product_review": "product_review_request",
        "observability.invoke_codex": "maker_request",
        "observability.invoke_claude": "checker_request",
        "observability.invoke_claude_maker": "maker_request",
        "observability.invoke_codex_checker": "checker_request",
    }

    def __init__(
        self,
        settings: Settings,
        worker_kind: str,
        adapter: ProviderAdapter | None = None,
        instruction_pack: InstructionPack | None = None,
    ) -> None:
        if worker_kind not in {Provider.CODEX.value, Provider.CLAUDE.value}:
            raise ValueError("provider handler requires a codex or claude worker")
        self.settings = settings
        self.provider = Provider(worker_kind)
        self.provider_run_root = settings.run_root / self.provider.value
        self.adapter = adapter or self._default_adapter()
        if self.adapter.provider is not self.provider:
            raise ValueError("provider adapter does not match the worker identity")
        self.instruction_pack = instruction_pack or AgentInstructionPack(settings.agent_pack_root)
        self.redactor = SecretRedactor()

    def _default_adapter(self) -> ProviderAdapter:
        if self.provider is Provider.CODEX:
            return CodexCLIAdapter(
                executable=str(self.settings.codex_executable),
                temp_root=str(self.settings.temporary_root / self.provider.value),
                default_model=self.settings.codex_model,
            )
        return ClaudeCLIAdapter(executable=str(self.settings.claude_executable))

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step == "quota.refresh":
            return await self._quota_snapshot(payload)
        if step == "delivery.invoke_spec" and payload.get("status") == "SPEC_REUSED":
            # NEW-53: delivery.prepare_spec found this ChangeSet's OpenSpec from an
            # earlier attempt for the same issue content; nothing to invoke.
            return dict(payload)
        request = self._request(step, payload)
        self._validate_boundary(step, request)
        # F-14: request.prompt (up to 200_000 chars) is already fully captured on `request`
        # above -- nothing reads it back out of this activity's own echoed result (verified:
        # instructions.py/the CLI adapters consume it from `request` during this same call;
        # _persist_handoff receives `request` directly for the real handoff record, and only
        # reads decisions/source_refs/diff/test_results/open_questions from the echoed
        # payload). Dropping it from every echo below is the dominant fix for this step's
        # contribution to Temporal history accretion.
        echo_payload = self._without_prompt(payload, self._request_keys[step])
        try:
            request, bundle = self.instruction_pack.enrich(request, step)
        except (OSError, ValueError) as error:
            return self._blocked(
                echo_payload,
                "AGENT_PACK_INVALID",
                detail=type(error).__name__,
            )
        runtime_payload = {
            **echo_payload,
            "instruction_bundle": {
                "sha256": bundle.sha256,
                "revision": bundle.revision,
                "sources": list(bundle.sources),
                "allowed_tools": list(bundle.allowed_tools),
            },
        }
        try:
            authenticated = await self.adapter.authenticated()
        except FileNotFoundError:
            return self._blocked(
                runtime_payload, f"PROVIDER_CLI_MISSING_{self.provider.value.upper()}"
            )
        if not authenticated:
            blocked = self._blocked(
                runtime_payload, f"MANUAL_AUTH_REQUIRED_{self.provider.value.upper()}"
            )
            return blocked

        orchestration = payload.get("orchestration")
        context = orchestration if isinstance(orchestration, dict) else {}
        workflow_id = str(
            context.get("workflow_id") or payload.get("workflow_id") or f"provider-{request.run_id}"
        )[:240]
        phase = step[:120]
        attempt = self._positive_int(context.get("attempt"), default=1)
        max_attempts = max(
            attempt, self._positive_int(payload.get("max_attempts"), default=attempt)
        )
        recorder = AgentTranscriptRecorder(
            self.settings,
            request.run_id,
            self.provider.value,
        )
        try:
            await recorder.start(
                ExecutionStartInput(
                    workflow_id=workflow_id,
                    temporal_run_id=self._optional_text(context.get("workflow_run_id"), 240),
                    activity_id=self._optional_text(context.get("activity_id"), 240),
                    provider=self.provider.value,
                    role=normalize_agent_role(request.role),
                    host=local_worker_host(),
                    worker_kind=self.provider.value,
                    task_queue=self._optional_text(context.get("task_queue"), 120)
                    or (
                        self.settings.codex_task_queue
                        if self.provider is Provider.CODEX
                        else self.settings.claude_task_queue
                    ),
                    phase=phase,
                    selection_reason=self._selection_reason(payload, request.role),
                    cli_path_identifier=self.provider.value,
                    cli_version=await self._cli_version(),
                    attempt=attempt,
                    max_attempts=max_attempts,
                    started_at=datetime.now(UTC),
                )
            )
        except Exception:  # noqa: BLE001 - observability cannot change provider work
            recorder.transcript_incomplete = True

        sequence = 0

        async def heartbeat_event(event: dict[str, Any]) -> None:
            nonlocal sequence
            sequence += 1
            safe = self.redactor.redact(event).value
            if not isinstance(safe, dict):
                safe = {"type": "provider.event"}
            activity.heartbeat(
                {
                    "provider": self.provider.value,
                    "run_id": str(request.run_id),
                    "sequence": sequence,
                    "event_type": safe.get("type", "provider.event"),
                }
            )
            try:
                await recorder.event(safe)
            except Exception:  # noqa: BLE001 - observability cannot change provider work
                recorder.transcript_incomplete = True

        async def heartbeat_keepalive() -> None:
            while True:
                await asyncio.sleep(self._heartbeat_interval_seconds)
                activity.heartbeat(
                    {
                        "provider": self.provider.value,
                        "run_id": str(request.run_id),
                        "sequence": sequence,
                        "event_type": "provider.keepalive",
                    }
                )
                try:
                    await recorder.heartbeat(phase)
                except Exception:  # noqa: BLE001 - observability cannot change provider work
                    recorder.transcript_incomplete = True

        keepalive = asyncio.create_task(heartbeat_keepalive())
        try:
            result = await self.adapter.execute(request, heartbeat_event)
        except BaseException as error:
            try:
                await recorder.finish(
                    ExecutionFinishInput(
                        status="FAILED",
                        phase=phase,
                        ended_at=datetime.now(UTC),
                        retryable=True,
                        blocker_code="PROVIDER_ACTIVITY_FAILED",
                        error_summary=type(error).__name__,
                        transcript_incomplete=True,
                    )
                )
            except Exception:  # noqa: BLE001 - original provider failure remains authoritative
                recorder.transcript_incomplete = True
            raise
        finally:
            keepalive.cancel()
            with suppress(asyncio.CancelledError):
                await keepalive
        try:
            await recorder.finish(
                ExecutionFinishInput(
                    status=result.status.value,
                    phase=phase,
                    ended_at=result.finished_at,
                    exit_code=result.exit_code,
                    retryable=result.status is RunStatus.RATE_LIMITED,
                    blocker_code=result.error_code,
                    error_summary=(
                        result.summary if result.status is not RunStatus.SUCCESS else None
                    ),
                )
            )
        except Exception:  # noqa: BLE001 - provider result remains authoritative
            recorder.transcript_incomplete = True
        if result.status is RunStatus.SUCCESS:
            self._validate_result_schema(request, result)

        response = {
            **runtime_payload,
            "provider": self.provider.value,
            "status": result.status.value,
            "agent_result": result.model_dump(mode="json"),
            "observability": {
                "execution_id": str(request.run_id),
                "transcript_incomplete": recorder.transcript_incomplete,
                "output_truncated": recorder.output_truncated,
            },
        }
        if result.status is RunStatus.RATE_LIMITED:
            response["handoff_hashes"] = self._persist_handoff(request, result, runtime_payload)
        safe = self.redactor.redact(response).value
        if not isinstance(safe, dict):
            raise TypeError("redacted provider response must remain an object")
        return safe

    async def _cli_version(self) -> str | None:
        executable = getattr(self.adapter, "executable", None)
        if not isinstance(executable, str) or not executable:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=subscription_only_environment(self.provider.value),
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except (OSError, TimeoutError):
            return None
        if process.returncode != 0:
            return None
        safe = self.redactor.redact_text(stdout.decode("utf-8", errors="replace").strip())
        return str(safe.value)[:120] or None

    def _selection_reason(self, payload: dict[str, Any], role: str) -> str:
        for key in ("provider_selection_reason", "selection_reason", "routing_reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return str(self.redactor.redact_text(value).value)[:500]
        return f"workflow routing selected {self.provider.value} for {role}"[:500]

    @staticmethod
    def _optional_text(value: Any, limit: int) -> str | None:
        return str(value)[:limit] if value not in (None, "") else None

    @staticmethod
    def _positive_int(value: Any, *, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def _blocked(
        self, payload: dict[str, Any], blocker: str, *, detail: str | None = None
    ) -> dict[str, Any]:
        safe = self.redactor.redact(
            {
                **payload,
                "provider": self.provider.value,
                "status": "BLOCKED",
                "blocker": blocker,
                **({"blocker_detail": detail} if detail else {}),
            }
        ).value
        if not isinstance(safe, dict):
            raise TypeError("redacted provider blocker must remain an object")
        return safe

    async def _quota_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload.get("provider")
        if requested != self.provider.value:
            raise ApplicationError(
                "quota refresh provider does not match worker identity",
                type="PolicyViolation",
                non_retryable=True,
            )
        try:
            authenticated = await self.adapter.authenticated()
            cli_present = True
        except FileNotFoundError:
            authenticated = False
            cli_present = False
        return {
            "provider": self.provider.value,
            "captured_at": datetime.now(UTC).isoformat(),
            "remaining_capacity": None,
            "total_capacity": None,
            "reset_at": None,
            "running_jobs": 0,
            "rate_limited": False,
            "authenticated": authenticated,
            "cli_present": cli_present,
            "status": (
                "AVAILABLE"
                if authenticated
                else f"MANUAL_AUTH_REQUIRED_{self.provider.value.upper()}"
                if cli_present
                else f"PROVIDER_CLI_MISSING_{self.provider.value.upper()}"
            ),
            "source": "native_cli_auth_status;capacity_not_exposed",
        }

    def _request(self, step: str, payload: dict[str, Any]) -> AgentRequest:
        key = self._request_keys.get(step)
        if key is None:
            raise ApplicationError("unsupported provider activity", non_retryable=True)
        raw = self._find_mapping(payload, key)
        if raw is None:
            raise ApplicationError(
                f"{step} requires a normalized {key}",
                type="NotConfigured",
                non_retryable=True,
            )
        try:
            return AgentRequest.model_validate(raw)
        except ValueError as error:
            raise ApplicationError(
                f"invalid normalized provider request for {step}",
                type="PolicyViolation",
                non_retryable=True,
            ) from error

    def _validate_boundary(self, step: str, request: AgentRequest) -> None:
        if request.provider is not self.provider:
            raise ApplicationError(
                "provider request does not match worker identity",
                type="PolicyViolation",
                non_retryable=True,
            )
        root = self.settings.worktree_root.resolve()
        worktree = Path(request.working_directory).resolve()
        if not worktree.is_dir() or worktree == root or not worktree.is_relative_to(root):
            raise ApplicationError(
                "provider working directory is not an assigned bounded worktree",
                type="PolicyViolation",
                non_retryable=True,
            )
        if request.skip_git_repository_check:
            try:
                UUID(worktree.name)
            except ValueError as error:
                raise ApplicationError(
                    "non-Git execution requires the fixed observability canary boundary",
                    type="PolicyViolation",
                    non_retryable=True,
                ) from error
            if (
                step not in {"observability.invoke_codex", "observability.invoke_codex_checker"}
                or request.provider is not Provider.CODEX
                or request.writable
                or worktree.parent != root / "observability-canary"
            ):
                raise ApplicationError(
                    "non-Git execution requires the fixed observability canary boundary",
                    type="PolicyViolation",
                    non_retryable=True,
                )
        if (
            step in {"delivery.invoke_maker", "delivery.invoke_deployment_maker"}
            and not request.writable
        ):
            raise ApplicationError(
                "implementation maker must receive its explicitly writable worktree",
                type="PolicyViolation",
                non_retryable=True,
            )
        if (
            step not in {"delivery.invoke_maker", "delivery.invoke_deployment_maker"}
            and request.writable
        ):
            raise ApplicationError(
                "audit and checker invocations must be read-only",
                type="PolicyViolation",
                non_retryable=True,
            )

    @staticmethod
    def _validate_result_schema(request: AgentRequest, result: AgentResult) -> None:
        try:
            Draft202012Validator.check_schema(request.output_schema)
            Draft202012Validator(request.output_schema).validate(result.structured_result)
        except (SchemaError, ValidationError) as error:
            raise ApplicationError(
                "provider result failed its immutable output schema",
                type="SchemaViolation",
                non_retryable=True,
            ) from error

    def _persist_handoff(
        self, request: AgentRequest, result: AgentResult, payload: dict[str, Any]
    ) -> dict[str, str]:
        return HandoffStore(self.provider_run_root).persist(
            request,
            result,
            context=payload,
            decisions=self._string_list(self._find_value(payload, "decisions", [])),
            source_refs=self._object_list(self._find_value(payload, "source_refs", [])),
            diff=str(self._find_value(payload, "diff", "")),
            test_results=self._object(self._find_value(payload, "test_results", {})),
            open_questions=self._string_list(self._find_value(payload, "open_questions", [])),
        )

    _PROMPT_REDACTED = "[redacted by F-14 -- see docs/GO-STATUS.md for the history-bloat fix]"

    @classmethod
    def _without_prompt(cls, value: Any, key: str) -> Any:
        """Return a copy of value with key's nested "prompt" field redacted to a
        short placeholder, if present.

        Deliberately REPLACES rather than removes the key (found live in
        production, 2026-09-22): metadata.py's _agent_request() re-validates
        this exact payload shape via AgentRequest.model_validate() when
        persisting the agent result (agent.persist_result), and prompt is a
        required, non-empty field on that model (min_length=1) -- fully
        dropping the key broke that reconstruction with a pydantic
        ValidationError on the very next real delivery attempt. The
        placeholder satisfies validation without storing the up-to-200K-char
        prompt content in Temporal history; nothing downstream reads
        request.prompt's actual value (only presence/type is required).
        """
        if not isinstance(value, dict):
            return value
        if key in value and isinstance(value[key], dict):
            trimmed = dict(value[key])
            if "prompt" in trimmed:
                trimmed["prompt"] = cls._PROMPT_REDACTED
            return {**value, key: trimmed}
        return {inner_key: cls._without_prompt(inner, key) for inner_key, inner in value.items()}

    @classmethod
    def _find_mapping(cls, value: Any, key: str) -> dict[str, Any] | None:
        found = cls._find_value(value, key, None)
        return found if isinstance(found, dict) else None

    @classmethod
    def _find_value(cls, value: Any, key: str, default: Any) -> Any:
        if isinstance(value, dict):
            if key in value:
                return value[key]
            for nested in value.values():
                found = cls._find_value(nested, key, None)
                if found is not None:
                    return found
        return default

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    @staticmethod
    def _object_list(value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @staticmethod
    def _object(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}
