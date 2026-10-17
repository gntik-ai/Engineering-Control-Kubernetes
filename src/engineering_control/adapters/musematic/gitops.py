from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engineering_control.adapters.command import SafeCommandRunner
from engineering_control.worktrees.hardened_git import (
    SUBMODULES,
    GitLocation,
    needs_index,
    private_index,
)


class MusematicPromotion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    change_set_id: str
    deployment_repository_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deployment_repository: Path
    deployment_pr_url: str
    image_digests: dict[str, str]
    argo_application: str
    target_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    target_namespace: str
    approval_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("image_digests")
    @classmethod
    def immutable_images(cls, values: dict[str, str]) -> dict[str, str]:
        if not values or any(not re.fullmatch(r"sha256:[a-f0-9]{64}", v) for v in values.values()):
            raise ValueError("Musematic promotion requires immutable image digests")
        return values


class MusematicGitOpsAdapter:
    _unsafe_patterns = {
        "newest-build": re.compile(
            r"(?im)^\s*[^#\n]*update-strategy\s*:\s*['\"]?newest-build['\"]?\s*$"
        ),
        "latest-tag": re.compile(
            r"(?im)^\s*[^#\n]*(?:newTag|tag)\s*:\s*['\"]?(?:latest|newest)['\"]?\s*$"
        ),
        "direct-main-writeback": re.compile(
            r"(?im)^\s*[^#\n]*write-back-target\s*:\s*['\"]?git:branch(?:=|:)main['\"]?\s*$"
        ),
    }
    _max_yaml_bytes = 4 * 1024 * 1024
    _max_repository_yaml_bytes = 64 * 1024 * 1024

    def __init__(
        self,
        runner: SafeCommandRunner,
        git_location: Callable[[Path], GitLocation] | None = None,
    ) -> None:
        self.runner = runner
        # NEW-221: the release activity passes this for ChangeSet worktrees (maker-
        # writable): Git then never discovers the repository, its configuration or hooks.
        self.git_location = git_location

    def _git(self, repository: Path, *arguments: str) -> str:
        if self.git_location is None:
            return self.runner.run("git", list(arguments), cwd=repository).stdout
        location = self.git_location(repository)
        if needs_index(arguments):
            # NEW-221: a private index from HEAD, never the maker's own.
            with private_index(location) as indexed:
                return self.runner.run(
                    "git",
                    indexed.arguments(arguments),
                    cwd=indexed.work_tree or repository,
                    environment=indexed.environment(),
                ).stdout
        return self.runner.run(
            "git",
            location.arguments(arguments),
            cwd=location.work_tree or repository,
            environment=location.environment(),
        ).stdout

    def preflight(self, plan: MusematicPromotion) -> dict[str, Any]:
        if not plan.deployment_repository.is_dir():
            raise ValueError("deployment repository worktree does not exist")
        status = self._git(plan.deployment_repository, "status", "--porcelain=v1", SUBMODULES)
        if status:
            raise ValueError("Musematic deployment worktree must be clean at promotion preflight")
        revision = self._git(plan.deployment_repository, "rev-parse", "HEAD").strip()
        if revision != plan.deployment_repository_sha or revision != plan.target_revision:
            raise ValueError("deployment Git SHA does not match the approved Argo target revision")
        repository_root = plan.deployment_repository.resolve()
        yaml_paths = sorted(
            {
                path
                for pattern in ("*.yaml", "*.yml")
                for path in plan.deployment_repository.rglob(pattern)
                if ".git" not in path.parts
            }
        )
        if not yaml_paths:
            raise ValueError("Musematic deployment repository contains no YAML desired state")
        violations: list[str] = []
        desired_state: list[str] = []
        total_bytes = 0
        for path in yaml_paths:
            metadata = path.lstat()
            resolved = path.resolve()
            if (
                path.is_symlink()
                or not path.is_file()
                or not resolved.is_relative_to(repository_root)
            ):
                raise ValueError("Musematic desired-state YAML escaped its deployment worktree")
            if metadata.st_size > self._max_yaml_bytes:
                raise ValueError("Musematic desired-state YAML exceeds its per-file size bound")
            total_bytes += metadata.st_size
            if total_bytes > self._max_repository_yaml_bytes:
                raise ValueError("Musematic desired-state YAML exceeds its repository size bound")
            text = path.read_text(encoding="utf-8", errors="replace")
            desired_state.append(text)
            violations.extend(
                f"{name}:{path.relative_to(plan.deployment_repository)}"
                for name, pattern in self._unsafe_patterns.items()
                if pattern.search(text)
            )
        if violations:
            raise ValueError(
                "uncontrolled mutable image promotion is forbidden: " + ", ".join(violations)
            )
        rendered_desired_state = "\n".join(desired_state)
        missing_digests = sorted(
            component
            for component, digest in plan.image_digests.items()
            if digest not in rendered_desired_state
        )
        if missing_digests:
            raise ValueError(
                "Musematic desired Git state is missing approved immutable image digests for: "
                + ", ".join(missing_digests)
            )
        return {
            "status": "PREFLIGHT_PASS",
            "deployment_sha": revision,
            "deployment_pr_url": plan.deployment_pr_url,
            "image_digests": plan.image_digests,
            "desired_state": "protected Git state",
        }

    def observe_reconciliation(self, plan: MusematicPromotion) -> dict[str, Any]:
        """Observe Argo; desired-state mutation happens only through the protected deployment PR."""
        self.preflight(plan)
        result = self.runner.run(
            "argocd",
            ["app", "get", plan.argo_application, "--output", "json", "--refresh"],
            cwd=plan.deployment_repository,
        )
        payload = json.loads(result.stdout)
        sync = payload.get("status", {}).get("sync", {})
        health = payload.get("status", {}).get("health", {})
        observed_revision = sync.get("revision")
        return {
            "sync_status": sync.get("status"),
            "health_status": health.get("status"),
            "observed_revision": observed_revision,
            "revision_matches": observed_revision == plan.target_revision,
        }
