from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from engineering_control.adapters.command import SafeCommandRunner


@dataclass(frozen=True)
class ConsumerCommandResult:
    command: str
    status: str
    return_code: int


class LLMWikiFalconeConsumerSuite:
    """Run only repository-declared LLMWiki gates with an in-memory Falcone session."""

    _commands = {
        "lint": ("pnpm", ("lint",)),
        "test": ("pnpm", ("test",)),
        "smoke": ("pnpm", ("run", "smoke")),
        "e2e": ("pnpm", ("test:e2e",)),
        "s14": ("pnpm", ("run", "gate:s14")),
        "s14-integration": ("pnpm", ("run", "gate:s14:integration")),
    }

    def __init__(self, runner: SafeCommandRunner) -> None:
        self.runner = runner

    def run(
        self,
        *,
        source_worktree: Path,
        command_ids: tuple[str, ...],
        falcone_access_token: str,
        falcone_api_base_url: str,
        campaign_id: str,
    ) -> list[ConsumerCommandResult]:
        root = source_worktree.resolve()
        if not root.is_dir() or not (root / "pnpm-lock.yaml").is_file():
            raise ValueError("LLMWiki consumer worktree is invalid")
        if not command_ids or len(set(command_ids)) != len(command_ids):
            raise ValueError("LLMWiki consumer commands must be unique and non-empty")
        if any(command not in self._commands for command in command_ids):
            raise ValueError("LLMWiki consumer command is not allowlisted")
        if not falcone_access_token or not falcone_api_base_url.startswith("https://"):
            raise ValueError("LLMWiki consumer suite requires an ephemeral staging session")
        environment = {
            "FALCONE_ACCESS_TOKEN": falcone_access_token,
            "FALCONE_API_BASE_URL": falcone_api_base_url,
            "ENGINEERING_E2E_CAMPAIGN_ID": campaign_id,
            "JOURNEY_SAFE_E2E_MUTATION": "false",
            # Run the reviewed browser-worker pnpm binary. pnpm 10 otherwise
            # self-provisions the target repository's packageManager version,
            # which can silently reintroduce an older vulnerable executable.
            "npm_config_manage_package_manager_versions": "false",
        }
        for value in environment.values():
            self.runner.redactor.register_secret(value)
        try:
            results: list[ConsumerCommandResult] = []
            for command_id in command_ids:
                executable, arguments = self._commands[command_id]
                result = self.runner.run(
                    executable,
                    list(arguments),
                    cwd=root,
                    environment=environment,
                    timeout_seconds=3600,
                    check=False,
                )
                results.append(
                    ConsumerCommandResult(
                        command=command_id,
                        status="PASS" if result.return_code == 0 else "FAIL",
                        return_code=result.return_code,
                    )
                )
                if result.return_code != 0:
                    break
            return results
        finally:
            for value in environment.values():
                self.runner.redactor.unregister_secret(value)
