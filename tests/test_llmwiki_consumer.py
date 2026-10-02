from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from engineering_control.adapters.command import CommandResult
from engineering_control.adapters.llmwiki.consumer import LLMWikiFalconeConsumerSuite
from engineering_control.security.redaction import SecretRedactor


class CapturingRunner:
    def __init__(self) -> None:
        self.redactor = SecretRedactor()
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        executable: str,
        arguments: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: int,
        check: bool,
    ) -> CommandResult:
        self.calls.append(
            {
                "executable": executable,
                "arguments": arguments,
                "cwd": cwd,
                "environment": dict(environment),
                "timeout_seconds": timeout_seconds,
                "check": check,
            }
        )
        return CommandResult(return_code=0, stdout="", stderr="")


def test_consumer_commands_pin_reviewed_pnpm_without_project_version_handoff(
    tmp_path: Path,
) -> None:
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    runner = CapturingRunner()
    suite = LLMWikiFalconeConsumerSuite(runner)  # type: ignore[arg-type]
    command_ids = ("lint", "test", "smoke", "e2e", "s14", "s14-integration")
    acceptance_token = uuid4().hex

    results = suite.run(
        source_worktree=tmp_path,
        command_ids=command_ids,
        falcone_access_token=acceptance_token,
        falcone_api_base_url="https://falcone.invalid",
        campaign_id="acceptance-campaign",
    )

    assert [result.command for result in results] == list(command_ids)
    assert len(runner.calls) == len(command_ids)
    assert all(call["executable"] == "pnpm" for call in runner.calls)
    assert all(
        call["environment"]["npm_config_manage_package_manager_versions"] == "false"
        for call in runner.calls
    )
    assert all(call["environment"]["JOURNEY_SAFE_E2E_MUTATION"] == "false" for call in runner.calls)
