from __future__ import annotations

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
        output_schema={"type": "object"},
    )

    result = await ClaudeCLIAdapter().execute(request)

    command = observed["command"]
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command
    assert result.status is RunStatus.SUCCESS
    assert result.structured_result == {"status": "PASS"}
    assert result.usage.input_tokens == 120
    assert result.usage.output_tokens == 24
    assert result.usage.cached_input_tokens == 8


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
