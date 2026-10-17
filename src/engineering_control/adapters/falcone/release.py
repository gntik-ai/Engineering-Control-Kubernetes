from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engineering_control.adapters.command import CommandResult, SafeCommandRunner
from engineering_control.worktrees.hardened_git import GitLocation


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


class FalconeRecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: str
    change_set_id: str
    release_name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,52}$")
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    kube_context: str
    kubeconfig_path: Path
    previous_revision: int = Field(ge=1)
    previous_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_artifact_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    recovery_approval_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timeout_minutes: int = Field(default=30, ge=5, le=120)


class FalconeHelmAdapter:
    def __init__(
        self,
        runner: SafeCommandRunner,
        git_location: Callable[[Path], GitLocation] | None = None,
    ) -> None:
        self.runner = runner
        # NEW-221: the release activity passes this; the chart directory lies inside a
        # maker-writable ChangeSet worktree, so Git must not discover from it.
        self.git_location = git_location

    def preflight(self, plan: FalconeReleasePlan) -> dict[str, Any]:
        self._validate_files(plan)
        if self.git_location is None:
            revision = self.runner.run(
                "git", ["rev-parse", "HEAD"], cwd=plan.chart_directory
            ).stdout.strip()
        else:
            location = self.git_location(plan.chart_directory)
            revision = self.runner.run(
                "git",
                location.arguments(["rev-parse", "HEAD"]),
                cwd=location.work_tree or plan.chart_directory,
                environment=location.environment(),
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
        rendered = self.runner.run("helm", template_arguments, cwd=plan.chart_directory)
        missing_digests = sorted(
            component
            for component, digest in plan.image_digests.items()
            if digest not in rendered.stdout
        )
        if missing_digests:
            raise ValueError(
                "rendered Falcone manifests are missing approved immutable image digests for: "
                + ", ".join(missing_digests)
            )
        with tempfile.TemporaryDirectory(prefix="engineering-falcone-manifest-") as raw:
            manifest = Path(raw) / "rendered.yaml"
            manifest.write_text(rendered.stdout, encoding="utf-8")
            schema_validation = self.runner.run(
                "kubeconform",
                [
                    "-strict",
                    "-summary",
                    "-ignore-missing-schemas",
                    str(manifest),
                ],
                cwd=plan.chart_directory,
            )
        previous = self._current_release(plan)
        documents = [item for item in yaml.safe_load_all(rendered.stdout) if item]
        return {
            "chart_sha": revision,
            "chart_version": plan.chart_version,
            "lint_sha256": hashlib.sha256(lint.stdout.encode()).hexdigest(),
            "rendered_sha256": hashlib.sha256(rendered.stdout.encode()).hexdigest(),
            "schema_validation_sha256": hashlib.sha256(
                schema_validation.stdout.encode()
            ).hexdigest(),
            "manifest_count": len(documents),
            "migration": plan.migration,
            "backup_evidence_sha256": plan.backup_evidence_sha256,
            "parity_evidence_sha256": plan.parity_evidence_sha256,
            "previous_revision": previous["revision"],
            "previous_status": previous["status"],
            "previous_manifest_sha256": previous["manifest_sha256"],
            "status": "PREFLIGHT_PASS",
        }

    def deploy(
        self,
        plan: FalconeReleasePlan,
        *,
        approval_is_current: bool,
        approved_preflight: dict[str, Any],
    ) -> CommandResult:
        if not approval_is_current:
            raise ValueError("release approval is absent, invalid, or bound to changed artifacts")
        current_preflight = self.preflight(plan)
        if current_preflight != approved_preflight:
            raise ValueError("release preflight drifted after immutable approval")
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
        return self.runner.run(
            "helm",
            arguments,
            cwd=plan.chart_directory,
            timeout_seconds=(plan.timeout_minutes + 5) * 60,
        )

    def verify(self, plan: FalconeReleasePlan) -> dict[str, Any]:
        current = self._current_release(plan)
        if current["status"] != "deployed" or current["revision"] is None:
            raise ValueError("Helm preview release is not deployed")
        manifest = self.runner.run(
            "helm",
            [
                "get",
                "manifest",
                plan.release_name,
                "--namespace",
                plan.namespace,
                "--kube-context",
                plan.kube_context,
                "--kubeconfig",
                str(plan.kubeconfig_path),
                "--revision",
                str(current["revision"]),
            ],
            cwd=plan.chart_directory,
        ).stdout
        missing = sorted(digest for digest in plan.image_digests.values() if digest not in manifest)
        if missing:
            raise ValueError("Helm preview manifest is missing approved immutable image digests")
        return {
            "status": "PREVIEW_VERIFIED",
            "revision": current["revision"],
            "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
            "immutable_image_count": len(plan.image_digests),
        }

    def rollback(self, plan: FalconeRecoveryPlan, *, approval_is_current: bool) -> dict[str, Any]:
        if not approval_is_current:
            raise ValueError("recovery approval is absent, invalid, or bound to changed artifacts")
        if not plan.kubeconfig_path.is_file():
            raise ValueError("scoped release kubeconfig does not exist")
        arguments = [
            "rollback",
            plan.release_name,
            str(plan.previous_revision),
            "--namespace",
            plan.namespace,
            "--kube-context",
            plan.kube_context,
            "--kubeconfig",
            str(plan.kubeconfig_path),
            "--cleanup-on-fail",
            "--wait",
            "--timeout",
            f"{plan.timeout_minutes}m",
        ]
        command = self.runner.run(
            "helm",
            arguments,
            cwd=plan.kubeconfig_path.parent,
            timeout_seconds=(plan.timeout_minutes + 5) * 60,
        )
        current = self._current_release_from_scope(
            release_name=plan.release_name,
            namespace=plan.namespace,
            kube_context=plan.kube_context,
            kubeconfig_path=plan.kubeconfig_path,
            cwd=plan.kubeconfig_path.parent,
        )
        if (
            current["status"] != "deployed"
            or current["manifest_sha256"] != plan.previous_manifest_sha256
        ):
            raise ValueError("Helm rollback did not restore the exact captured manifest")
        return {
            "status": "ROLLBACK_APPLIED",
            "strategy": "helm",
            "target_revision": plan.previous_revision,
            "result_revision": current["revision"],
            "manifest_sha256": current["manifest_sha256"],
            "transaction_sha256": hashlib.sha256(command.stdout.encode()).hexdigest(),
        }

    def _current_release(self, plan: FalconeReleasePlan) -> dict[str, Any]:
        return self._current_release_from_scope(
            release_name=plan.release_name,
            namespace=plan.namespace,
            kube_context=plan.kube_context,
            kubeconfig_path=plan.kubeconfig_path,
            cwd=plan.chart_directory,
        )

    def _current_release_from_scope(
        self,
        *,
        release_name: str,
        namespace: str,
        kube_context: str,
        kubeconfig_path: Path,
        cwd: Path,
    ) -> dict[str, Any]:
        scope = [
            "--namespace",
            namespace,
            "--kube-context",
            kube_context,
            "--kubeconfig",
            str(kubeconfig_path),
        ]
        history = self.runner.run(
            "helm",
            ["history", release_name, *scope, "--max", "1", "--output", "json"],
            cwd=cwd,
            check=False,
        )
        if history.return_code != 0:
            if "release: not found" in history.stderr.lower():
                return {"revision": None, "status": None, "manifest_sha256": None}
            raise ValueError("Helm recovery baseline inspection failed")
        try:
            records = json.loads(history.stdout)
            record = (
                max(records, key=lambda item: int(item["revision"]))
                if isinstance(records, list)
                and records
                and all(isinstance(item, dict) for item in records)
                else None
            )
            revision = int(record["revision"]) if isinstance(record, dict) else None
            status = str(record["status"]).lower() if isinstance(record, dict) else None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Helm history returned invalid recovery metadata") from error
        if revision is None:
            return {"revision": None, "status": status, "manifest_sha256": None}
        manifest = self.runner.run(
            "helm",
            ["get", "manifest", release_name, *scope, "--revision", str(revision)],
            cwd=cwd,
        )
        return {
            "revision": revision,
            "status": status,
            "manifest_sha256": hashlib.sha256(manifest.stdout.encode()).hexdigest(),
        }

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
