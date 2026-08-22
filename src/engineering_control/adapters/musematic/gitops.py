from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engineering_control.adapters.command import SafeCommandRunner


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
        "newest-build": re.compile(r"(?im)^\s*update-strategy:\s*newest-build\s*$"),
        "latest-tag": re.compile(r"(?im)(?:newTag|tag):\s*(?:latest|newest)\s*$"),
        "direct-main-writeback": re.compile(r"(?im)^\s*write-back-target:\s*git:branch:main\s*$"),
    }

    def __init__(self, runner: SafeCommandRunner) -> None:
        self.runner = runner

    def preflight(self, plan: MusematicPromotion) -> dict[str, Any]:
        if not plan.deployment_repository.is_dir():
            raise ValueError("deployment repository worktree does not exist")
        status = self.runner.run(
            "git", ["status", "--porcelain=v1"], cwd=plan.deployment_repository
        ).stdout
        if status:
            raise ValueError("Musematic deployment worktree must be clean at promotion preflight")
        revision = self.runner.run(
            "git", ["rev-parse", "HEAD"], cwd=plan.deployment_repository
        ).stdout.strip()
        if revision != plan.deployment_repository_sha or revision != plan.target_revision:
            raise ValueError("deployment Git SHA does not match the approved Argo target revision")
        violations: list[str] = []
        for path in plan.deployment_repository.rglob("*.yaml"):
            if ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            violations.extend(
                f"{name}:{path.relative_to(plan.deployment_repository)}"
                for name, pattern in self._unsafe_patterns.items()
                if pattern.search(text)
            )
        if violations:
            raise ValueError(
                "uncontrolled mutable image promotion is forbidden: " + ", ".join(violations)
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
