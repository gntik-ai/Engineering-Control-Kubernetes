from __future__ import annotations

import os
import stat
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from engineering_control.adapters.github.client import ALLOWED_REPOSITORIES, READ_ONLY_REPOSITORIES

GITHUB_WRITE_POLICY_PATH = Path(
    "/etc/engineering-control/policy/github-issue-write-validation.yaml"
)
READ_ONLY_GITHUB_STEPS = frozenset(
    {
        "enhancement.github_dedupe",
        "finding.dedupe",
        "github.refresh_mirrors",
        "llmwiki.bootstrap.plan",
        "reconcile.inspect_ci",
    }
)
WRITE_CAPABLE_GITHUB_STEPS = frozenset(
    {
        "delivery.publish",
        "enhancement.publish",
        "finding.publish",
        "reconcile.publish_promotion",
    }
)
GITHUB_ACTIVITY_STEPS = READ_ONLY_GITHUB_STEPS | WRITE_CAPABLE_GITHUB_STEPS
WRITE_REPOSITORIES = ALLOWED_REPOSITORIES - READ_ONLY_REPOSITORIES


class GitHubWriteDenialReason(StrEnum):
    POLICY_DISABLED = "POLICY_DISABLED"
    EMPTY_SAFE_REPOSITORY_LIST = "EMPTY_SAFE_REPOSITORY_LIST"
    REPOSITORY_NOT_ALLOWLISTED = "REPOSITORY_NOT_ALLOWLISTED"
    POLICY_MISSING = "POLICY_MISSING"
    POLICY_UNREADABLE = "POLICY_UNREADABLE"
    POLICY_INVALID = "POLICY_INVALID"


class GitHubWritePolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    safe_repositories: tuple[str, ...]

    @field_validator("safe_repositories")
    @classmethod
    def bounded_allowlist(cls, repositories: tuple[str, ...]) -> tuple[str, ...]:
        if len(repositories) > len(WRITE_REPOSITORIES):
            raise ValueError("GitHub write repository allowlist exceeds its bound")
        if len(repositories) != len(set(repositories)):
            raise ValueError("GitHub write repository allowlist contains duplicates")
        if any(repository not in WRITE_REPOSITORIES for repository in repositories):
            raise ValueError("GitHub write repository allowlist contains an unsafe target")
        return repositories


class GitHubWritePolicyState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    repository_allowed: bool


class GitHubWritePolicyDenial(BaseModel):
    """Secret-free terminal result returned before any GitHub write-side effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["BLOCKED_BY_POLICY"] = "BLOCKED_BY_POLICY"
    blocker: Literal["BLOCKED_BY_POLICY"] = "BLOCKED_BY_POLICY"
    workflow_state: Literal["AWAITING_WRITE_POLICY"] = "AWAITING_WRITE_POLICY"
    terminal: Literal[True] = True
    retryable: Literal[False] = False
    external_command_started: Literal[False] = False
    external_mutation_attempted: Literal[False] = False
    mutating_command_constructed: Literal[False] = False
    external_command_count: Literal[0] = 0
    external_mutation_attempt_count: Literal[0] = 0
    mutating_command_construction_count: Literal[0] = 0
    policy: GitHubWritePolicyState
    reason_code: GitHubWriteDenialReason


class GitHubWritePolicyEvaluator:
    """Load one policy and authorize typed repositories before mutation construction."""

    def __init__(
        self,
        *,
        policy_path: Path,
        project_config_root: Path,
        production: bool,
        writes_enabled: bool,
    ) -> None:
        self.policy_path = policy_path
        self.config_root = project_config_root.resolve().parent
        self.production = production
        self.writes_enabled = writes_enabled

    def evaluate(
        self, repository_resolver: Callable[[], tuple[str, ...]]
    ) -> GitHubWritePolicyDenial | None:
        loaded = self._load()
        if isinstance(loaded, GitHubWritePolicyDenial):
            return loaded
        effective_enabled = self.writes_enabled and loaded.enabled
        if not effective_enabled:
            return self._denial(GitHubWriteDenialReason.POLICY_DISABLED, enabled=False)
        if not loaded.safe_repositories:
            return self._denial(
                GitHubWriteDenialReason.EMPTY_SAFE_REPOSITORY_LIST,
                enabled=True,
            )
        repositories = repository_resolver()
        if not repositories:
            return self._denial(GitHubWriteDenialReason.POLICY_INVALID, enabled=True)
        repository_allowed = all(
            repository in WRITE_REPOSITORIES and repository in loaded.safe_repositories
            for repository in repositories
        )
        if not repository_allowed:
            return self._denial(
                GitHubWriteDenialReason.REPOSITORY_NOT_ALLOWLISTED,
                enabled=True,
            )
        return None

    def _load(self) -> GitHubWritePolicyDocument | GitHubWritePolicyDenial:
        path = self.policy_path
        expected = self.config_root / "policy" / GITHUB_WRITE_POLICY_PATH.name
        if path != expected or path.is_symlink():
            return self._denial(GitHubWriteDenialReason.POLICY_INVALID)
        if not path.exists():
            return self._denial(GitHubWriteDenialReason.POLICY_MISSING)
        try:
            metadata = path.stat()
            parent_metadata = path.parent.stat()
        except OSError:
            return self._denial(GitHubWriteDenialReason.POLICY_UNREADABLE)
        permissions = stat.S_IMODE(metadata.st_mode)
        parent_permissions = stat.S_IMODE(parent_metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return self._denial(GitHubWriteDenialReason.POLICY_INVALID)
        if self.production:
            readable_groups = {*os.getgroups(), os.getegid()}
            if (
                metadata.st_uid != 0
                or metadata.st_gid not in readable_groups
                or permissions != 0o640
                or parent_metadata.st_uid != 0
                or parent_metadata.st_gid != metadata.st_gid
                or parent_permissions != 0o750
            ):
                return self._denial(GitHubWriteDenialReason.POLICY_UNREADABLE)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            return GitHubWritePolicyDocument.model_validate(raw)
        except PermissionError:
            return self._denial(GitHubWriteDenialReason.POLICY_UNREADABLE)
        except (OSError, UnicodeError):
            return self._denial(GitHubWriteDenialReason.POLICY_UNREADABLE)
        except (yaml.YAMLError, ValidationError):
            return self._denial(GitHubWriteDenialReason.POLICY_INVALID)

    @staticmethod
    def _denial(
        reason: GitHubWriteDenialReason,
        *,
        enabled: bool = False,
    ) -> GitHubWritePolicyDenial:
        return GitHubWritePolicyDenial(
            policy=GitHubWritePolicyState(enabled=enabled, repository_allowed=False),
            reason_code=reason,
        )
