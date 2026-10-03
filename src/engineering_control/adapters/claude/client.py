from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from engineering_control.adapters.auth_probe import AuthProbeError, run_auth_probe
from engineering_control.adapters.process import (
    EventCallback,
    classify_failure,
    run_jsonl_process,
)
from engineering_control.models.core import AgentRequest, AgentResult, ProviderUsage
from engineering_control.models.enums import Provider, RunStatus
from engineering_control.security.redaction import SecretRedactor


class ClaudeCLIAdapter:
    provider = Provider.CLAUDE

    def __init__(self, executable: str = "claude") -> None:
        self.executable = executable

    async def authenticated(self) -> bool:
        result = await run_auth_probe(
            [self.executable, "auth", "status"],
            provider=self.provider.value,
            capture_stdout=True,
        )
        if result.failure is not None:
            raise AuthProbeError(result.failure)
        if result.exit_code != 0:
            return False
        try:
            payload = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError):
            raise AuthProbeError("INVALID_RESPONSE") from None
        if not isinstance(payload, dict):
            raise AuthProbeError("INVALID_RESPONSE")
        states = [payload[key] for key in ("loggedIn", "authenticated") if key in payload]
        if not states or any(not isinstance(state, bool) for state in states):
            raise AuthProbeError("INVALID_RESPONSE")
        return all(state is True for state in states)

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

        cli_schema = dict(request.output_schema)
        if cli_schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema":
            # Claude Code validates the structural schema but rejects the otherwise
            # valid Draft 2020-12 declaration as an unknown meta-schema. Engineering
            # Control still validates the original schema and the returned object
            # with Draft202012Validator at the provider boundary; removing this one
            # annotation from the CLI copy does not remove an output constraint.
            cli_schema.pop("$schema")

        command = [
            self.executable,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            json.dumps(cli_schema, separators=(",", ":"), sort_keys=True),
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
        recovered: dict[str, Any] | None = None
        if process_result.exit_code != 0 and not process_result.timed_out:
            recovered = self._recover_structured_output(
                process_result.events, request.output_schema
            )
        if process_result.exit_code == 0 and not process_result.timed_out:
            status = RunStatus.SUCCESS
            error_code = None
        elif recovered is not None:
            # NEW-53: the CLI exhausted its structured-output retries, but its last
            # candidate satisfies the ORIGINAL schema (validated again at the provider
            # boundary), so the attempt is not burned on the CLI's stricter verdict.
            status = RunStatus.SUCCESS
            error_code = None
            structured_result = recovered
            summary = (
                "recovered_from_structured_retry: accepted the last StructuredOutput "
                f"candidate after the CLI reported: {summary}"
            )[:4000]
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
            recovered_from_structured_retry=recovered is not None,
        )

    _STRUCTURED_RETRY_SUBTYPE = "error_max_structured_output_retries"

    @classmethod
    def _recover_structured_output(
        cls, events: tuple[dict[str, Any], ...], schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        """NEW-53: the last StructuredOutput candidate, if it passes the original schema.

        Only used when the final result is error_max_structured_output_retries. The
        candidate is the input of the last StructuredOutput tool_use in the stream
        (a JSON string input is decoded once). Returns None when there is no candidate
        or it does not validate, so the attempt still fails exactly as before.
        """
        final = next((event for event in reversed(events) if event.get("type") == "result"), None)
        if final is None or final.get("subtype") != cls._STRUCTURED_RETRY_SUBTYPE:
            return None
        candidate = cls._last_structured_candidate(events)
        if candidate is None:
            return None
        try:
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            if not validator.is_valid(candidate):
                candidate = cls._decode_stringified_arrays(candidate, schema)
                if candidate is None:
                    return None
                validator.validate(candidate)
        except (SchemaError, ValidationError):
            return None
        redacted = SecretRedactor().redact(candidate).value
        return redacted if isinstance(redacted, dict) else None

    @staticmethod
    def _last_structured_candidate(events: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
        for event in reversed(events):
            if event.get("type") != "assistant":
                continue
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in reversed(content):
                if (
                    not isinstance(block, dict)
                    or block.get("type") != "tool_use"
                    or block.get("name") != "StructuredOutput"
                ):
                    continue
                value = block.get("input")
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        return None
                return value if isinstance(value, dict) else None
        return None

    @staticmethod
    def _decode_stringified_arrays(
        candidate: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Decode top-level array properties the model emitted as JSON-encoded strings.

        A frequent structured-output slip on large documents; the decoded object is
        validated against the full original schema afterwards, so nothing is loosened.
        """
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return None
        decoded = dict(candidate)
        changed = False
        for key, value in candidate.items():
            spec = properties.get(key)
            if not isinstance(spec, dict) or spec.get("type") != "array":
                continue
            if not isinstance(value, str):
                continue
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            if isinstance(parsed, list):
                decoded[key] = parsed
                changed = True
        return decoded if changed else None

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
                elif event.get("is_error") or event.get("subtype") not in {None, "success"}:
                    # NEW-31: error results (max turns, structured-output retries, ...)
                    # carry no "result" text, so the reason was lost and only an
                    # unrelated stderr warning reached the run summary.
                    errors = event.get("errors")
                    detail = f"claude result {event.get('subtype') or 'error'}"
                    if isinstance(errors, list) and errors:
                        detail += ": " + "; ".join(str(error) for error in errors)[:2000]
                    summary = str(SecretRedactor().redact_text(detail).value)
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
