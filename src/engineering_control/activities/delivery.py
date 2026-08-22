from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio.exceptions import ApplicationError

from engineering_control.adapters.command import CommandFailure, SafeCommandRunner
from engineering_control.config import Settings
from engineering_control.security.redaction import SecretRedactor


class RepositoryPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(
        pattern=r"^gntik-ai/(falcone|falcone-charts|musematic|musematic-deploy)$"
    )
    repository_slug: str = Field(pattern=r"^(falcone|falcone-charts|musematic|musematic-deploy)$")
    working_directory: Path
    branch: str = Field(pattern=r"^agent/(falcone|musematic)/[A-Za-z0-9._-]+/[a-f0-9-]{36}$")
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,120}$")
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=32_000)

    @model_validator(mode="after")
    def matching_repository_slug(self) -> RepositoryPublication:
        if self.repository.rsplit("/", 1)[-1] != self.repository_slug:
            raise ValueError("publication repository slug does not match its GitHub repository")
        return self


class DeferredDeployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_slug: str = Field(pattern=r"^(falcone-charts|musematic-deploy)$")
    branch: str = Field(pattern=r"^agent/(falcone|musematic)/[A-Za-z0-9._-]+/[a-f0-9-]{36}$")
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    reason: str = Field(pattern=r"^AWAITING_IMMUTABLE_ARTIFACT$")


class PublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    change_set_id: UUID
    publications: tuple[RepositoryPublication, ...] = Field(min_length=1, max_length=2)
    deferred_deployment: DeferredDeployment | None = None

    @model_validator(mode="after")
    def source_and_deployment_pair(self) -> PublicationPlan:
        slugs = {item.repository_slug for item in self.publications}
        if len(slugs) != len(self.publications):
            raise ValueError("publication repositories must be unique")
        full_pairs = (
            {"falcone", "falcone-charts"},
            {"musematic", "musematic-deploy"},
        )
        source_only = slugs in ({"falcone"}, {"musematic"})
        if slugs not in full_pairs and not source_only:
            raise ValueError("publication plan does not match a configured ChangeSet")
        if source_only:
            expected = "falcone-charts" if slugs == {"falcone"} else "musematic-deploy"
            if (
                self.deferred_deployment is None
                or self.deferred_deployment.repository_slug != expected
            ):
                raise ValueError("source-only publication must bind its deferred deployment")
        elif self.deferred_deployment is not None:
            raise ValueError("published deployment cannot also be deferred")
        if len({item.branch for item in self.publications}) != 1:
            raise ValueError("source and deployment publication must share one ChangeSet branch")
        branch = self.publications[0].branch
        if self.deferred_deployment is not None and self.deferred_deployment.branch != branch:
            raise ValueError("deferred deployment must share the ChangeSet branch")
        return self


class DeliveryActivityHandler:
    """Publish exact validated commits and draft PRs; never merge or rewrite history."""

    def __init__(self, settings: Settings, runner: SafeCommandRunner | None = None) -> None:
        self.settings = settings
        self.runner = runner or SafeCommandRunner(
            {"git": settings.git_executable, "gh": settings.github_executable}
        )
        self.redactor = SecretRedactor()

    async def handle(self, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        if step != "delivery.publish":
            raise ApplicationError("unsupported delivery activity", non_retryable=True)
        try:
            plan = PublicationPlan.model_validate(payload.get("publication_plan"))
            token = self._token()
            if token is None:
                return {
                    **payload,
                    "status": "BLOCKED",
                    "blocker": "MANUAL_GITHUB_DELIVERY_CREDENTIAL_REQUIRED",
                }
            askpass = self._askpass()
            results = [self._publish(item, token, askpass) for item in plan.publications]
        except (CommandFailure, OSError, ValueError) as error:
            return {
                **payload,
                "status": "BLOCKED",
                "blocker": "GITHUB_PUBLICATION_FAILED",
                "error_type": type(error).__name__,
            }
        safe = self.redactor.redact(
            {**payload, "status": "SOURCE_PRS_CREATED", "publication_results": results}
        ).value
        if not isinstance(safe, dict):
            raise TypeError("redacted delivery publication must remain an object")
        return safe

    def _publish(self, plan: RepositoryPublication, token: str, askpass: Path) -> dict[str, Any]:
        worktree = self._bounded_worktree(plan)
        git_arguments = ["-c", f"safe.directory={worktree}"]
        branch = self.runner.run(
            "git", [*git_arguments, "branch", "--show-current"], cwd=worktree
        ).stdout.strip()
        head = self.runner.run(
            "git", [*git_arguments, "rev-parse", "HEAD"], cwd=worktree
        ).stdout.strip()
        status = self.runner.run(
            "git", [*git_arguments, "status", "--porcelain=v1"], cwd=worktree
        ).stdout
        if branch != plan.branch or head != plan.head_sha or status:
            raise ValueError("publication worktree identity, head, or cleanliness changed")
        environment = {
            "ENGINEERING_GITHUB_TOKEN": token,
            "GH_TOKEN": token,
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(worktree),
        }
        self.runner.run(
            "git",
            [
                *git_arguments,
                "push",
                "--porcelain",
                f"https://github.com/{plan.repository}.git",
                f"HEAD:refs/heads/{plan.branch}",
            ],
            cwd=worktree,
            timeout_seconds=900,
            environment=environment,
        )
        existing = self._existing_pull_request(plan, environment)
        if existing is None:
            body_path = self._body_file(plan)
            try:
                created = self.runner.run(
                    "gh",
                    [
                        "pr",
                        "create",
                        "--repo",
                        plan.repository,
                        "--head",
                        plan.branch,
                        "--base",
                        plan.base_branch,
                        "--title",
                        plan.title,
                        "--body-file",
                        str(body_path),
                        "--draft",
                    ],
                    cwd=worktree,
                    environment=environment,
                ).stdout.strip()
                if not re.fullmatch(
                    rf"https://github\.com/{re.escape(plan.repository)}/pull/[1-9][0-9]*",
                    created,
                ):
                    raise ValueError("GitHub PR creation returned an invalid URL")
            finally:
                body_path.unlink(missing_ok=True)
            existing = self._existing_pull_request(plan, environment)
        if existing is None or existing.get("headRefOid") != plan.head_sha:
            raise ValueError("GitHub PR does not bind to the exact validated commit")
        return {
            "repository": plan.repository,
            "repository_slug": plan.repository_slug,
            "branch": plan.branch,
            "head_sha": plan.head_sha,
            "base_branch": plan.base_branch,
            "pr_number": existing["number"],
            "pr_url": existing["url"],
            "pr_state": existing["state"],
            "draft": existing["isDraft"],
        }

    def _existing_pull_request(
        self,
        plan: RepositoryPublication,
        environment: dict[str, str],
    ) -> dict[str, Any] | None:
        result = self.runner.run(
            "gh",
            [
                "pr",
                "list",
                "--repo",
                plan.repository,
                "--head",
                plan.branch,
                "--state",
                "all",
                "--json",
                "number,url,state,isDraft,headRefOid,baseRefName",
                "--limit",
                "10",
            ],
            cwd=plan.working_directory,
            environment=environment,
        )
        value = json.loads(result.stdout)
        if not isinstance(value, list):
            raise ValueError("GitHub PR lookup returned a non-list response")
        matches = [
            item
            for item in value
            if isinstance(item, dict)
            and item.get("baseRefName") == plan.base_branch
            and item.get("headRefOid") == plan.head_sha
        ]
        if len(matches) > 1:
            raise ValueError("multiple GitHub PRs bind to the same publication")
        return matches[0] if matches else None

    def _bounded_worktree(self, plan: RepositoryPublication) -> Path:
        root = self.settings.worktree_root.resolve()
        resolved = plan.working_directory.resolve()
        expected = root / plan.repository_slug / str(plan.branch.rsplit("/", 1)[-1])
        if not resolved.is_dir() or resolved != expected:
            raise ValueError("publication escaped its exact ChangeSet worktree")
        return resolved

    def _token(self) -> str | None:
        path = self.settings.github_token_file
        if path is None or not path.exists():
            return None
        root = self.settings.delivery_credential_root.resolve()
        resolved = path.resolve()
        if not resolved.is_file() or not resolved.is_relative_to(root):
            raise ValueError("GitHub delivery token escaped its credential root")
        mode = resolved.stat().st_mode
        permissions = stat.S_IMODE(mode)
        if not stat.S_ISREG(mode) or mode & stat.S_IRWXO:
            raise ValueError("GitHub delivery token permissions are too broad")
        if self.settings.environment == "production":
            metadata = resolved.stat()
            if metadata.st_uid != 0 or metadata.st_gid != os.getegid() or permissions != 0o640:
                raise ValueError("production GitHub delivery token ownership is invalid")
        elif permissions not in {0o600, 0o640}:
            raise ValueError("GitHub delivery token permissions are invalid")
        token = resolved.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("GitHub delivery token is invalid")
        return token

    def _askpass(self) -> Path:
        configured = self.settings.git_askpass_executable
        if not configured.is_absolute():
            raise ValueError("Git askpass executable must use an absolute path")
        resolved = configured.resolve()
        if not resolved.is_file():
            raise ValueError("Git askpass executable is missing")
        mode = resolved.stat().st_mode
        if not stat.S_ISREG(mode) or mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("Git askpass executable permissions are unsafe")
        if not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            raise ValueError("Git askpass helper is not executable")
        return resolved

    def _body_file(self, plan: RepositoryPublication) -> Path:
        root = (self.settings.run_root / "delivery").resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe = str(self.redactor.redact_text(plan.body).value)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f"{plan.repository_slug}-{plan.head_sha}-",
            suffix=".md",
            dir=root,
        )
        path = Path(raw_path)
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(safe)
        return path
