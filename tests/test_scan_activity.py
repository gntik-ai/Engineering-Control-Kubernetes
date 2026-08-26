from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.retention import cleanup_authorization_sha256
from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings
from engineering_control.worktrees.manager import WorktreeAllocation


class FakeWorktreeManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.snapshots: list[str] = []
        self.release_reads: list[tuple[str, Path]] = []

    def snapshot(self, repository: str, *, base_ref: str, expected_sha: str) -> dict[str, str]:
        self.snapshots.append(repository)
        return {
            "repository": repository,
            "mirror": str(self.root / f"{repository}.git"),
            "base_ref": base_ref,
            "sha": expected_sha,
        }

    def allocate(self, **values: Any) -> WorktreeAllocation:
        target = self.root / values["repository"] / str(values["change_set_id"])
        target.mkdir(parents=True, exist_ok=True)
        return WorktreeAllocation(
            repository=values["repository"],
            path=target,
            branch=f"agent/{values['project']}/{values['issue']}/{values['change_set_id']}",
            base_sha=values["expected_base_sha"],
            change_set_id=values["change_set_id"],
        )

    def allocate_audit_snapshot(self, **values: Any) -> WorktreeAllocation:
        target = self.root / values["repository"] / str(values["campaign_id"])
        target.mkdir(parents=True, exist_ok=True)
        return WorktreeAllocation(
            repository=values["repository"],
            path=target,
            branch="",
            base_sha=values["expected_base_sha"],
            change_set_id=values["campaign_id"],
        )

    def inspect_snapshot(self, path: Path, *, expected_sha: str) -> dict[str, str]:
        return {"path": str(path), "sha": expected_sha, "status": "clean"}

    def grant_release_read(self, repository: str, target: Path) -> None:
        self.release_reads.append((repository, target))

    def grant_delivery_read(self, repository: str, target: Path) -> None:
        return None


class FakeObserverRunner:
    def run(self, executable: str, arguments: Any, **kwargs: Any) -> CommandResult:
        assert executable == "kubectl"
        if "auth" in arguments:
            if "can-i" in arguments:
                index = arguments.index("can-i")
                verb, resource = arguments[index + 1 : index + 3]
                allowed = verb in {"get", "list", "watch"} and resource not in {
                    "secrets",
                    "pods/exec",
                    "pods/attach",
                    "pods/portforward",
                }
                return CommandResult(0, "yes\n" if allowed else "no\n", "")
            return CommandResult(0, "no\n", "")
        resource = arguments[arguments.index("get") + 1]
        revision = "d" * 40
        digest = f"sha256:{'e' * 64}"
        if resource == "deployments":
            value = {
                "items": [
                    {
                        "metadata": {
                            "name": "api",
                            "annotations": {"engineering.gntik.ai/deployment-sha": revision},
                        },
                        "spec": {
                            "replicas": 1,
                            "template": {
                                "spec": {
                                    "containers": [
                                        {"name": "api", "image": f"example.invalid/api@{digest}"}
                                    ]
                                }
                            },
                        },
                        "status": {"availableReplicas": 1},
                    }
                ]
            }
        else:
            value = {
                "items": [
                    {
                        "metadata": {
                            "name": "api-pod",
                            "annotations": {"engineering.gntik.ai/deployment-sha": revision},
                        },
                        "spec": {"containers": [{"name": "api", "image": "example.invalid/api"}]},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{"name": "api", "imageID": digest}],
                        },
                    }
                ]
            }
        return CommandResult(0, json.dumps(value), "")


class IndeterminateObserverRunner(FakeObserverRunner):
    def run(self, executable: str, arguments: Any, **kwargs: Any) -> CommandResult:
        if "auth" in arguments:
            return CommandResult(1, "", "authorization endpoint unavailable")
        return super().run(executable, arguments, **kwargs)


class FakeCleanupManager:
    def __init__(self, branch: str, *, dirty: bool = False) -> None:
        self.branch = branch
        self.dirty = dirty
        self.cleaned: list[str] = []

    def _run(self, arguments: list[str], path: Path) -> str:
        if arguments[:2] == ["git", "branch"]:
            return self.branch
        if arguments[:2] == ["git", "rev-parse"]:
            return "b" * 40
        raise AssertionError(arguments)

    def is_dirty(self, path: Path) -> bool:
        return self.dirty

    def cleanup(self, allocation: Any, *, evidence_captured: bool) -> None:
        assert evidence_captured is False
        self.cleaned.append(str(allocation.path))


@pytest.mark.asyncio
async def test_project_snapshot_maps_both_transaction_repositories(tmp_path: Path) -> None:
    manager = FakeWorktreeManager(tmp_path)
    handler = ScanActivityHandler(
        Settings(mirror_root=tmp_path, worktree_root=tmp_path / "worktrees"),
        manager,  # type: ignore[arg-type]
    )

    result = await handler.handle(
        "project.snapshot",
        {
            "project": "musematic",
            "campaign_id": str(uuid4()),
            "source_sha": "a" * 40,
            "deployment_sha": "b" * 40,
        },
    )

    assert result["status"] == "SNAPSHOT_VERIFIED"
    assert manager.snapshots == ["musematic", "musematic-deploy"]
    assert result["deployment_snapshot"]["sha"] == "b" * 40
    assert manager.release_reads == [
        ("musematic-deploy", tmp_path / "musematic-deploy" / str(result["campaign_id"]))
    ]


@pytest.mark.asyncio
async def test_worktree_allocation_requires_independent_providers(tmp_path: Path) -> None:
    manager = FakeWorktreeManager(tmp_path)
    handler = ScanActivityHandler(Settings(), manager)  # type: ignore[arg-type]
    change_set_id: UUID = uuid4()
    request = {
        "change_set_id": str(change_set_id),
        "project": "falcone",
        "issue": "123",
        "source_repository": "falcone",
        "source_base_sha": "a" * 40,
        "deployment_repository": "falcone-charts",
        "deployment_base_sha": "b" * 40,
        "maker_provider": "codex",
        "checker_provider": "codex",
    }

    with pytest.raises(ApplicationError, match="must differ"):
        await handler.handle("delivery.allocate_worktrees", {"allocation_request": request})


def test_release_impact_classifier_escalates_migrations_and_security_boundaries() -> None:
    impact = ScanActivityHandler._classify_release_impact(
        repository="falcone-charts",
        changed_paths=[
            "charts/in-falcone/templates/clusterrole.yaml",
            "migrations/0042.sql",
            "src/auth/policy.py",
        ],
        diff=(
            "+kind: ClusterRole\n+ALTER TABLE tenant_credentials ADD COLUMN encrypted_value text;\n"
        ),
    )

    assert impact["risk_floor"] == "R3"
    assert impact["migration"] is True
    assert impact["categories"] == [
        "auth_or_tenant_boundary",
        "cluster_scoped",
        "migration",
    ]
    assert len(impact["sha256"]) == 64


@pytest.mark.asyncio
async def test_runtime_observation_reduces_read_only_cluster_evidence(tmp_path: Path) -> None:
    credential_root = tmp_path / "observer"
    credential_root.mkdir()
    kubeconfig = credential_root / "falcone.kubeconfig"
    kubeconfig.write_text("scoped observer test config", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    run_root = tmp_path / "runs"
    (run_root / "scan").mkdir(parents=True)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    source_worktree = tmp_path / "worktrees" / "falcone" / "change-set"
    deployment_worktree = tmp_path / "worktrees" / "falcone-charts" / "change-set"
    source_worktree.mkdir(parents=True)
    deployment_worktree.mkdir(parents=True)
    manager = FakeWorktreeManager(tmp_path / "worktrees")
    handler = ScanActivityHandler(
        Settings(
            observer_credential_root=credential_root,
            run_root=run_root,
            evidence_root=evidence_root,
            environment="development",
        ),
        manager,  # type: ignore[arg-type]
        observer_runner=FakeObserverRunner(),  # type: ignore[arg-type]
    )
    change_set_id = uuid4()
    deployment_id = uuid4()
    digest = f"sha256:{'e' * 64}"

    result = await handler.handle(
        "runtime.deterministic_checks",
        {
            "project": "falcone",
            "change_set_id": str(change_set_id),
            "deployment_id": str(deployment_id),
            "runtime_observation_plan": {
                "project": "falcone",
                "change_set_id": str(change_set_id),
                "deployment_id": str(deployment_id),
                "kubeconfig_path": str(kubeconfig),
                "kube_context": "falcone-observer",
                "namespace": "falcone-staging",
                "resources": ["deployments", "pods"],
                "source_worktree": str(source_worktree),
                "deployment_worktree": str(deployment_worktree),
                "expected_source_sha": "b" * 40,
                "expected_deployment_sha": "d" * 40,
                "expected_image_digests": {"api": digest},
                "revision_annotation": "engineering.gntik.ai/deployment-sha",
                "argo_application": None,
            },
        },
    )

    assert result["status"] == "RUNTIME_OBSERVATION_PASS"
    observation = result["runtime_observation"]
    assert observation["revision_matches"] is True
    assert observation["digests_match"] is True
    assert observation["resources"]["pods"]["names"] == ["api-pod"]


@pytest.mark.asyncio
async def test_runtime_observation_rejects_indeterminate_authorization(tmp_path: Path) -> None:
    credential_root = tmp_path / "observer"
    credential_root.mkdir()
    kubeconfig = credential_root / "falcone.kubeconfig"
    kubeconfig.write_text("scoped observer test config", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    run_root = tmp_path / "runs"
    (run_root / "scan").mkdir(parents=True)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    source_worktree = tmp_path / "worktrees" / "falcone" / "change-set"
    deployment_worktree = tmp_path / "worktrees" / "falcone-charts" / "change-set"
    source_worktree.mkdir(parents=True)
    deployment_worktree.mkdir(parents=True)
    handler = ScanActivityHandler(
        Settings(
            observer_credential_root=credential_root,
            run_root=run_root,
            evidence_root=evidence_root,
            environment="development",
        ),
        FakeWorktreeManager(tmp_path / "worktrees"),  # type: ignore[arg-type]
        observer_runner=IndeterminateObserverRunner(),  # type: ignore[arg-type]
    )
    digest = f"sha256:{'e' * 64}"

    with pytest.raises(ApplicationError, match="least-privilege"):
        await handler.handle(
            "runtime.deterministic_checks",
            {
                "runtime_observation_plan": {
                    "project": "falcone",
                    "change_set_id": str(uuid4()),
                    "deployment_id": str(uuid4()),
                    "kubeconfig_path": str(kubeconfig),
                    "kube_context": "falcone-observer",
                    "namespace": "falcone-staging",
                    "resources": ["deployments", "pods"],
                    "source_worktree": str(source_worktree),
                    "deployment_worktree": str(deployment_worktree),
                    "expected_source_sha": "b" * 40,
                    "expected_deployment_sha": "d" * 40,
                    "expected_image_digests": {"api": digest},
                    "revision_annotation": "engineering.gntik.ai/deployment-sha",
                    "argo_application": None,
                }
            },
        )


@pytest.mark.asyncio
async def test_cleanup_protects_dirty_and_executes_only_controller_authorization(
    tmp_path: Path,
) -> None:
    worktree_root = tmp_path / "worktrees"
    change_set_id = uuid4()
    lease_id = uuid4()
    path = worktree_root / "falcone" / str(change_set_id)
    path.mkdir(parents=True)
    branch = f"agent/falcone/123/{change_set_id}"
    candidate = {
        "lease_id": str(lease_id),
        "change_set_id": str(change_set_id),
        "repository": "falcone",
        "worktree_path": str(path),
        "branch": branch,
        "base_sha": "a" * 40,
        "reason": "RELEASED_RETENTION_EXPIRED",
    }
    dirty_handler = ScanActivityHandler(
        Settings(worktree_root=worktree_root),
        FakeCleanupManager(branch, dirty=True),  # type: ignore[arg-type]
    )
    protected = await dirty_handler.handle(
        "cleanup.protect_active", {"cleanup_candidates": [candidate]}
    )
    assert protected["eligible_candidates"] == []
    assert protected["protected_candidates"][0]["protection"] == "DIRTY_WORKTREE"

    manager = FakeCleanupManager(branch)
    handler = ScanActivityHandler(
        Settings(worktree_root=worktree_root, cleanup_execution_enabled=True),
        manager,  # type: ignore[arg-type]
    )
    inspected = await handler.handle("cleanup.protect_active", {"cleanup_candidates": [candidate]})
    assert inspected["eligible_candidates"][0]["inspection"] == "CLEAN"
    workflow_id = "cleanup-test"
    authorization_sha = cleanup_authorization_sha256(
        workflow_id=workflow_id,
        worktrees=[candidate],
        files=[],
        component_leases=[],
        preview_namespaces=[],
    )
    disabled_handler = ScanActivityHandler(
        Settings(worktree_root=worktree_root),
        manager,  # type: ignore[arg-type]
    )
    with pytest.raises(ApplicationError, match="disabled by policy"):
        await disabled_handler.handle(
            "cleanup.execute",
            {
                "authorized_candidates": [candidate],
                "authorized_file_candidates": [],
                "authorized_component_lease_candidates": [],
                "authorized_preview_namespace_candidates": [],
                "cleanup_authorization_sha256": authorization_sha,
                "orchestration": {"workflow_id": workflow_id},
            },
        )
    result = await handler.handle(
        "cleanup.execute",
        {
            "authorized_candidates": [candidate],
            "authorized_file_candidates": [],
            "authorized_component_lease_candidates": [],
            "authorized_preview_namespace_candidates": [],
            "cleanup_authorization_sha256": authorization_sha,
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    assert result["removed_lease_ids"] == [str(lease_id)]
    assert manager.cleaned == [str(path)]
