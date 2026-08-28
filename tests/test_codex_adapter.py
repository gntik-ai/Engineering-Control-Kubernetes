from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from engineering_control.adapters.codex import client as codex_module
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.adapters.process import ProcessResult
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus


@pytest.mark.asyncio
async def test_codex_adapter_uses_deterministic_per_run_temp_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_root = tmp_path / "codex-temp"
    working_directory = tmp_path / "worktree"
    working_directory.mkdir()
    observed: dict[str, Any] = {}

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        observed["command"] = command
        schema_path = Path(command[command.index("--output-schema") + 1])
        final_path = Path(command[command.index("--output-last-message") + 1])
        observed["attempt_root"] = schema_path.parent
        assert schema_path.is_file()  # noqa: ASYNC240 - local synchronous test double
        final_path.write_text(  # noqa: ASYNC240 - local synchronous test double
            '{"summary":"bounded"}', encoding="utf-8"
        )
        return ProcessResult(exit_code=0, events=(), stderr="")

    monkeypatch.setattr(codex_module, "run_jsonl_process", fake_run)
    request = AgentRequest(
        project=Project.FALCONE,
        role="implementer",
        provider=Provider.CODEX,
        model_class=ModelClass.WORKER,
        prompt="perform a bounded synthetic task",
        working_directory=str(working_directory),
        output_schema={"type": "object"},
        skip_git_repository_check=True,
    )
    result = await CodexCLIAdapter(temp_root=str(temp_root)).execute(request)

    run_root = temp_root / str(request.run_id)
    assert result.status is RunStatus.SUCCESS
    assert run_root.is_dir()
    assert stat.S_IMODE(run_root.stat().st_mode) == 0o700
    assert observed["attempt_root"].parent == run_root
    assert "--skip-git-repo-check" in observed["command"]
    assert list(run_root.iterdir()) == []


def test_codex_adapter_rejects_symlinked_run_temp_parent(tmp_path: Path) -> None:
    temp_root = tmp_path / "codex-temp"
    temp_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    request = AgentRequest(
        project=Project.FALCONE,
        role="implementer",
        provider=Provider.CODEX,
        model_class=ModelClass.WORKER,
        prompt="perform a bounded synthetic task",
        working_directory=str(tmp_path),
        output_schema={"type": "object"},
    )
    (temp_root / str(request.run_id)).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        CodexCLIAdapter(temp_root=str(temp_root))._run_temp_parent(request.run_id)


def test_codex_failure_summary_uses_only_redacted_error_event_payload() -> None:
    marker = "Bearer " + "A" * 20
    summary = CodexCLIAdapter._failure_summary(
        (
            {"type": "thread.started", "thread_id": "bounded"},
            {
                "type": "turn.failed",
                "error": {"message": f"provider rejected {marker}", "code": "invalid_request"},
                "unrelated": "must-not-be-persisted",
            },
        ),
        "stderr fallback",
    )

    assert marker not in summary
    assert "[REDACTED]" in summary
    assert "invalid_request" in summary
    assert "must-not-be-persisted" not in summary
