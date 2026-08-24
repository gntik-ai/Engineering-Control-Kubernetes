from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from engineering_control.adapters.process import (
    EventCallback,
    classify_failure,
    run_jsonl_process,
    subscription_only_environment,
)
from engineering_control.models.core import AgentRequest, AgentResult, ProviderUsage
from engineering_control.models.enums import Provider, RunStatus
from engineering_control.security.redaction import SecretRedactor


class ClaudeCLIAdapter:
    provider = Provider.CLAUDE

    def __init__(self, executable: str = "claude") -> None:
        self.executable = executable

    async def authenticated(self) -> bool:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "auth",
            "status",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=subscription_only_environment(self.provider.value),
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return False
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return bool(stdout.strip())
        return bool(payload.get("loggedIn") or payload.get("authenticated"))

    async def execute(
        self, request: AgentRequest, on_event: EventCallback | None = None
    ) -> AgentResult:
        if request.provider is not self.provider:
            raise ValueError("Claude adapter received a non-Claude request")
        started_at = datetime.now(UTC)
        tools = request.allowed_tools or (
            ("Read", "Glob", "Grep", "Bash", "Edit", "Write")
            if request.writable
            else ("Read", "Glob", "Grep", "Bash")
        )
        if not request.writable and any(
            tool in {"Edit", "Write", "NotebookEdit"} for tool in tools
        ):
            raise ValueError("read-only Claude requests cannot enable write tools")

        command = [
            self.executable,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            json.dumps(request.output_schema, separators=(",", ":"), sort_keys=True),
            "--no-session-persistence",
            "--safe-mode",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--permission-mode",
            "dontAsk",
            "--tools",
            ",".join(tools),
            "--allowed-tools",
            ",".join(tools),
        ]
        if request.model:
            command.extend(("--model", request.model))
        if request.effort:
            command.extend(("--effort", request.effort))

        process_result = await run_jsonl_process(
            command,
            prompt=request.prompt,
            cwd=request.working_directory,
            timeout_seconds=request.timeout_seconds,
            provider=self.provider.value,
            on_event=on_event,
        )
        structured_result, summary, session_id = self._final(process_result.events)
        usage = self._usage(process_result.events)
        if process_result.exit_code == 0 and not process_result.timed_out:
            status = RunStatus.SUCCESS
            error_code = None
        else:
            raw_status, error_code = classify_failure(process_result)
            status = RunStatus(raw_status)
            if not summary:
                summary = process_result.stderr[-4000:]
        return AgentResult(
            run_id=request.run_id,
            provider=self.provider,
            role=request.role,
            status=status,
            summary=summary,
            usage=usage,
            structured_result=structured_result,
            provider_session_id=session_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            exit_code=process_result.exit_code,
            error_code=error_code,
        )

    @staticmethod
    def _final(events: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], str, str | None]:
        structured: dict[str, Any] = {}
        summary = ""
        session_id: str | None = None
        for event in events:
            candidate_session = event.get("session_id")
            if isinstance(candidate_session, str):
                session_id = candidate_session
            if event.get("type") == "result":
                candidate = event.get("structured_output")
                if isinstance(candidate, dict):
                    structured = SecretRedactor().redact(candidate).value
                result_text = event.get("result")
                if isinstance(result_text, str):
                    summary = str(SecretRedactor().redact_text(result_text).value)
        return structured, summary, session_id

    @staticmethod
    def _usage(events: tuple[dict[str, Any], ...]) -> ProviderUsage:
        def counter(value: Any) -> int | None:
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )

        for event in reversed(events):
            usage = event.get("usage")
            if isinstance(usage, dict):
                return ProviderUsage(
                    input_tokens=counter(usage.get("input_tokens")),
                    output_tokens=counter(usage.get("output_tokens")),
                    cached_input_tokens=counter(usage.get("cache_read_input_tokens")),
                )
        return ProviderUsage()
