from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.agents.instructions import AgentInstructionPack, InstructionBundle
from engineering_control.config import Settings
from engineering_control.handoff.store import HandoffStore
from engineering_control.models.core import AgentRequest, AgentResult
from engineering_control.models.enums import Provider, RunStatus
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

    _heartbeat_interval_seconds = 30.0

    _request_keys = {
        "audit.agent_cell": "agent_request",
        "finding.reproduce": "verifier_request",
        "delivery.invoke_spec": "spec_request",
        "delivery.invoke_maker": "maker_request",
        "delivery.invoke_deployment_maker": "deployment_maker_request",
        "delivery.invoke_checker": "checker_request",
        "reconcile.invoke_release_reviewer": "release_review_request",
        "runtime.independent_review": "runtime_review_request",
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
            )
        return ClaudeCLIAdapter(executable=str(self.settings.claude_executable))

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step == "quota.refresh":
            return await self._quota_snapshot(payload)
        request = self._request(step, payload)
        self._validate_boundary(step, request)
        try:
            request, bundle = self.instruction_pack.enrich(request, step)
        except (OSError, ValueError) as error:
            return self._blocked(
                payload,
                "AGENT_PACK_INVALID",
                detail=type(error).__name__,
            )
        runtime_payload = {
            **payload,
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
            return self._blocked(
                runtime_payload, f"MANUAL_AUTH_REQUIRED_{self.provider.value.upper()}"
            )

        sequence = 0

        async def heartbeat_event(event: dict[str, Any]) -> None:
            nonlocal sequence
            sequence += 1
            safe = self.redactor.redact(event).value
            activity.heartbeat(
                {
                    "provider": self.provider.value,
                    "run_id": str(request.run_id),
                    "sequence": sequence,
                    "event_type": safe.get("type", "provider.event"),
                }
            )

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

        keepalive = asyncio.create_task(heartbeat_keepalive())
        try:
            result = await self.adapter.execute(request, heartbeat_event)
        finally:
            keepalive.cancel()
            with suppress(asyncio.CancelledError):
                await keepalive
        if result.status is RunStatus.SUCCESS:
            self._validate_result_schema(request, result)

        response = {
            **runtime_payload,
            "provider": self.provider.value,
            "status": result.status.value,
            "agent_result": result.model_dump(mode="json"),
        }
        if result.status is RunStatus.RATE_LIMITED:
            response["handoff_hashes"] = self._persist_handoff(request, result, runtime_payload)
        safe = self.redactor.redact(response).value
        if not isinstance(safe, dict):
            raise TypeError("redacted provider response must remain an object")
        return safe

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
