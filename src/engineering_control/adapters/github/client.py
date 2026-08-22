from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engineering_control.adapters.command import SafeCommandRunner


class GitHubDeliveryAdapter:
    """Fixed GitHub operations for issue search, PR creation, and gate inspection; never merges."""

    def __init__(self, runner: SafeCommandRunner, working_directory: Path) -> None:
        self.runner = runner
        self.working_directory = working_directory

    def search_open_issues(self, repository: str, fingerprint: str) -> list[dict[str, Any]]:
        result = self.runner.run(
            "gh",
            [
                "issue",
                "list",
                "--repo",
                repository,
                "--state",
                "all",
                "--search",
                f'"{fingerprint}"',
                "--json",
                "number,title,url,state",
                "--limit",
                "100",
            ],
            cwd=self.working_directory,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list):
            raise ValueError("GitHub issue search returned an invalid payload")
        return payload

    def create_pull_request(
        self,
        *,
        repository: str,
        head: str,
        base: str,
        title: str,
        body_file: Path,
        draft: bool = True,
    ) -> str:
        if not body_file.is_file():
            raise ValueError("PR body evidence file does not exist")
        arguments = [
            "pr",
            "create",
            "--repo",
            repository,
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body-file",
            str(body_file),
        ]
        if draft:
            arguments.append("--draft")
        return self.runner.run("gh", arguments, cwd=self.working_directory).stdout.strip()

    def required_checks(self, repository: str, pr_number: int) -> dict[str, Any]:
        result = self.runner.run(
            "gh",
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repository,
                "--json",
                "headRefOid,baseRefOid,mergeStateStatus,reviewDecision,statusCheckRollup,url",
            ],
            cwd=self.working_directory,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("GitHub PR inspection returned an invalid payload")
        return payload
