from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from engineering_control.adapters.claude import client as claude_module
from engineering_control.adapters.claude.client import ClaudeCLIAdapter
from engineering_control.adapters.process import ProcessResult
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus


@pytest.mark.asyncio
async def test_claude_stream_json_invocation_enables_verbose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, Any] = {}

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        observed["command"] = command
        return ProcessResult(
            exit_code=0,
            events=(
                {
                    "type": "result",
                    "structured_output": {"status": "PASS"},
                    "result": "bounded",
                    "usage": {
                        "input_tokens": 120,
                        "output_tokens": 24,
                        "cache_read_input_tokens": 8,
                    },
                },
            ),
            stderr="",
        )

    monkeypatch.setattr(claude_module, "run_jsonl_process", fake_run)
    request = AgentRequest(
        project=Project.FALCONE,
        role="implementer",
        provider=Provider.CLAUDE,
        model_class=ModelClass.WORKER,
        prompt="perform a bounded synthetic task",
        working_directory=str(tmp_path),
        output_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
        },
        writable=True,
        allowed_tools=("Read", "Edit", "Write"),
    )

    result = await ClaudeCLIAdapter().execute(request)

    command = observed["command"]
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--tools") + 1] == "Read,Edit,Write"
    assert command[command.index("--allowed-tools") + 1] == "Read,Edit,Write"
    cli_schema = json.loads(command[command.index("--json-schema") + 1])
    assert cli_schema == {"type": "object"}
    assert request.output_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "--dangerously-skip-permissions" not in command
    assert result.status is RunStatus.SUCCESS
    assert result.structured_result == {"status": "PASS"}
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 24
    assert result.usage.cached_input_tokens == 8


@pytest.mark.asyncio
async def test_claude_read_only_request_rejects_write_tools(tmp_path: Path) -> None:
    request = AgentRequest(
        project=Project.FALCONE,
        role="code-reviewer",
        provider=Provider.CLAUDE,
        model_class=ModelClass.JUDGE,
        prompt="review a bounded synthetic task",
        working_directory=str(tmp_path),
        output_schema={"type": "object"},
        allowed_tools=("Read", "Write"),
    )

    with pytest.raises(ValueError, match="read-only Claude requests"):
        await ClaudeCLIAdapter().execute(request)


def test_claude_usage_ignores_malformed_or_redacted_counters() -> None:
    usage = ClaudeCLIAdapter._usage(
        (
            {
                "type": "result",
                "usage": {
                    "input_tokens": "[REDACTED]",
                    "output_tokens": True,
                    "cache_read_input_tokens": -1,
                },
            },
        )
    )

    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cached_input_tokens is None


def test_error_result_reason_reaches_the_run_summary() -> None:
    # NEW-31: a live spec run (musematic #1026) exited 1 after 10 minutes; the error
    # result event had no "result" text, so the summary showed only an unrelated stderr
    # warning and the real reason was lost.
    structured, summary, session_id = ClaudeCLIAdapter._final(
        (
            {"type": "system", "session_id": "s-1"},
            {
                "type": "result",
                "subtype": "error_max_structured_output_retries",
                "is_error": True,
                "errors": ["structured output did not match the schema"],
                "session_id": "s-1",
            },
        )
    )

    assert structured == {}
    assert session_id == "s-1"
    assert summary == (
        "claude result error_max_structured_output_retries: "
        "structured output did not match the schema"
    )


_RECOVERY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "scope"],
    "properties": {
        "decision": {"type": "string", "enum": ["READY", "BLOCKED"]},
        "scope": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    },
}


def _structured_attempt(candidate: object) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "returning the result"},
                {"type": "tool_use", "id": "t", "name": "StructuredOutput", "input": candidate},
            ]
        },
    }


_RETRIES_EXHAUSTED = {
    "type": "result",
    "subtype": "error_max_structured_output_retries",
    "is_error": True,
    "errors": ["Failed to provide valid structured output after 5 attempts"],
}


async def _execute_with_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: tuple[dict[str, Any], ...],
    *,
    exit_code: int = 1,
    model: str | None = None,
) -> tuple[Any, list[str]]:
    observed: dict[str, Any] = {}

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        observed["command"] = command
        return ProcessResult(exit_code=exit_code, events=events, stderr="")

    monkeypatch.setattr(claude_module, "run_jsonl_process", fake_run)
    request = AgentRequest(
        project=Project.MUSEMATIC,
        role="product-spec-architect",
        provider=Provider.CLAUDE,
        model_class=ModelClass.JUDGE,
        prompt="produce a bounded spec",
        working_directory=str(tmp_path),
        output_schema=_RECOVERY_SCHEMA,
        model=model,
    )
    result = await ClaudeCLIAdapter().execute(request)
    return result, observed["command"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        {"decision": "READY", "scope": ["cap uploads"]},
        json.dumps({"decision": "READY", "scope": ["cap uploads"]}),
        {"decision": "READY", "scope": json.dumps(["cap uploads"])},
    ],
    ids=["object", "json-string", "stringified-array"],
)
async def test_valid_last_candidate_is_recovered_after_structured_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate: object
) -> None:
    # NEW-53: ~13% of spec runs ended in error_max_structured_output_retries and burned
    # a whole delivery attempt although the last candidate may satisfy our schema.
    result, _ = await _execute_with_events(
        tmp_path,
        monkeypatch,
        (
            _structured_attempt({"decision": "MAYBE"}),
            _structured_attempt(candidate),
            _RETRIES_EXHAUSTED,
        ),
    )

    assert result.status is RunStatus.SUCCESS
    assert result.error_code is None
    assert result.recovered_from_structured_retry is True
    assert result.structured_result == {"decision": "READY", "scope": ["cap uploads"]}
    assert result.summary.startswith("recovered_from_structured_retry")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        (_RETRIES_EXHAUSTED,),
        (_structured_attempt({"decision": "READY", "scope": []}), _RETRIES_EXHAUSTED),
        (_structured_attempt("{not json"), _RETRIES_EXHAUSTED),
        (
            _structured_attempt({"decision": "READY", "scope": ["x"], "extra": 1}),
            _RETRIES_EXHAUSTED,
        ),
        (
            _structured_attempt({"decision": "READY", "scope": ["x"]}),
            {"type": "result", "subtype": "error_max_turns", "is_error": True},
        ),
    ],
    ids=["no-candidate", "invalid", "unparseable", "extra-key", "other-error"],
)
async def test_structured_retry_failure_stands_without_a_valid_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: tuple[dict[str, Any], ...]
) -> None:
    result, _ = await _execute_with_events(tmp_path, monkeypatch, events)

    assert result.status is RunStatus.FAILED
    assert result.error_code == "PROVIDER_EXIT_NONZERO"
    assert result.recovered_from_structured_retry is False
    assert result.structured_result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [None, "claude-opus-4-9"])
async def test_request_model_reaches_the_cli_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str | None
) -> None:
    # NEW-53: claude_spec_model / claude_checker_model travel as AgentRequest.model.
    _, command = await _execute_with_events(
        tmp_path,
        monkeypatch,
        ({"type": "result", "subtype": "success", "structured_output": {}},),
        exit_code=0,
        model=model,
    )

    if model is None:
        assert "--model" not in command
    else:
        assert command[command.index("--model") + 1] == model


def test_successful_result_text_is_still_the_summary() -> None:
    _, summary, _ = ClaudeCLIAdapter._final(
        ({"type": "result", "subtype": "success", "result": "done"},)
    )

    assert summary == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope",
    [
        {"errors": ["API Error: 529 Selected model is at capacity"]},
        {"result": "Selected model is at capacity. Please try again later."},
        {"errors": [{"type": "overloaded_error", "message": "Overloaded"}]},
    ],
    ids=["errors", "result", "overloaded"],
)
async def test_a_capacity_error_result_is_a_transient_capacity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, envelope: dict[str, Any]
) -> None:
    # NEW-114 review: the capacity message only lived in the error result envelope, so
    # it reached the summary but was classified PROVIDER_EXIT_NONZERO (no retry).
    result, _ = await _execute_with_events(
        tmp_path,
        monkeypatch,
        ({"type": "result", "subtype": "error_during_execution", "is_error": True, **envelope},),
    )

    assert result.status is RunStatus.FAILED
    assert result.error_code == "PROVIDER_MODEL_AT_CAPACITY"


@pytest.mark.asyncio
async def test_successful_or_ordinary_failed_results_are_not_capacity_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Agent output mentioning capacity in a non-error result is never inspected.
    success = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "the selected model is at capacity in this test fixture",
    }
    result, _ = await _execute_with_events(tmp_path, monkeypatch, (success,))
    assert result.error_code == "PROVIDER_EXIT_NONZERO"
    failed = {"type": "result", "subtype": "error_during_execution", "is_error": True}
    result, _ = await _execute_with_events(tmp_path, monkeypatch, ({**failed, "errors": ["x"]},))
    assert result.error_code == "PROVIDER_EXIT_NONZERO"
