from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any, Literal, cast

from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.adapters.github.client import GhCliAuthError, GhCliBroker
from engineering_control.adapters.llmwiki import LLMWikiBootstrapPlanner
from engineering_control.config import Settings
from engineering_control.engineering.backlog import (
    BACKLOG_PROJECT_PAIRS,
    GitHubBacklogImportSnapshot,
    GitHubIssueSnapshot,
    GitHubPullRequestSnapshot,
    GitHubRepositoryBacklogSnapshot,
)


class GitHubActivityHandler:
    """Run only typed gh broker activities under the authenticated broker identity."""

    supported_steps = frozenset(
        {"llmwiki.bootstrap.plan", "github.refresh_mirrors", "backlog.github_import"}
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.broker = GhCliBroker(
            SafeCommandRunner({"gh": settings.github_executable}),
            settings.run_root,
            writes_enabled=settings.github_writes_enabled,
            authenticated_home=settings.github_authenticated_home,
            maximum_json_bytes=16_000_000,
        )
        self.git = SafeCommandRunner({"git": settings.git_executable})

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        # F-08: every branch here does blocking gh/git subprocess work through sync
        # broker/runner methods; run each off the event loop in a worker thread rather
        # than converting the whole synchronous call graph to async/await.
        if step == "backlog.github_import":
            return await asyncio.to_thread(self._import_backlog, payload)
        if step == "github.refresh_mirrors":
            return await asyncio.to_thread(self._refresh_mirrors, payload)
        if step != "llmwiki.bootstrap.plan":
            raise ApplicationError("unsupported GitHub broker activity", non_retryable=True)
        if payload.get("project") != "llmwiki" or payload.get("environment") != "staging":
            raise ApplicationError(
                "LLMWiki bootstrap accepts only staging",
                type="PolicyViolation",
                non_retryable=True,
            )
        return await asyncio.to_thread(self._plan_llmwiki_bootstrap, payload)

    def _refresh_mirrors(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = payload.get("project")
        mapping = {
            "falcone": ("falcone", "falcone-charts"),
            "musematic": ("musematic", "musematic-deploy"),
            "llmwiki": ("llmwiki",),
        }
        repositories = mapping.get(str(project))
        if repositories is None:
            raise ApplicationError("mirror refresh project is invalid", non_retryable=True)
        full_names = tuple(f"gntik-ai/{item}" for item in repositories)
        self.broker.preflight(full_names)
        refreshed = []
        for slug, repository in zip(repositories, full_names, strict=True):
            mirror = (self.settings.mirror_root / f"{slug}.git").resolve()
            if not mirror.is_dir() or not mirror.is_relative_to(
                self.settings.mirror_root.resolve()
            ):
                raise ApplicationError("canonical mirror is unavailable", type="NotConfigured")
            remote = self.git.run(
                "git", ["remote", "get-url", "origin"], cwd=mirror
            ).stdout.strip()
            if remote != f"https://github.com/{repository}.git":
                raise ApplicationError("canonical mirror origin changed", type="PolicyViolation")
            self.git.run(
                "git",
                [
                    "fetch",
                    "--prune",
                    "origin",
                    "+refs/heads/*:refs/remotes/origin/*",
                    "+refs/tags/*:refs/tags/*",
                ],
                cwd=mirror,
                environment={
                    "HOME": str(self.broker.authenticated_home),
                    "GIT_TERMINAL_PROMPT": "0",
                },
            )
            refreshed.append(repository)
        return {**payload, "status": "PASS", "refreshed_repositories": refreshed}

    def _plan_llmwiki_bootstrap(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            llmwiki = self.broker.branch_commit("gntik-ai/llmwiki", "master")
            falcone = self.broker.branch_commit("gntik-ai/falcone", "main")
            charts = self.broker.branch_commit("gntik-ai/falcone-charts", "main")
        except GhCliAuthError as error:
            raise ApplicationError(
                "GitHub authentication expired during bootstrap planning",
                type="BLOCKED_BY_GH_AUTH",
                non_retryable=True,
            ) from error
        plan = LLMWikiBootstrapPlanner.plan(
            source_sha=llmwiki["sha"],
            falcone_source_sha=falcone["sha"],
            falcone_deployment_sha=charts["sha"],
            falcone_url_resolved=False,
            public_origin_resolved=False,
            image_pull_ready=False,
        )
        return {
            **payload,
            "status": "BLOCKED" if plan.blockers else "AWAITING_APPROVAL",
            "bootstrap_plan": plan.model_dump(mode="json"),
            "approval_binding": plan.approval_binding(),
            "blockers": list(plan.blockers),
        }

    def _import_backlog(self, payload: dict[str, Any]) -> dict[str, Any]:
        project = str(payload.get("project", ""))
        pair = BACKLOG_PROJECT_PAIRS.get(project)
        maximum_items = int(payload.get("maximum_items", 500))
        if pair is None:
            raise ApplicationError(
                "GitHub backlog project is not allowlisted",
                type="PolicyViolation",
                non_retryable=True,
            )
        if not 1 <= maximum_items <= 500:
            raise ApplicationError(
                "GitHub backlog import bound is invalid",
                type="PolicyViolation",
                non_retryable=True,
            )
        try:
            repositories_for_project = tuple(dict.fromkeys(pair))
            self.broker.preflight(repositories_for_project)
            repositories: list[GitHubRepositoryBacklogSnapshot] = []
            remaining = maximum_items
            for repository in repositories_for_project:
                metadata = self.broker.repository_metadata(repository)
                branch = metadata.get("defaultBranchRef")
                default_branch = branch.get("name") if isinstance(branch, dict) else None
                expected_branch: Literal["main", "master"] = (
                    "master" if project == "llmwiki" else "main"
                )
                if default_branch != expected_branch:
                    raise ApplicationError(
                        "backlog repository default branch changed",
                        type="PolicyViolation",
                        non_retryable=True,
                    )
                head = self.broker.branch_commit(repository, expected_branch)
                raw_issues = self.broker.list_open_issues(repository)
                issues = tuple(
                    self._issue_snapshot(repository, value) for value in raw_issues[:remaining]
                )
                remaining -= len(issues)
                listed_pull_requests = self.broker.list_repository_pull_requests(repository)
                pull_requests = tuple(
                    self._pull_request_snapshot(
                        repository,
                        self.broker.view_backlog_pull_request(repository, int(value["number"])),
                    )
                    for value in listed_pull_requests
                )
                repositories.append(
                    GitHubRepositoryBacklogSnapshot(
                        repository=repository,
                        default_branch=expected_branch,
                        head_sha=head["sha"],
                        issues=issues,
                        pull_requests=pull_requests,
                    )
                )
        except GhCliAuthError as error:
            raise ApplicationError(
                "GitHub authentication expired during backlog import",
                type="BLOCKED_BY_GH_AUTH",
                non_retryable=True,
            ) from error
        snapshot = GitHubBacklogImportSnapshot(
            project=cast(Literal["falcone", "musematic", "llmwiki"], project),
            imported_at=datetime.now(UTC),
            repositories=tuple(repositories),
        )
        return {
            **payload,
            "status": "IMPORTED_READ_ONLY",
            "snapshot": snapshot.model_dump(mode="json"),
            "issue_count": sum(len(repository.issues) for repository in snapshot.repositories),
            "existing_pr_count": sum(
                pull_request.state == "OPEN"
                for repository in snapshot.repositories
                for pull_request in repository.pull_requests
            ),
        }

    @staticmethod
    def _issue_snapshot(repository: str, value: dict[str, Any]) -> GitHubIssueSnapshot:
        body = str(value.get("body") or "")
        labels = value.get("labels")
        assignees = value.get("assignees")
        comments = value.get("comments")
        milestone = value.get("milestone")
        return GitHubIssueSnapshot(
            repository=repository,
            number=int(value["number"]),
            url=str(value["url"]),
            title=str(value["title"])[:300],
            body_excerpt=body[:6000],
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            state="OPEN",
            labels=tuple(
                str(item.get("name", ""))[:120]
                for item in labels
                if isinstance(item, dict) and item.get("name")
            )
            if isinstance(labels, list)
            else (),
            milestone=(
                str(milestone.get("title"))[:200]
                if isinstance(milestone, dict) and milestone.get("title")
                else None
            ),
            assignees=tuple(
                str(item.get("login", ""))[:80]
                for item in assignees
                if isinstance(item, dict) and item.get("login")
            )
            if isinstance(assignees, list)
            else (),
            comments=tuple(
                str(item.get("body", ""))[:512] for item in comments[:10] if isinstance(item, dict)
            )
            if isinstance(comments, list)
            else (),
            updated_at=GitHubActivityHandler._timestamp(value["updatedAt"]),
        )

    @staticmethod
    def _pull_request_snapshot(repository: str, value: dict[str, Any]) -> GitHubPullRequestSnapshot:
        comments = value.get("comments")
        commits = value.get("commits")
        state = str(value.get("state", "CLOSED")).upper()
        if value.get("mergedAt"):
            state = "MERGED"
        if state not in {"OPEN", "CLOSED", "MERGED"}:
            state = "CLOSED"
        return GitHubPullRequestSnapshot(
            repository=repository,
            number=int(value["number"]),
            url=str(value["url"]),
            title=str(value["title"])[:300],
            body_excerpt=str(value.get("body") or "")[:6000],
            state=cast(Literal["OPEN", "CLOSED", "MERGED"], state),
            draft=bool(value.get("isDraft")),
            head_ref=str(value.get("headRefName") or "unknown")[:240],
            head_sha=str(value["headRefOid"]),
            base_ref=str(value.get("baseRefName") or "main")[:120],
            merge_state=str(value.get("mergeStateStatus") or "UNKNOWN")[:40],
            updated_at=GitHubActivityHandler._timestamp(value["updatedAt"]),
            merged_at=GitHubActivityHandler._optional_timestamp(value.get("mergedAt")),
            closed_at=GitHubActivityHandler._optional_timestamp(value.get("closedAt")),
            comments=tuple(
                str(item.get("body", ""))[:512] for item in comments[:10] if isinstance(item, dict)
            )
            if isinstance(comments, list)
            else (),
            commits=tuple(
                str(item.get("oid", ""))
                for item in commits[:250]
                if isinstance(item, dict) and item.get("oid")
            )
            if isinstance(commits, list)
            else (),
        )

    @staticmethod
    def _timestamp(value: object) -> datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    @staticmethod
    def _optional_timestamp(value: object) -> datetime | None:
        return GitHubActivityHandler._timestamp(value) if value else None
