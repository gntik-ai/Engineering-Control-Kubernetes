from __future__ import annotations

import json
import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from engineering_control.adapters.command import CommandFailure, CommandResult, SafeCommandRunner

ALLOWED_REPOSITORIES = frozenset(
    {
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
        "gntik-ai/musematic",
        "gntik-ai/musematic-deploy",
        "gntik-ai/llmwiki",
        "gntik-ai/llmwiki-contracts",
    }
)
READ_ONLY_REPOSITORIES = frozenset({"gntik-ai/llmwiki-contracts"})
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/@+ =,\"'-]{1,500}$")


class GhCliAuthError(RuntimeError):
    """The authenticated gh identity is missing or no longer usable."""


class GhCliPolicyError(ValueError):
    """A requested GitHub operation is outside the typed policy surface."""


@dataclass(frozen=True)
class GhIdentity:
    login: str
    account_type: str


class GhCliBroker:
    """Typed GitHub broker that never extracts or handles an OAuth token."""

    def __init__(
        self,
        runner: SafeCommandRunner,
        working_directory: Path,
        *,
        writes_enabled: bool = False,
        authenticated_home: Path | None = None,
        maximum_json_bytes: int = 2_000_000,
    ) -> None:
        self.runner = runner
        self.working_directory = working_directory
        self.writes_enabled = writes_enabled
        self.maximum_json_bytes = maximum_json_bytes
        self.authenticated_home = authenticated_home or Path(pwd.getpwuid(os.geteuid()).pw_dir)

    def preflight(self, repositories: tuple[str, ...] = ()) -> GhIdentity:
        self._run(["auth", "status", "--hostname", "github.com"], check_auth=True)
        value = self._json(
            self._run(["api", "user", "--jq", "{login: .login, type: .type}"]).stdout,
            "GitHub identity",
        )
        if not isinstance(value, dict) or not isinstance(value.get("login"), str):
            raise GhCliAuthError("gh api user returned an invalid identity")
        for repository in repositories:
            self.repository_metadata(repository)
        return GhIdentity(login=value["login"], account_type=str(value.get("type", "UNKNOWN")))

    def repository_metadata(self, repository: str) -> dict[str, Any]:
        repository = self._repository(repository)
        value = self._json(
            self._run(
                [
                    "repo",
                    "view",
                    repository,
                    "--json",
                    "nameWithOwner,defaultBranchRef,viewerPermission,isPrivate",
                ]
            ).stdout,
            "repository metadata",
        )
        if not isinstance(value, dict) or value.get("nameWithOwner") != repository:
            raise GhCliPolicyError("repository access response did not match the requested target")
        return value

    def search_issues(self, repository: str, query: str) -> list[dict[str, Any]]:
        repository = self._batch(repository)
        query = self._value(query, "issue query")
        value = self._json(
            self._run(
                [
                    "issue",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "all",
                    "--search",
                    query,
                    "--json",
                    "number,title,url,state",
                    "--limit",
                    "100",
                ]
            ).stdout,
            "issue search",
        )
        return self._list(value, "issue search")

    def view_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        repository = self._batch(repository)
        if number < 1:
            raise GhCliPolicyError("pull request number is invalid")
        value = self._json(
            self._run(
                [
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    repository,
                    "--json",
                    "number,url,state,isDraft,headRefName,headRefOid,baseRefName,mergeStateStatus,reviewDecision,statusCheckRollup",
                ]
            ).stdout,
            "pull request",
        )
        if not isinstance(value, dict):
            raise GhCliPolicyError("pull request response is not an object")
        return value

    def list_pull_requests(
        self, repository: str, *, head: str, state: Literal["open", "closed", "merged", "all"]
    ) -> list[dict[str, Any]]:
        repository = self._batch(repository)
        head = self._value(head, "pull request head")
        value = self._json(
            self._run(
                [
                    "pr",
                    "list",
                    "--repo",
                    repository,
                    "--head",
                    head,
                    "--state",
                    state,
                    "--json",
                    "number,url,state,isDraft,headRefOid,baseRefName",
                    "--limit",
                    "10",
                ]
            ).stdout,
            "pull request list",
        )
        return self._list(value, "pull request list")

    def workflow_run(self, repository: str, run_id: int) -> dict[str, Any]:
        repository = self._batch(repository)
        if run_id < 1:
            raise GhCliPolicyError("workflow run ID is invalid")
        value = self._json(
            self._run(
                [
                    "run",
                    "view",
                    str(run_id),
                    "--repo",
                    repository,
                    "--json",
                    "databaseId,headSha,conclusion,status,event,workflowName,url",
                ]
            ).stdout,
            "workflow run",
        )
        if not isinstance(value, dict):
            raise GhCliPolicyError("workflow run response is not an object")
        return value

    def download_run_artifact(
        self, repository: str, run_id: int, artifact_name: str, destination: Path
    ) -> None:
        repository = self._batch(repository)
        if run_id < 1 or not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", artifact_name):
            raise GhCliPolicyError("workflow artifact request is invalid")
        resolved = destination.resolve()
        if not resolved.is_dir() or not resolved.is_relative_to(self.working_directory.resolve()):
            raise GhCliPolicyError("workflow artifact destination escaped the run root")
        self._run(
            [
                "run",
                "download",
                str(run_id),
                "--repo",
                repository,
                "--name",
                artifact_name,
                "--dir",
                str(resolved),
            ]
        )

    def verify_attestation(
        self,
        *,
        repository: str,
        subject: str,
        signer_workflow: str,
        source_digest: str,
        deny_self_hosted_runners: bool,
        docker_config: Path,
    ) -> list[dict[str, Any]]:
        repository = self._batch(repository)
        arguments = [
            "attestation",
            "verify",
            self._value(subject, "attestation subject"),
            "--repo",
            repository,
            "--signer-workflow",
            self._value(signer_workflow, "signer workflow"),
            "--source-digest",
            self._value(source_digest, "source digest"),
            "--format",
            "json",
        ]
        if deny_self_hosted_runners:
            arguments.append("--deny-self-hosted-runners")
        result = self._run_with_environment(arguments, {"DOCKER_CONFIG": str(docker_config)})
        return self._list(self._json(result.stdout, "attestation"), "attestation")

    def branch_commit(self, repository: str, branch: str) -> dict[str, str]:
        repository = self._batch(repository)
        branch = self._value(branch, "branch", maximum=120)
        # The API path is constructed exclusively from the repository allowlist and
        # validated branch. No caller can supply an arbitrary API route.
        path = f"repos/{repository}/commits/{branch}"
        value = self._json(
            self._run(
                [
                    "api",
                    path,
                    "--jq",
                    "{sha: .sha, tree: .commit.tree.sha}",
                ]
            ).stdout,
            "branch commit",
        )
        if not isinstance(value, dict):
            raise GhCliPolicyError("branch commit response is not an object")
        sha, tree = value.get("sha"), value.get("tree")
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise GhCliPolicyError("branch commit SHA is invalid")
        if not isinstance(tree, str) or not re.fullmatch(r"[a-f0-9]{40}", tree):
            raise GhCliPolicyError("branch tree SHA is invalid")
        return {"sha": sha, "tree": tree}

    def create_issue(
        self, *, repository: str, title: str, body_file: Path, change_binding: str
    ) -> str:
        repository = self._write(repository, change_binding)
        return self._run(
            [
                "issue",
                "create",
                "--repo",
                repository,
                "--title",
                self._value(title, "issue title", maximum=200),
                "--body-file",
                str(self._bounded_file(body_file)),
            ]
        ).stdout.strip()

    def create_pull_request(
        self,
        *,
        repository: str,
        head: str,
        base: str,
        title: str,
        body_file: Path,
        change_binding: str,
        draft: bool = True,
    ) -> str:
        repository = self._write(repository, change_binding)
        arguments = [
            "pr",
            "create",
            "--repo",
            repository,
            "--head",
            self._value(head, "pull request head"),
            "--base",
            self._value(base, "pull request base"),
            "--title",
            self._value(title, "pull request title", maximum=240),
            "--body-file",
            str(self._bounded_file(body_file)),
        ]
        if draft:
            arguments.append("--draft")
        return self._run(arguments).stdout.strip()

    def search_open_issues(self, repository: str, fingerprint: str) -> list[dict[str, Any]]:
        return self.search_issues(repository, f'"{fingerprint}"')

    def required_checks(self, repository: str, pr_number: int) -> dict[str, Any]:
        return self.view_pull_request(repository, pr_number)

    def _batch(self, repository: str) -> str:
        repository = self._repository(repository)
        self.preflight((repository,))
        return repository

    def _write(self, repository: str, change_binding: str) -> str:
        repository = self._batch(repository)
        if repository in READ_ONLY_REPOSITORIES:
            raise GhCliPolicyError("read-only companion repository rejects writes")
        if not self.writes_enabled:
            raise GhCliPolicyError("GitHub writes are disabled by policy")
        if not re.fullmatch(
            r"(?:changeset|campaign|finding|enhancement):[A-Za-z0-9-]{8,200}", change_binding
        ):
            raise GhCliPolicyError("GitHub write is not bound to an approved control-plane record")
        return repository

    def _run(self, arguments: list[str], *, check_auth: bool = False) -> CommandResult:
        forbidden = {"secret", "environment", "variable", "collaborator"}
        if "auth" in arguments and "token" in arguments:
            raise GhCliPolicyError("gh auth token is prohibited")
        if any(part in forbidden for part in arguments) and not check_auth:
            raise GhCliPolicyError("GitHub administrative operation is prohibited")
        environment = {
            "HOME": str(self.authenticated_home),
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
        }
        try:
            return self.runner.run(
                "gh",
                arguments,
                cwd=self.working_directory,
                timeout_seconds=120,
                environment=environment,
            )
        except CommandFailure as error:
            if check_auth or arguments[:2] == ["api", "user"]:
                raise GhCliAuthError("GitHub CLI authentication is unavailable") from error
            raise

    def _run_with_environment(self, arguments: list[str], extra: dict[str, str]) -> CommandResult:
        environment = {
            "HOME": str(self.authenticated_home),
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            **extra,
        }
        return self.runner.run(
            "gh",
            arguments,
            cwd=self.working_directory,
            timeout_seconds=900,
            environment=environment,
        )

    def _json(self, raw: str, label: str) -> Any:
        if len(raw.encode()) > self.maximum_json_bytes:
            raise GhCliPolicyError(f"{label} exceeded the bounded JSON size")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise GhCliPolicyError(f"{label} was not valid JSON") from error

    @staticmethod
    def _list(value: Any, label: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > 100:
            raise GhCliPolicyError(f"{label} response is not a bounded list")
        if any(not isinstance(item, dict) for item in value):
            raise GhCliPolicyError(f"{label} list contains a non-object")
        return value

    @staticmethod
    def _repository(repository: str) -> str:
        if repository not in ALLOWED_REPOSITORIES:
            raise GhCliPolicyError("repository is outside the GitHub allowlist")
        return repository

    @staticmethod
    def _value(value: str, label: str, *, maximum: int = 500) -> str:
        if len(value) > maximum or not _SAFE_VALUE.fullmatch(value):
            raise GhCliPolicyError(f"{label} contains forbidden shell or control characters")
        if any(marker in value for marker in ("$(`", "$(", "${", ";", "&&", "||", "\n", "\r")):
            raise GhCliPolicyError(f"{label} contains shell metacharacters")
        return value

    def _bounded_file(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(self.working_directory.resolve()):
            raise GhCliPolicyError("GitHub body file escaped the broker working directory")
        return resolved


GitHubDeliveryAdapter = GhCliBroker
