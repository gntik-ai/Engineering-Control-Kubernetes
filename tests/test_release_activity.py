from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.release import ReleaseActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings


class FakeHelm:
    def preflight(self, plan: Any) -> dict[str, Any]:
        return {
            "status": "PREFLIGHT_PASS",
            "chart_sha": plan.chart_repository_sha,
            "rendered_sha256": "f" * 64,
        }

    def deploy(self, plan: Any, *, approval_is_current: bool) -> CommandResult:
        assert approval_is_current
        return CommandResult(0, "bounded helm transaction", "")

    def rollback(self, plan: Any, *, approval_is_current: bool) -> dict[str, Any]:
        assert approval_is_current
        return {
            "status": "ROLLBACK_APPLIED",
            "strategy": "helm",
            "target_revision": plan.previous_revision,
            "result_revision": plan.previous_revision + 1,
            "manifest_sha256": plan.previous_manifest_sha256,
            "transaction_sha256": "f" * 64,
        }


class FakeGitOps:
    def preflight(self, plan: Any) -> dict[str, Any]:
        return {
            "status": "PREFLIGHT_PASS",
            "deployment_sha": plan.target_revision,
        }

    def observe_reconciliation(self, plan: Any) -> dict[str, Any]:
        return {
            "sync_status": "Synced",
            "health_status": "Healthy",
            "observed_revision": plan.target_revision,
            "revision_matches": True,
        }


def helm_payload(worktree: Path, kubeconfig: Path) -> dict[str, Any]:
    binding = "b" * 64
    return {
        "dry_run": False,
        "release_plan": {
            "strategy": "helm",
            "change_set_id": "synthetic-change-set",
            "chart_repository_sha": "a" * 40,
            "chart_version": "1.2.3",
            "release_name": "falcone-preview",
            "chart_directory": str(worktree),
            "values_files": [],
            "image_digests": {"api": f"sha256:{'c' * 64}"},
            "kube_context": "scoped-preview",
            "namespace": "falcone-preview",
            "kubeconfig_path": str(kubeconfig),
            "migration": False,
            "rollback_plan_sha256": "d" * 64,
            "approval_binding_sha256": binding,
            "timeout_minutes": 5,
        },
        "approval_validation": {
            "current": True,
            "artifact_binding_sha256": binding,
        },
    }


@pytest.mark.asyncio
async def test_release_activity_requires_typed_current_bound_plan(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    chart = worktree_root / "falcone-charts" / "change-set"
    chart.mkdir(parents=True)
    credential_root = tmp_path / "credentials"
    credential_root.mkdir()
    kubeconfig = credential_root / "preview.kubeconfig"
    kubeconfig.write_text("scoped test configuration", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    settings = Settings(
        worktree_root=worktree_root,
        release_credential_root=credential_root,
    )
    handler = ReleaseActivityHandler(
        settings,
        helm=FakeHelm(),  # type: ignore[arg-type]
        gitops=FakeGitOps(),  # type: ignore[arg-type]
    )

    result = await handler.handle("staging.deploy", helm_payload(chart, kubeconfig))

    assert result["status"] == "DEPLOYED"
    assert result["strategy"] == "helm"
    assert len(result["transaction_id"]) == 64


@pytest.mark.asyncio
async def test_release_preflight_is_bounded_and_does_not_require_approval(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    chart = worktree_root / "falcone-charts" / "change-set"
    chart.mkdir(parents=True)
    credential_root = tmp_path / "credentials"
    credential_root.mkdir()
    kubeconfig = credential_root / "preview.kubeconfig"
    kubeconfig.write_text("scoped test configuration", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    handler = ReleaseActivityHandler(
        Settings(worktree_root=worktree_root, release_credential_root=credential_root),
        helm=FakeHelm(),  # type: ignore[arg-type]
        gitops=FakeGitOps(),  # type: ignore[arg-type]
    )
    payload = helm_payload(chart, kubeconfig)
    payload.pop("approval_validation")

    result = await handler.handle("staging.preflight", payload)

    assert result["status"] == "PREFLIGHT_PASS"
    assert result["release_preflight"]["chart_sha"] == "a" * 40


@pytest.mark.asyncio
async def test_release_activity_rejects_changed_approval(tmp_path: Path) -> None:
    worktree_root = tmp_path / "worktrees"
    chart = worktree_root / "falcone-charts" / "change-set"
    chart.mkdir(parents=True)
    credential_root = tmp_path / "credentials"
    credential_root.mkdir()
    kubeconfig = credential_root / "preview.kubeconfig"
    kubeconfig.write_text("scoped test configuration", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    handler = ReleaseActivityHandler(
        Settings(worktree_root=worktree_root, release_credential_root=credential_root),
        helm=FakeHelm(),  # type: ignore[arg-type]
        gitops=FakeGitOps(),  # type: ignore[arg-type]
    )
    payload = helm_payload(chart, kubeconfig)
    payload["approval_validation"]["artifact_binding_sha256"] = "e" * 64

    with pytest.raises(ApplicationError, match="no longer matches"):
        await handler.handle("staging.deploy", payload)


@pytest.mark.asyncio
async def test_release_recovery_remains_fail_closed(tmp_path: Path) -> None:
    handler = ReleaseActivityHandler(
        Settings(worktree_root=tmp_path),
        helm=FakeHelm(),  # type: ignore[arg-type]
        gitops=FakeGitOps(),  # type: ignore[arg-type]
    )
    with pytest.raises(ApplicationError, match="typed plan and immutable recovery approval"):
        await handler.handle("recovery.execute", {"dry_run": False})


@pytest.mark.asyncio
async def test_release_recovery_requires_exact_recovery_binding(tmp_path: Path) -> None:
    credential_root = tmp_path / "credentials"
    credential_root.mkdir()
    kubeconfig = credential_root / "staging.kubeconfig"
    kubeconfig.write_text("scoped test configuration", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    binding = "b" * 64
    handler = ReleaseActivityHandler(
        Settings(worktree_root=tmp_path, release_credential_root=credential_root),
        helm=FakeHelm(),  # type: ignore[arg-type]
        gitops=FakeGitOps(),  # type: ignore[arg-type]
    )

    result = await handler.handle(
        "recovery.execute",
        {
            "recovery_plan": {
                "strategy": "helm",
                "deployment_id": "deployment",
                "change_set_id": "change-set",
                "release_name": "falcone",
                "namespace": "falcone-staging",
                "kube_context": "falcone-staging-releaser",
                "kubeconfig_path": str(kubeconfig),
                "previous_revision": 3,
                "previous_manifest_sha256": "c" * 64,
                "original_artifact_binding_sha256": "d" * 64,
                "recovery_approval_binding_sha256": binding,
                "timeout_minutes": 5,
            },
            "approval_validation": {
                "current": True,
                "artifact_binding_sha256": binding,
            },
        },
    )

    assert result["status"] == "ROLLBACK_APPLIED"
    assert result["manifest_sha256"] == "c" * 64
