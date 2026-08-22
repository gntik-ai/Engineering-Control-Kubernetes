from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engineering_control.adapters.command import CommandResult, SafeCommandRunner


class FalconeReleasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    change_set_id: str
    chart_repository_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    chart_version: str
    release_name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,52}$")
    chart_directory: Path
    values_files: tuple[Path, ...]
    image_digests: dict[str, str]
    kube_context: str
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    kubeconfig_path: Path
    migration: bool = False
    backup_evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    parity_evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    rollback_plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timeout_minutes: int = Field(default=30, ge=5, le=120)

    @field_validator("image_digests")
    @classmethod
    def require_digests(cls, values: dict[str, str]) -> dict[str, str]:
        if not values or any(
            not value.startswith("sha256:") or len(value) != 71 for value in values.values()
        ):
            raise ValueError("every release image must be pinned to a sha256 digest")
        return values

    @model_validator(mode="after")
    def require_migration_safety_evidence(self) -> FalconeReleasePlan:
        if self.migration and (not self.backup_evidence_sha256 or not self.parity_evidence_sha256):
            raise ValueError(
                "migrations require real backup and environment-parity evidence; "
                "waivers are not synthesized"
            )
        return self


class FalconeHelmAdapter:
    def __init__(self, runner: SafeCommandRunner) -> None:
        self.runner = runner

    def preflight(self, plan: FalconeReleasePlan) -> dict[str, Any]:
        self._validate_files(plan)
        revision = self.runner.run(
            "git", ["rev-parse", "HEAD"], cwd=plan.chart_directory
        ).stdout.strip()
        if revision != plan.chart_repository_sha:
            raise ValueError(
                f"chart repository SHA mismatch: expected {plan.chart_repository_sha}, "
                f"got {revision}"
            )
        lint_arguments = ["lint", str(plan.chart_directory)]
        for values in plan.values_files:
            lint_arguments.extend(("--values", str(values)))
        lint = self.runner.run("helm", lint_arguments, cwd=plan.chart_directory)
        template_arguments = [
            "template",
            plan.release_name,
            str(plan.chart_directory),
            "--namespace",
            plan.namespace,
        ]
        for values in plan.values_files:
            template_arguments.extend(("--values", str(values)))
        for component, digest in sorted(plan.image_digests.items()):
            template_arguments.extend(("--set-string", f"images.{component}.digest={digest}"))
        rendered = self.runner.run("helm", template_arguments, cwd=plan.chart_directory)
        documents = [item for item in yaml.safe_load_all(rendered.stdout) if item]
        return {
            "chart_sha": revision,
            "chart_version": plan.chart_version,
            "lint_sha256": hashlib.sha256(lint.stdout.encode()).hexdigest(),
            "rendered_sha256": hashlib.sha256(rendered.stdout.encode()).hexdigest(),
            "manifest_count": len(documents),
            "migration": plan.migration,
            "backup_evidence_sha256": plan.backup_evidence_sha256,
            "parity_evidence_sha256": plan.parity_evidence_sha256,
            "status": "PREFLIGHT_PASS",
        }

    def deploy(self, plan: FalconeReleasePlan, *, approval_is_current: bool) -> CommandResult:
        if not approval_is_current:
            raise ValueError("release approval is absent, invalid, or bound to changed artifacts")
        self.preflight(plan)
        arguments = [
            "upgrade",
            "--install",
            plan.release_name,
            str(plan.chart_directory),
            "--namespace",
            plan.namespace,
            "--kube-context",
            plan.kube_context,
            "--kubeconfig",
            str(plan.kubeconfig_path),
            "--atomic",
            "--wait",
            "--timeout",
            f"{plan.timeout_minutes}m",
        ]
        for values in plan.values_files:
            arguments.extend(("--values", str(values)))
        for component, digest in sorted(plan.image_digests.items()):
            arguments.extend(("--set-string", f"images.{component}.digest={digest}"))
        return self.runner.run(
            "helm",
            arguments,
            cwd=plan.chart_directory,
            timeout_seconds=(plan.timeout_minutes + 5) * 60,
        )

    @staticmethod
    def _validate_files(plan: FalconeReleasePlan) -> None:
        if not plan.chart_directory.is_dir():
            raise ValueError("chart directory does not exist")
        resolved_chart = plan.chart_directory.resolve()
        for values in plan.values_files:
            resolved = values.resolve()
            if not resolved.is_file() or not resolved.is_relative_to(resolved_chart):
                raise ValueError("values files must exist within the bounded chart worktree")
        if not plan.kubeconfig_path.is_file():
            raise ValueError("scoped release kubeconfig does not exist")
