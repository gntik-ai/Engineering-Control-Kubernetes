from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engineering_control.adapters.process import EventCallback, classify_failure, run_jsonl_process
from engineering_control.models.core import AgentRequest, AgentResult, ProviderUsage
from engineering_control.models.enums import Provider, RunStatus
from engineering_control.security.redaction import SecretRedactor


class CodexCLIAdapter:
    provider = Provider.CODEX

    def __init__(self, executable: str = "codex", temp_root: str | None = None) -> None:
        self.executable = executable
        self.temp_root = temp_root

    async def authenticated(self) -> bool:
        import asyncio

        process = await asyncio.create_subprocess_exec(
            self.executable,
            "login",
            "status",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await process.wait() == 0

    async def execute(
        self, request: AgentRequest, on_event: EventCallback | None = None
    ) -> AgentResult:
        if request.provider is not self.provider:
            raise ValueError("Codex adapter received a non-Codex request")
        started_at = datetime.now(UTC)
        temp_parent = Path(self.temp_root) if self.temp_root else None
        with tempfile.TemporaryDirectory(prefix=f"codex-{request.run_id}-", dir=temp_parent) as raw:
            temp = Path(raw)
            os.chmod(temp, 0o700)
            schema_path = temp / "output-schema.json"
            final_path = temp / "final.json"
            schema_path.write_text(
                json.dumps(request.output_schema, sort_keys=True), encoding="utf-8"
            )
            os.chmod(schema_path, 0o600)

            command = [
                self.executable,
                "exec",
                "--json",
                "--color",
                "never",
                "--ephemeral",
                "--strict-config",
                "--cd",
                request.working_directory,
                "--sandbox",
                "workspace-write" if request.writable else "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(final_path),
            ]
            if request.model:
                command.extend(("--model", request.model))
            if request.effort:
                command.extend(("--config", f'model_reasoning_effort="{request.effort}"'))
            command.append("-")

            process_result = await run_jsonl_process(
                command,
                prompt=request.prompt,
                cwd=request.working_directory,
                timeout_seconds=request.timeout_seconds,
                provider=self.provider.value,
                on_event=on_event,
            )
            structured_result: dict[str, Any] = {}
            summary = ""
            if final_path.exists():
                safe = SecretRedactor().redact_text(final_path.read_text(encoding="utf-8"))
                summary = str(safe.value).strip()
                try:
                    parsed = json.loads(summary)
                    if isinstance(parsed, dict):
                        structured_result = SecretRedactor().redact(parsed).value
                except json.JSONDecodeError:
                    pass

            usage = self._usage(process_result.events)
            session_id = self._session_id(process_result.events)
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
    def _session_id(events: tuple[dict[str, Any], ...]) -> str | None:
        for event in events:
            if event.get("type") in {"thread.started", "session.started"}:
                candidate = event.get("thread_id") or event.get("session_id")
                if isinstance(candidate, str):
                    return candidate
        return None

    @staticmethod
    def _usage(events: tuple[dict[str, Any], ...]) -> ProviderUsage:
        values: dict[str, int] = {}
        for event in events:
            usage = event.get("usage")
            if isinstance(usage, dict):
                for target, alternatives in {
                    "input_tokens": ("input_tokens", "inputTokens"),
                    "output_tokens": ("output_tokens", "outputTokens"),
                    "cached_input_tokens": ("cached_input_tokens", "cachedInputTokens"),
                }.items():
                    for source in alternatives:
                        if isinstance(usage.get(source), int):
                            values[target] = usage[source]
                            break
        return ProviderUsage(
            input_tokens=values.get("input_tokens"),
            output_tokens=values.get("output_tokens"),
            cached_input_tokens=values.get("cached_input_tokens"),
        )
