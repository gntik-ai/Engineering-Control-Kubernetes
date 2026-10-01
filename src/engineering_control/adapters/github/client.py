from __future__ import annotations

import base64
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
    }
)
READ_ONLY_REPOSITORIES: frozenset[str] = frozenset()
# NEW-63: the only content Hermes writes to a CI baseline branch.
BASELINE_MARKER_CONTENT = (
    "# Hermes CI baseline marker: makes path-filtered CI jobs run on unchanged main.\n"
    "# Lives only on hermes/ci-baseline/* branches; never merged.\n"
)
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/@+ =,\"'-]{1,500}$")
STAGING_GITOPS_MARKER = re.compile(
    r"<!-- engineering-control-staging-gitops:[a-z-]{1,40}:[a-f0-9-]{36}(?::[a-z0-9-]{1,40})? -->"
)


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

    def list_open_issues(self, repository: str) -> list[dict[str, Any]]:
        """Return the bounded structured fields required by backlog import."""
        repository = self._batch(repository)
        value = self._json(
            self._run(
                [
                    "issue",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "open",
                    "--json",
                    "number,title,body,labels,milestone,assignees,comments,url,state,updatedAt",
                    "--limit",
                    "1000",
                ]
            ).stdout,
            "open issue backlog",
        )
        return self._list(value, "open issue backlog", maximum=1000)

    def list_repository_pull_requests(self, repository: str) -> list[dict[str, Any]]:
        """Return open PRs used for existing-work adoption without a history-wide query."""
        repository = self._batch(repository)
        value = self._json(
            self._run(
                [
                    "pr",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "open",
                    "--json",
                    (
                        "number,title,body,url,state,isDraft,headRefName,headRefOid,"
                        "baseRefName,mergeStateStatus,updatedAt,mergedAt,closedAt"
                    ),
                    "--limit",
                    "100",
                ]
            ).stdout,
            "pull request backlog",
        )
        return self._list(value, "pull request backlog", maximum=100)

    def view_backlog_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        """Return bounded existing-PR linkage data using one fixed structured command."""
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
                    (
                        "number,title,body,url,state,isDraft,headRefName,headRefOid,"
                        "baseRefName,mergeStateStatus,updatedAt,mergedAt,closedAt,"
                        "comments,commits"
                    ),
                ]
            ).stdout,
            "backlog pull request",
        )
        if not isinstance(value, dict):
            raise GhCliPolicyError("backlog pull request response is not an object")
        comments = value.get("comments", [])
        commits = value.get("commits", [])
        if (
            not isinstance(comments, list)
            or len(comments) > 250
            or not isinstance(commits, list)
            or len(commits) > 250
        ):
            raise GhCliPolicyError("backlog pull request nested response is unbounded")
        return value

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
                    (
                        "number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
                        "mergeStateStatus,reviewDecision,statusCheckRollup,mergedAt,mergeCommit"
                    ),
                ]
            ).stdout,
            "pull request",
        )
        if not isinstance(value, dict):
            raise GhCliPolicyError("pull request response is not an object")
        return value

    def view_issue(self, repository: str, number: int) -> dict[str, Any]:
        repository = self._batch(repository)
        if number < 1:
            raise GhCliPolicyError("issue number is invalid")
        value = self._json(
            self._run(
                [
                    "issue",
                    "view",
                    str(number),
                    "--repo",
                    repository,
                    "--json",
                    "number,url,state,title,comments,closedAt",
                ]
            ).stdout,
            "issue",
        )
        if not isinstance(value, dict):
            raise GhCliPolicyError("issue response is not an object")
        comments = value.get("comments")
        if not isinstance(comments, list) or len(comments) > 500:
            raise GhCliPolicyError("issue comments response is unbounded")
        return value

    def view_issue_body(self, repository: str, number: int) -> dict[str, Any]:
        """Read one issue's current state and body (read-only)."""
        repository = self._batch(repository)
        if number < 1:
            raise GhCliPolicyError("issue number is invalid")
        value = self._json(
            self._run(
                ["issue", "view", str(number), "--repo", repository, "--json", "number,state,body"]
            ).stdout,
            "issue",
        )
        if not isinstance(value, dict) or not isinstance(value.get("body"), str):
            raise GhCliPolicyError("issue body response is invalid")
        return value

    def list_workflow_runs(
        self,
        repository: str,
        *,
        workflow_name: str,
        commit_sha: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        repository = self._batch(repository)
        workflow_name = self._value(workflow_name, "workflow name", maximum=160)
        if not re.fullmatch(r"[a-f0-9]{40}", commit_sha) or not 1 <= limit <= 20:
            raise GhCliPolicyError("workflow run lookup binding is invalid")
        value = self._json(
            self._run(
                [
                    "run",
                    "list",
                    "--repo",
                    repository,
                    "--workflow",
                    workflow_name,
                    "--commit",
                    commit_sha,
                    "--limit",
                    str(limit),
                    "--json",
                    "databaseId,headSha,conclusion,status,event,workflowName,url,createdAt",
                ]
            ).stdout,
            "workflow runs",
        )
        return self._list(value, "workflow runs", maximum=limit)

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
        self,
        *,
        repository: str,
        title: str,
        body_file: Path,
        change_binding: str,
        labels: tuple[str, ...] = (),
    ) -> str:
        repository = self._write(repository, change_binding)
        if len(labels) > 10:
            raise GhCliPolicyError("issue labels exceed their bound")
        label_arguments = [
            argument
            for label in labels
            for argument in ("--label", self._value(label, "issue label", maximum=100))
        ]
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
                *label_arguments,
            ]
        ).stdout.strip()

    def list_labels(self, repository: str) -> list[str]:
        """NEW-108: names of the labels that already exist (read-only; never creates)."""
        repository = self._batch(repository)
        value = self._json(
            self._run(
                ["label", "list", "--repo", repository, "--json", "name", "--limit", "500"]
            ).stdout,
            "repository labels",
        )
        return [
            str(item["name"])
            for item in self._list(value, "repository labels", maximum=500)
            if isinstance(item.get("name"), str)
        ]

    def list_issues_page(
        self, repository: str, page: int, *, per_page: int = 50
    ) -> list[dict[str, Any]]:
        """NEW-108: one page of every issue, newest first, from the REST list endpoint.

        Read-only and independent of the search index, so a caller can page back
        conclusively to a point in time.
        """
        repository = self._batch(repository)
        if not 1 <= page <= 100 or not 1 <= per_page <= 100:
            raise GhCliPolicyError("issue page is out of bounds")
        value = self._json(
            self._run(
                [
                    "api",
                    "--method",
                    "GET",
                    f"repos/{repository}/issues?state=all&sort=created&direction=desc"
                    f"&per_page={per_page}&page={page}",
                ]
            ).stdout,
            "issue page",
        )
        return self._list(value, "issue page", maximum=per_page)

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

    def merge_pull_request(
        self,
        *,
        repository: str,
        number: int,
        expected_head_sha: str,
        expected_base_branch: str,
        change_binding: str,
        allow_unstable: bool = False,
    ) -> dict[str, Any]:
        """Merge the bound head. ``allow_unstable`` (NEW-63) is passed only after the CI
        baseline gate attributed every failing non-required check to unchanged main."""
        repository = self._write(repository, change_binding)
        accepted_states = {"CLEAN", "UNSTABLE"} if allow_unstable else {"CLEAN"}
        expected_base_branch = self._value(
            expected_base_branch, "protected merge base branch", maximum=120
        )
        if number < 1 or not re.fullmatch(r"[a-f0-9]{40}", expected_head_sha):
            raise GhCliPolicyError("protected merge binding is invalid")
        current = self.view_pull_request(repository, number)
        if (
            current.get("headRefOid") != expected_head_sha
            or current.get("baseRefName") != expected_base_branch
        ):
            raise GhCliPolicyError("protected merge head or base changed")
        if current.get("state") != "MERGED":
            if (
                current.get("state") != "OPEN"
                or current.get("isDraft") is not False
                or current.get("mergeStateStatus") not in accepted_states
            ):
                raise GhCliPolicyError("protected merge preconditions are not satisfied")
            self._run(
                [
                    "pr",
                    "merge",
                    str(number),
                    "--repo",
                    repository,
                    "--merge",
                    "--match-head-commit",
                    expected_head_sha,
                ]
            )
            current = self.view_pull_request(repository, number)
        merge_commit = current.get("mergeCommit")
        merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        if (
            current.get("state") != "MERGED"
            or current.get("headRefOid") != expected_head_sha
            or current.get("baseRefName") != expected_base_branch
            or not isinstance(merge_sha, str)
            or not re.fullmatch(r"[a-f0-9]{40}", merge_sha)
        ):
            raise GhCliPolicyError("protected merge result is not immutable")
        commit = self.branch_commit(repository, merge_sha)
        return {
            "repository": repository,
            "pull_request_number": number,
            "pull_request_url": current.get("url"),
            "head_sha": expected_head_sha,
            "merge_sha": merge_sha,
            "merge_tree_sha": commit["tree"],
            "state": "MERGED",
        }

    def comment_issue(
        self,
        *,
        repository: str,
        number: int,
        body_file: Path,
        marker: str,
        change_binding: str,
    ) -> dict[str, Any]:
        repository = self._write(repository, change_binding)
        if number < 1 or not re.fullmatch(
            r"<!-- engineering-control-final-closure:[a-f0-9-]{36} -->", marker
        ):
            raise GhCliPolicyError("issue comment binding is invalid")
        bounded_body = self._bounded_file(body_file)
        if bounded_body.stat().st_size > 12_000:
            raise GhCliPolicyError("issue comment body exceeded the bounded size")
        try:
            body = bounded_body.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise GhCliPolicyError("issue comment body is unreadable") from error
        if body.count(marker) != 1:
            raise GhCliPolicyError("issue comment body is missing its exact binding marker")
        issue = self.view_issue(repository, number)
        matches = [
            comment
            for comment in issue["comments"]
            if isinstance(comment, dict) and marker in str(comment.get("body") or "")
        ]
        if len(matches) > 1:
            raise GhCliPolicyError("multiple final-closure comments share one binding")
        if matches:
            return {"status": "COMMENT_PRESENT", "comment_url": matches[0].get("url")}
        if issue.get("state") != "OPEN":
            raise GhCliPolicyError("issue closed before final evidence publication")
        url = self._run(
            [
                "issue",
                "comment",
                str(number),
                "--repo",
                repository,
                "--body-file",
                str(bounded_body),
            ]
        ).stdout.strip()
        if not re.fullmatch(r"https://github\.com/[^\s]+#issuecomment-[1-9][0-9]*", url):
            raise GhCliPolicyError("issue comment returned an invalid URL")
        return {"status": "COMMENTED", "comment_url": url}

    def close_issue(
        self,
        *,
        repository: str,
        number: int,
        marker: str,
        change_binding: str,
    ) -> dict[str, Any]:
        repository = self._write(repository, change_binding)
        if number < 1 or not re.fullmatch(
            r"<!-- engineering-control-final-closure:[a-f0-9-]{36} -->", marker
        ):
            raise GhCliPolicyError("issue closure binding is invalid")
        issue = self.view_issue(repository, number)
        if not any(
            isinstance(comment, dict) and marker in str(comment.get("body") or "")
            for comment in issue["comments"]
        ):
            raise GhCliPolicyError("issue closure requires its persisted evidence comment")
        if issue.get("state") == "OPEN":
            self._run(
                [
                    "issue",
                    "close",
                    str(number),
                    "--repo",
                    repository,
                    "--reason",
                    "completed",
                ]
            )
            issue = self.view_issue(repository, number)
        if issue.get("state") != "CLOSED" or not issue.get("closedAt"):
            raise GhCliPolicyError("issue closure was not confirmed")
        return {
            "status": "CLOSED",
            "issue_url": issue.get("url"),
            "closed_at": issue.get("closedAt"),
        }

    def mark_pull_request_ready(
        self,
        *,
        repository: str,
        number: int,
        expected_head_sha: str,
        change_binding: str,
    ) -> dict[str, Any]:
        """Take a draft PR out of draft only while it still points at the bound head."""
        repository = self._write(repository, change_binding)
        if number < 1 or not re.fullmatch(r"[a-f0-9]{40}", expected_head_sha):
            raise GhCliPolicyError("ready-for-review binding is invalid")
        current = self.view_pull_request(repository, number)
        if current.get("headRefOid") != expected_head_sha:
            raise GhCliPolicyError("pull request head changed before ready-for-review")
        if current.get("state") == "MERGED":
            return current
        if current.get("state") != "OPEN":
            raise GhCliPolicyError("pull request is not open")
        if current.get("isDraft") is not False:
            self._run(["pr", "ready", str(number), "--repo", repository])
            current = self.view_pull_request(repository, number)
        if current.get("headRefOid") != expected_head_sha:
            raise GhCliPolicyError("pull request head changed during ready-for-review")
        return current

    def list_tag_refs(self, repository: str, prefix: str) -> list[dict[str, str]]:
        """Return bounded tag refs under refs/tags/<prefix> as {ref, sha, type}."""
        repository = self._batch(repository)
        if not re.fullmatch(r"[A-Za-z]{0,8}", prefix):
            raise GhCliPolicyError("tag prefix is invalid")
        raw = self._run(
            [
                "api",
                f"repos/{repository}/git/matching-refs/tags/{prefix}",
                "--paginate",
                "--jq",
                ".[] | {ref: .ref, sha: .object.sha, type: .object.type}",
            ]
        ).stdout
        lines = [line for line in raw.splitlines() if line.strip()]
        if len(lines) > 5000:
            raise GhCliPolicyError("tag listing is unbounded")
        refs: list[dict[str, str]] = []
        for line in lines:
            value = self._json(line, "tag ref")
            if not isinstance(value, dict):
                raise GhCliPolicyError("tag ref is not an object")
            refs.append({key: str(value.get(key) or "") for key in ("ref", "sha", "type")})
        return refs

    def create_tag_ref(
        self, *, repository: str, tag: str, sha: str, change_binding: str
    ) -> dict[str, str]:
        """Create one lightweight tag ref at an exact commit (never moves a tag)."""
        repository = self._write(repository, change_binding)
        if not re.fullmatch(r"[A-Za-z]{0,8}[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}", tag):
            raise GhCliPolicyError("release tag is not a bounded semantic version")
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise GhCliPolicyError("release tag target is not an exact commit")
        value = self._json(
            self._run(
                [
                    "api",
                    "-X",
                    "POST",
                    f"repos/{repository}/git/refs",
                    "-f",
                    f"ref=refs/tags/{tag}",
                    "-f",
                    f"sha={sha}",
                    "--jq",
                    "{ref: .ref, sha: .object.sha}",
                ]
            ).stdout,
            "created tag ref",
        )
        if (
            not isinstance(value, dict)
            or value.get("ref") != f"refs/tags/{tag}"
            or value.get("sha") != sha
        ):
            raise GhCliPolicyError("created tag ref does not match its binding")
        return {"ref": f"refs/tags/{tag}", "sha": sha}

    # -- NEW-63: CI baseline pull requests (only ever on hermes/ci-baseline/* branches) --

    # GitHub lists at most 3000 files per pull request; a listing that reaches the cap may
    # be truncated, so it is refused rather than evaluated.
    PULL_REQUEST_FILE_CAP = 3000

    def pull_request_files(self, repository: str, number: int) -> list[str]:
        """Return every path changed by one pull request, including the source path of a
        rename (a file moved out of .github/ must still count as touching .github/)."""
        repository = self._batch(repository)
        if number < 1:
            raise GhCliPolicyError("pull request number is invalid")
        raw = self._run(
            [
                "api",
                f"repos/{repository}/pulls/{number}/files",
                "--paginate",
                "--jq",
                '.[] | [.filename, (.previous_filename // "")] | @tsv',
            ]
        ).stdout
        entries = [line for line in raw.splitlines() if line.strip()]
        if len(entries) >= self.PULL_REQUEST_FILE_CAP:
            raise GhCliPolicyError("pull request file listing reached the GitHub cap")
        paths: list[str] = []
        for entry in entries:
            paths.extend(part for part in entry.split("\t") if part)
        return paths

    def job_log_excerpt(self, repository: str, job_id: int, *, timeout_seconds: int = 15) -> str:
        """NEW-100: the failing lines of one Actions job log, bounded (untrusted text).

        Only the allowlist is checked: the caller's gate already preflighted this repository
        in the same step, and a second preflight would not be bounded by the read timeout.
        """
        repository = self._repository(repository)
        if job_id < 1:
            raise GhCliPolicyError("workflow job ID is invalid")
        raw = self._run(
            ["api", f"repos/{repository}/actions/jobs/{job_id}/logs"],
            max_output_bytes=JOB_LOG_TAIL_BYTES,
            timeout_seconds=timeout_seconds,
        ).stdout
        return failure_excerpt(raw)

    def create_baseline_branch(
        self, *, repository: str, branch: str, sha: str, change_binding: str
    ) -> None:
        """Create one CI baseline branch at an exact commit (never moves an existing ref)."""
        repository = self._write(repository, change_binding)
        branch = self._baseline_branch(branch)
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise GhCliPolicyError("baseline branch target is not an exact commit")
        value = self._json(
            self._run(
                [
                    "api",
                    "-X",
                    "POST",
                    f"repos/{repository}/git/refs",
                    "-f",
                    f"ref=refs/heads/{branch}",
                    "-f",
                    f"sha={sha}",
                    "--jq",
                    "{ref: .ref, sha: .object.sha}",
                ]
            ).stdout,
            "created baseline branch",
        )
        if (
            not isinstance(value, dict)
            or value.get("ref") != f"refs/heads/{branch}"
            or value.get("sha") != sha
        ):
            raise GhCliPolicyError("created baseline branch does not match its binding")

    def put_baseline_marker(
        self, *, repository: str, branch: str, path: str, change_binding: str
    ) -> None:
        """Commit one fixed marker file to a CI baseline branch (contents API)."""
        repository = self._write(repository, change_binding)
        branch = self._baseline_branch(branch)
        if (
            not re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", path)
            or ".." in path.split("/")
            or path.startswith((".github/", "/"))
        ):
            raise GhCliPolicyError("baseline marker path is invalid")
        content = base64.b64encode(BASELINE_MARKER_CONTENT.encode()).decode()
        self._run(
            [
                "api",
                "-X",
                "PUT",
                f"repos/{repository}/contents/{path}",
                "-f",
                "message=ci: Hermes CI baseline marker (never merged)",
                "-f",
                f"content={content}",
                "-f",
                f"branch={branch}",
                "--jq",
                ".commit.sha",
            ]
        )

    def close_baseline_pull_request(
        self, *, repository: str, number: int, change_binding: str
    ) -> None:
        """Close a CI baseline PR and delete its branch; refuses any other PR."""
        repository = self._write(repository, change_binding)
        current = self.view_pull_request(repository, number)
        branch = self._baseline_branch(str(current.get("headRefName") or ""))
        if current.get("state") == "OPEN":
            self._run(["pr", "close", str(number), "--repo", repository])
        self.delete_baseline_branch(
            repository=repository, branch=branch, change_binding=change_binding
        )

    def delete_baseline_branch(self, *, repository: str, branch: str, change_binding: str) -> None:
        """Delete one CI baseline branch; refuses every other branch."""
        repository = self._write(repository, change_binding)
        branch = self._baseline_branch(branch)
        self._run(["api", "-X", "DELETE", f"repos/{repository}/git/refs/heads/{branch}"])

    def commit_date(self, repository: str, ref: str) -> str:
        """NEW-67: committer date (ISO 8601) of one branch or commit."""
        repository = self._batch(repository)
        ref = self._value(ref, "commit ref", maximum=120)
        value = self._run(
            ["api", f"repos/{repository}/commits/{ref}", "--jq", ".commit.committer.date"]
        ).stdout.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
            raise GhCliPolicyError("commit date is invalid")
        return value

    def commit_contains(self, repository: str, *, ancestor: str, descendant: str) -> bool:
        """NEW-68: True when ``descendant`` is ``ancestor`` or contains it in its history."""
        repository = self._batch(repository)
        if not re.fullmatch(r"[a-f0-9]{40}", ancestor) or not re.fullmatch(
            r"[a-f0-9]{40}", descendant
        ):
            raise GhCliPolicyError("commit comparison binding is invalid")
        status = self._run(
            ["api", f"repos/{repository}/compare/{ancestor}...{descendant}", "--jq", ".status"]
        ).stdout.strip()
        return status in {"ahead", "identical"}

    def last_reopened_at(self, repository: str, number: int) -> str:
        """NEW-67: when the pull request was last reopened ('' if never)."""
        repository = self._batch(repository)
        if number < 1:
            raise GhCliPolicyError("pull request number is invalid")
        raw = self._run(
            [
                "api",
                f"repos/{repository}/issues/{number}/events",
                "--paginate",
                "--jq",
                '.[] | select(.event == "reopened") | .created_at',
            ]
        ).stdout
        values = [line.strip() for line in raw.splitlines() if line.strip()]
        return max(values) if values else ""

    def rerun_pull_request_ci(
        self, *, repository: str, number: int, expected_head_sha: str, change_binding: str
    ) -> None:
        """NEW-67: close and reopen the bound PR so pull_request CI runs on current base.

        The head commit is unchanged (the ChangeSet binding stays valid); only the
        merge ref GitHub builds for the PR moves to the current base branch.
        """
        repository = self._write(repository, change_binding)
        if number < 1 or not re.fullmatch(r"[a-f0-9]{40}", expected_head_sha):
            raise GhCliPolicyError("CI re-run binding is invalid")
        current = self.view_pull_request(repository, number)
        if current.get("headRefOid") != expected_head_sha or current.get("state") != "OPEN":
            raise GhCliPolicyError("pull request changed before the CI re-run")
        self._run(["pr", "close", str(number), "--repo", repository])
        self._run(["pr", "reopen", str(number), "--repo", repository])

    @staticmethod
    def _baseline_branch(branch: str) -> str:
        if not re.fullmatch(r"hermes/ci-baseline/[a-f0-9]{12}-[a-f0-9]{8}", branch):
            raise GhCliPolicyError("only Hermes CI baseline branches may be written")
        return branch

    def repository_file_text(self, repository: str, path: str, ref: str) -> str:
        """Read one bounded repository file at a branch; never returned to callers verbatim."""
        repository = self._batch(repository)
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", path) or ".." in path.split("/"):
            raise GhCliPolicyError("repository file path is invalid")
        ref = self._value(ref, "repository file ref", maximum=120)
        raw = self._run(
            [
                "api",
                "-X",
                "GET",
                f"repos/{repository}/contents/{path}",
                "-f",
                f"ref={ref}",
                "-H",
                "Accept: application/vnd.github.raw",
            ]
        ).stdout
        if len(raw.encode()) > self.maximum_json_bytes:
            raise GhCliPolicyError("repository file exceeded its bounded size")
        return raw

    def comment_marked_issue(
        self,
        *,
        repository: str,
        number: int,
        body_file: Path,
        marker: str,
        change_binding: str,
        require_open: bool = True,
    ) -> dict[str, Any]:
        """Comment once per staging-GitOps marker; an existing marker comment is reused."""
        repository = self._write(repository, change_binding)
        if number < 1 or not STAGING_GITOPS_MARKER.fullmatch(marker):
            raise GhCliPolicyError("staging issue comment binding is invalid")
        bounded_body = self._bounded_file(body_file)
        if bounded_body.stat().st_size > 12_000:
            raise GhCliPolicyError("issue comment body exceeded the bounded size")
        try:
            body = bounded_body.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise GhCliPolicyError("issue comment body is unreadable") from error
        if body.count(marker) != 1:
            raise GhCliPolicyError("issue comment body is missing its exact binding marker")
        issue = self.view_issue(repository, number)
        matches = [
            comment
            for comment in issue["comments"]
            if isinstance(comment, dict) and marker in str(comment.get("body") or "")
        ]
        if matches:
            return {"status": "COMMENT_PRESENT", "comment_url": matches[0].get("url")}
        if require_open and issue.get("state") != "OPEN":
            raise GhCliPolicyError("issue closed before staging evidence publication")
        url = self._run(
            [
                "issue",
                "comment",
                str(number),
                "--repo",
                repository,
                "--body-file",
                str(bounded_body),
            ]
        ).stdout.strip()
        if not re.fullmatch(r"https://github\.com/[^\s]+#issuecomment-[1-9][0-9]*", url):
            raise GhCliPolicyError("issue comment returned an invalid URL")
        return {"status": "COMMENTED", "comment_url": url}

    def close_marked_issue(
        self,
        *,
        repository: str,
        number: int,
        marker: str,
        change_binding: str,
    ) -> dict[str, Any]:
        """Close an issue only after its staging-GitOps evidence comment exists."""
        repository = self._write(repository, change_binding)
        if number < 1 or not STAGING_GITOPS_MARKER.fullmatch(marker):
            raise GhCliPolicyError("staging issue closure binding is invalid")
        issue = self.view_issue(repository, number)
        if not any(
            isinstance(comment, dict) and marker in str(comment.get("body") or "")
            for comment in issue["comments"]
        ):
            raise GhCliPolicyError("issue closure requires its staging evidence comment")
        if issue.get("state") == "OPEN":
            self._run(
                ["issue", "close", str(number), "--repo", repository, "--reason", "completed"]
            )
            issue = self.view_issue(repository, number)
        if issue.get("state") != "CLOSED":
            raise GhCliPolicyError("issue closure was not confirmed")
        return {
            "status": "CLOSED",
            "issue_url": issue.get("url"),
            "closed_at": issue.get("closedAt"),
        }

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
            r"(?:changeset|campaign|finding|enhancement|change-request):[A-Za-z0-9-]{8,200}",
            change_binding,
        ):
            raise GhCliPolicyError("GitHub write is not bound to an approved control-plane record")
        return repository

    def _run(
        self,
        arguments: list[str],
        *,
        check_auth: bool = False,
        max_output_bytes: int | None = None,
        timeout_seconds: int = 120,
    ) -> CommandResult:
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
            if max_output_bytes is not None:
                return self.runner.run(
                    "gh",
                    arguments,
                    cwd=self.working_directory,
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                    max_output_bytes=max_output_bytes,
                )
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
    def _list(value: Any, label: str, *, maximum: int = 100) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > maximum:
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
        if value.startswith("-") or len(value) > maximum or not _SAFE_VALUE.fullmatch(value):
            raise GhCliPolicyError(f"{label} contains forbidden shell or control characters")
        if any(marker in value for marker in ("$(`", "$(", "${", ";", "&&", "||", "\n", "\r")):
            raise GhCliPolicyError(f"{label} contains shell metacharacters")
        return value

    def _bounded_file(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(self.working_directory.resolve()):
            raise GhCliPolicyError("GitHub body file escaped the broker working directory")
        return resolved


# NEW-100: only the tail of a job log is read (failures and the final error are last).
JOB_LOG_TAIL_BYTES = 2_000_000
_LOG_PREFIX = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[0-9:.]+Z ")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_FAILURE_LINE = re.compile(
    r"^not ok\b|##\[error\]|\b(?:error|failed|failure|assert(?:ion)?|expected|actual|"
    r"exception|traceback|panic)\b|^\s*(?:throw|at )|^\s*--- FAIL\b|^FAIL\b|"
    r"\bgot\b.*\bwant\b|"
    # NEW-104: npm audit's per-package "Severity: high|critical" lines.
    r"^\s*Severity:\s*(?:high|critical)\s*$",
    re.IGNORECASE,
)
# NEW-104: a pnpm audit advisory table row for a high or critical finding.
_AUDIT_FINDING_ROW = re.compile(r"^\s*│\s*(?:high|critical)\s*│", re.IGNORECASE)
_AUDIT_PACKAGE_ROW = re.compile(r"^\s*│\s*Package\s*│")
_AUDIT_SEVERITY_ROW = re.compile(r"^\s*│\s*(?:info|low|moderate|high|critical)\s*│", re.IGNORECASE)
_AUDIT_TABLE_ROWS = 24
_TABLE_RULING = re.compile(r"\s*[─━┌┐└┘├┤┬┴┼╭╮╰╯│]+[─━┌┐└┘├┤┬┴┼╭╮╰╯│ ]*")
_PASSING_LINE = re.compile(r"^\s*(?:ok \d+\b|# Subtest:)")


def failure_excerpt(
    raw: str, *, context: int = 8, max_lines: int = 60, max_chars: int = 4000
) -> str:
    """NEW-100: keep the lines around each failure marker of a CI log, bounded.

    Timestamps and ANSI colours are stripped; passing test lines never count as markers.
    NEW-104: a high/critical dependency-audit row keeps exactly its own advisory table
    (down to that table's closing ruling, without the inner rulings), so the package,
    patched versions and paths fit and no neighbouring advisory bleeds in.
    """
    lines = [
        _ANSI.sub("", _LOG_PREFIX.sub("", line))[:300]
        for line in raw[-JOB_LOG_TAIL_BYTES:].splitlines()
    ]
    audit_tables = _audit_table_rows(lines)
    keep: list[int] = []
    for index, line in enumerate(lines):
        if index in audit_tables:
            # Only a high/critical advisory is a finding; its table's rows are kept whole.
            if _AUDIT_FINDING_ROW.match(line):
                row = index
                while row in audit_tables and (
                    row == index or not _AUDIT_SEVERITY_ROW.match(lines[row])
                ):
                    keep.append(row)
                    row += 1
        elif _FAILURE_LINE.search(line) and not _PASSING_LINE.match(line):
            keep.extend(range(max(0, index - 1), min(len(lines), index + context + 1)))
    selected: list[str] = []
    last = -2
    for index in sorted(set(keep)):
        if index in audit_tables and _TABLE_RULING.fullmatch(lines[index]):
            last = index  # an inner audit-table ruling is skipped, not a gap
            continue
        if index != last + 1 and selected:
            selected.append("...")
        selected.append(lines[index])
        last = index
        if len(selected) >= max_lines:
            break
    return "\n".join(selected)[:max_chars]


def _audit_table_rows(lines: list[str]) -> set[int]:
    """NEW-104: indices of pnpm audit advisory table rows (any severity).

    A table starts at a severity row and ends before its closing ruling, at the first
    line that is not a table row or ruling (a truncated table), or after a bound. It is
    an audit table only if it has a "│ Package │" row; anything else is left alone.
    """
    rows: set[int] = set()
    index = 0
    while index < len(lines):
        if not _AUDIT_SEVERITY_ROW.match(lines[index]):
            index += 1
            continue
        end = min(len(lines), index + _AUDIT_TABLE_ROWS)
        row = index
        while row < end:
            stripped = lines[row].lstrip()
            if stripped.startswith("└") or not (
                stripped.startswith("│") or _TABLE_RULING.fullmatch(lines[row])
            ):
                break
            if row > index and _AUDIT_SEVERITY_ROW.match(lines[row]):
                break
            row += 1
        if any(_AUDIT_PACKAGE_ROW.match(lines[candidate]) for candidate in range(index, row)):
            rows.update(range(index, row))
        index = max(row, index + 1)
    return rows


GitHubDeliveryAdapter = GhCliBroker
