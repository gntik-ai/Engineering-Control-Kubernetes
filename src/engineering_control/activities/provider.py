from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import ValidationError as PydanticValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.auth_probe import AuthProbeError
from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.adapters.process import (
    PROVIDER_CAPACITY_ERROR,
    SandboxToolchain,
    subscription_only_environment,
)
from engineering_control.agents.instructions import AgentInstructionPack, InstructionBundle
from engineering_control.config import Settings
from engineering_control.engineering.toolchain import (
    ToolchainUnavailableError,
    payload_node_toolchain,
)
from engineering_control.engineering.ui_blueprint import agent_pack_parts_only
from engineering_control.engineering.ui_page_delivery import (
    UI_PAGE_CHECKER_ROLE,
    UI_PAGE_MAKER_ROLE,
    checker_result,
    maker_result,
)
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
from engineering_control.observability.cgroup_memory import (
    memory_pressure_event,
    memory_snapshot,
)
from engineering_control.security.redaction import (
    SecretRedactor,
    bounded_detail,
    redact_detail_text,
)

_PYDANTIC_MESSAGE_PREFIX = re.compile(r"^(?:Value error|Assertion failed), ")


def error_detail(error: BaseException, redactor: SecretRedactor | None = None) -> str:
    """NEW-146: "<Type>: <message>", redacted and bounded, for a step's blocker_detail.

    A pydantic ValidationError (a ValueError) is summarised by location and message only:
    its str() embeds the offending input values, which can be a whole prompt."""
    # NEW-146 review 1: every piece is redacted on its own, before any label is put in front
    # of it: the line-anchored credential rules (password=..., token: ...) match only text
    # that still starts its line.
    redactor = redactor or SecretRedactor()

    def safe(text: str) -> str:
        # NEW-146 review 6: inline credentials first, then the redactor's rules.
        return redact_detail_text(text, redactor)

    def safe_message(text: str) -> str:
        # NEW-146 review 2: pydantic prefixes a validator's own message ("Value error, ",
        # "Assertion failed, "), which would hide a leading credential from the
        # line-anchored rules; redact the validator's text alone, then restore the label.
        match = _PYDANTIC_MESSAGE_PREFIX.match(text)
        if match is None:
            return safe(text)
        return f"{match.group(0)}{safe(text[match.end() :])}"

    if isinstance(error, PydanticValidationError):
        problems = error.errors(include_url=False, include_context=False, include_input=False)
        shown = "; ".join(
            f"{safe('.'.join(str(part) for part in problem.get('loc', ()))) or '<root>'}: "
            f"{safe_message(str(problem.get('msg', problem.get('type', 'invalid'))))}"
            for problem in problems[:5]
        )
        message = f"{error.error_count()} validation error(s) for {safe(error.title)}: {shown}"
    else:
        message = safe(str(error)) or "(no message)"
    return bounded_detail(f"{type(error).__name__}: {message}", redactor)


class ProviderAdapter(Protocol):
    provider: Provider

    async def authenticated(self) -> bool: ...

    async def execute(
        self,
        request: AgentRequest,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        *,
        toolchain: SandboxToolchain | None = None,
    ) -> AgentResult: ...


class InstructionPack(Protocol):
    def enrich(
        self, request: AgentRequest, step: str
    ) -> tuple[AgentRequest, InstructionBundle]: ...


class ProviderActivityHandler:
    """Execute a normalized native-CLI request within one assigned worktree."""

    _heartbeat_interval_seconds = 10.0
    # "Selected model is at capacity" is transient: the same invocation (same provider,
    # same model; never a substitute) is retried after 2, 5 and 10 minutes, at most three
    # times, and only while the activity's own timeout leaves room for real work.
    _capacity_backoff_seconds: tuple[float, ...] = (120.0, 300.0, 600.0)
    _capacity_minimum_work_seconds = 600.0

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
        "change_request.draft": "draft_request",
        "change_request.review": "review_request",
        "ui_plan.draft": "draft_request",
        "ui_plan.review": "review_request",
        "ui_fanout.gap_draft": "draft_request",
        # NEW-154: the gap consolidation plan (Claude, Hermes-pinned output schema).
        "ui_consolidate.plan": "draft_request",
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
        # NEW-226: every run's TMPDIR under temporary_root/<provider>, and its memory bound.
        temp_root = str(self.settings.temporary_root / self.provider.value)
        memory_mib = self.settings.agent_run_memory_max_mib
        memory = memory_mib << 20 if memory_mib is not None else None
        if self.provider is Provider.CODEX:
            return CodexCLIAdapter(
                executable=str(self.settings.codex_executable),
                temp_root=temp_root,
                default_model=self.settings.codex_model,
                run_memory_max_bytes=memory,
            )
        return ClaudeCLIAdapter(
            executable=str(self.settings.claude_executable),
            temp_root=temp_root,
            run_memory_max_bytes=memory,
        )

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step == "quota.refresh":
            return await self._quota_snapshot(payload)
        if step == "delivery.invoke_spec" and payload.get("status") == "SPEC_REUSED":
            # NEW-53: delivery.prepare_spec found this ChangeSet's OpenSpec from an
            # earlier attempt for the same issue content; nothing to invoke.
            return dict(payload)
        request = self._request(step, payload)
        self._validate_boundary(step, request)
        if (
            step in {"ui_plan.draft", "ui_plan.review"}
            and "part" not in payload
            and agent_pack_parts_only(self.settings.agent_pack_root)
        ):
            # NEW-131 review 3: a pre-NEW-131 whole-blueprint call (prepared before the
            # upgrade, replayed now) never reaches a model under the parts-only pack.
            return self._blocked(
                self._without_prompt(payload, self._request_keys[step]),
                "UI_PLAN_LEGACY_SUPERSEDED",
            )
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
                detail=error_detail(error, self.redactor),
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
            # NEW-159: the node/pnpm delivery.prepare_toolchain resolved from the manifest
            # travels in the payload (this worker never reads projects.d). Absent is the
            # default sandbox, exactly as before; a selection not installed here fails
            # closed.
            sandbox_toolchain = payload_node_toolchain(self.settings, payload).sandbox()
        except ToolchainUnavailableError as error:
            return self._blocked(runtime_payload, error.blocker, detail=str(error))
        except ValueError as error:
            return self._blocked(
                runtime_payload,
                "TOOLCHAIN_CONTRACT_INVALID",
                detail=error_detail(error, self.redactor),
            )
        try:
            authenticated = await self.adapter.authenticated()
        except FileNotFoundError:
            return self._blocked(
                runtime_payload, f"PROVIDER_CLI_MISSING_{self.provider.value.upper()}"
            )
        except AuthProbeError as error:
            return self._blocked(
                runtime_payload, f"AUTH_STATUS_{error.reason}_{self.provider.value.upper()}"
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

        # NEW-226: compared with the worker's memory state if the step fails.
        memory_before = memory_snapshot()
        keepalive = asyncio.create_task(heartbeat_keepalive())
        try:
            result = await self._execute(request, heartbeat_event, sandbox_toolchain)
            for retry, delay in enumerate(self._capacity_backoff_seconds, start=1):
                if not self._capacity_retry_allowed(result, delay):
                    break
                # Recorded in the transcript and heartbeat like any provider event.
                await heartbeat_event(
                    {
                        "type": "provider.capacity_retry",
                        "retry": retry,
                        "delay_seconds": delay,
                        "error_code": result.error_code,
                    }
                )
                await asyncio.sleep(delay)
                result = await self._execute(request, heartbeat_event, sandbox_toolchain)
        except BaseException as error:
            # NEW-226: an activity timeout arrives here as a cancellation.
            pressure = memory_pressure_event(memory_before, memory_snapshot())
            if pressure is not None:
                with suppress(Exception):
                    await recorder.event(pressure)
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
        if result.status in {RunStatus.FAILED, RunStatus.TIMED_OUT}:
            # NEW-226: a slow maker reads "memory-throttled", not only a timeout.
            pressure = memory_pressure_event(memory_before, memory_snapshot())
            if pressure is not None:
                await heartbeat_event(pressure)
        try:
            await recorder.finish(
                ExecutionFinishInput(
                    status=result.status.value,
                    phase=phase,
                    ended_at=result.finished_at,
                    exit_code=result.exit_code,
                    retryable=result.status is RunStatus.RATE_LIMITED
                    or result.error_code == PROVIDER_CAPACITY_ERROR,
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
            result = self._ui_page_contract(step, request, result, payload)

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

    async def _execute(
        self,
        request: AgentRequest,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        toolchain: SandboxToolchain | None,
    ) -> AgentResult:
        if toolchain is None:
            # The default toolchain calls the adapter exactly as before NEW-159.
            return await self.adapter.execute(request, on_event)
        return await self.adapter.execute(request, on_event, toolchain=toolchain)

    def _capacity_retry_allowed(self, result: AgentResult, delay: float) -> bool:
        """Retry only a model-at-capacity failure, and only within the activity budget."""
        if result.status is not RunStatus.FAILED or result.error_code != PROVIDER_CAPACITY_ERROR:
            return False
        try:
            info = activity.info()
        except RuntimeError:  # not inside an activity (direct handler use)
            return True
        timeout = info.start_to_close_timeout
        if timeout is None:
            return True
        started = info.started_time
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        remaining = (started + timeout - datetime.now(UTC)).total_seconds()
        return remaining - delay >= self._capacity_minimum_work_seconds

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
        # NEW-146: every step's detail is bounded and redacted here, on the one path.
        detail = bounded_detail(detail, self.redactor) if detail else None
        # NEW-168: a block before (or instead of) the provider call has no result of its
        # own. The payload may still carry the PREVIOUS provider step's agent_result (e.g.
        # the spec's into the maker), which agent.persist_result read as this invocation's
        # result and refused ("provider result identity does not match its immutable
        # request"): a PolicyViolation, a FAILED child and a PLANNED ChangeSet (falcone #975).
        stale = ("agent_result", "observability", "handoff_hashes")
        safe = self.redactor.redact(
            {
                **{key: value for key, value in payload.items() if key not in stale},
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
        probe_failure = None
        authenticated: bool | None
        try:
            authenticated = await self.adapter.authenticated()
            cli_present = True
        except FileNotFoundError:
            authenticated = False
            cli_present = False
        except AuthProbeError as error:
            authenticated = None
            cli_present = True
            probe_failure = f"AUTH_STATUS_{error.reason}_{self.provider.value.upper()}"
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
                probe_failure
                if probe_failure is not None
                else "AVAILABLE"
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
    def _ui_page_contract(
        step: str, request: AgentRequest, result: AgentResult, payload: dict[str, Any]
    ) -> AgentResult:
        """NEW-227: the UI page skills' outputs, valid against their released schemas, become
        the delivery maker result and checker verdict every later step consumes."""
        ui_page = payload.get("ui_page")
        if step == "delivery.invoke_maker" and request.role == UI_PAGE_MAKER_ROLE:
            mapped = maker_result(result.structured_result, ui_page)
        elif step == "delivery.invoke_checker" and request.role == UI_PAGE_CHECKER_ROLE:
            mapped = checker_result(result.structured_result, ui_page)
        else:
            return result
        return result.model_copy(update={"structured_result": mapped})

    _SCHEMA_FINDINGS = 20
    _SCHEMA_FINDING_MESSAGE = 300

    def _schema_findings(self, request: AgentRequest, result: AgentResult) -> list[str]:
        """NEW-132: bounded, redacted validation errors (JSON path, validator keyword and a
        short message), so the drafter's single redraft can fix them."""
        try:
            validator = Draft202012Validator(request.output_schema)
            errors = sorted(
                validator.iter_errors(result.structured_result),
                key=lambda item: [str(part) for part in item.absolute_path],
            )
        except Exception:  # noqa: BLE001 - findings are best effort; the refusal stands
            return []
        specific: list[Any] = []
        for error in errors:
            # An anyOf failure (e.g. the part or null) reports its deepest branch errors.
            nested = sorted(error.context or (), key=lambda item: -len(item.absolute_path))
            specific.extend([item for item in nested if item.absolute_path][:3] or [error])
        findings = []
        for error in specific[: self._SCHEMA_FINDINGS]:
            path = "/".join(str(part) for part in error.absolute_path) or "<root>"
            # Redact the complete text first (path and message both carry model output);
            # truncating first could cut a secret below the redactor's pattern.
            text = self.redactor.redact_text(
                f"{path} [{error.validator}]: {' '.join(str(error.message).split())}"
            ).value
            findings.append(text[: self._SCHEMA_FINDING_MESSAGE])
        return findings

    def _validate_result_schema(self, request: AgentRequest, result: AgentResult) -> None:
        try:
            Draft202012Validator.check_schema(request.output_schema)
            Draft202012Validator(request.output_schema).validate(result.structured_result)
        except (SchemaError, ValidationError) as error:
            # NEW-131 review 2: the call consumed tokens even though its output is refused;
            # carry its reported usage (numbers only) so it is still accounted for.
            raise ApplicationError(
                "provider result failed its immutable output schema",
                {
                    "usage": result.usage.model_dump(mode="json"),
                    "provider": result.provider.value,
                    "run_id": str(result.run_id),
                    "findings": (
                        self._schema_findings(request, result)
                        if isinstance(error, ValidationError)
                        else []
                    ),
                },
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
