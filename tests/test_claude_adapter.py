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
