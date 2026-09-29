from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest

from engineering_control.adapters.codex import client as codex_module
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.adapters.process import ProcessResult
from engineering_control.config import Settings
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_model", "expected"),
    [(None, "gpt-6-sol"), ("gpt-5.5", "gpt-5.5")],
)
async def test_codex_adapter_pins_the_configured_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_model: str | None,
    expected: str,
) -> None:
    # NEW-36: without --model the CLI's own default applied; codex-cli 0.149.0 defaulted
    # to gpt-5.4, which the ChatGPT account refuses, failing the first live maker run.
    working_directory = tmp_path / "worktree"
    working_directory.mkdir()
    observed: dict[str, Any] = {}

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        observed["command"] = command
        final_path = Path(command[command.index("--output-last-message") + 1])
        final_path.write_text('{"summary":"bounded"}', encoding="utf-8")  # noqa: ASYNC240
        return ProcessResult(exit_code=0, events=(), stderr="")

    monkeypatch.setattr(codex_module, "run_jsonl_process", fake_run)
    request = AgentRequest(
        project=Project.MUSEMATIC,
        role="implementer",
        provider=Provider.CODEX,
        model_class=ModelClass.WORKER,
        prompt="perform a bounded synthetic task",
        working_directory=str(working_directory),
        output_schema={"type": "object"},
        model=request_model,
    )
    adapter = CodexCLIAdapter(
        temp_root=str(tmp_path / "codex-temp"),
        default_model=Settings().codex_model,
    )

    await adapter.execute(request)

    command = observed["command"]
    assert command[command.index("--model") + 1] == expected


def _linked_worktree(repository: Path, worktree: Path) -> None:
    """Create a real linked git worktree, as the worktree manager does."""
    import shutil
    import subprocess

    git = shutil.which("git") or "git"
    for arguments in (
        ["init", "-q", str(repository)],
        [
            "-C",
            str(repository),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        ["-C", str(repository), "worktree", "add", "-q", "--detach", str(worktree)],
    ):
        subprocess.run([git, *arguments], check=True)  # noqa: S603 - fixed Git executable in test harness


@pytest.mark.asyncio
@pytest.mark.parametrize("writable", [True, False])
async def test_codex_maker_can_write_its_linked_worktree_git_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writable: bool
) -> None:
    # NEW-37: the first live Codex maker (musematic #1026) changed 9 files and passed
    # its tests but could not commit: the linked worktree's index lives outside the
    # workspace-write sandbox ("Git cannot create its index.lock").
    repository = tmp_path / "metadata"
    worktree = tmp_path / "worktrees" / "change"
    _linked_worktree(repository, worktree)
    observed: dict[str, Any] = {}

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        observed["command"] = command
        final_path = Path(command[command.index("--output-last-message") + 1])
        final_path.write_text('{"summary":"bounded"}', encoding="utf-8")  # noqa: ASYNC240
        return ProcessResult(exit_code=0, events=(), stderr="")

    monkeypatch.setattr(codex_module, "run_jsonl_process", fake_run)
    request = AgentRequest(
        project=Project.MUSEMATIC,
        role="implementer",
        provider=Provider.CODEX,
        model_class=ModelClass.WORKER,
        prompt="perform a bounded synthetic task",
        working_directory=str(worktree),
        output_schema={"type": "object"},
        writable=writable,
    )

    await CodexCLIAdapter(temp_root=str(tmp_path / "codex-temp")).execute(request)

    command = observed["command"]
    added = [command[i + 1] for i, part in enumerate(command) if part == "--add-dir"]
    git_dir = (repository / ".git").resolve()
    if writable:
        assert str(git_dir / "worktrees" / "change") in added
        assert str(git_dir) in added
    else:
        assert added == []
