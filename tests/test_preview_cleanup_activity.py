from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.preview_cleanup import PreviewCleanupActivityHandler
from engineering_control.activities.retention import (
    PreviewNamespaceCleanupCandidate,
    cleanup_authorization_sha256,
)
from engineering_control.adapters.command import CommandResult
from engineering_control.config import Settings


class FakeKubectlRunner:
    def __init__(self, deployment_id: UUID, change_set_id: UUID, *, managed: bool = True) -> None:
        self.deployment_id = deployment_id
        self.change_set_id = change_set_id
        self.managed = managed
        self.uid = "namespace-uid"
        self.resource_version = "7"
        self.deleted: list[str] = []

    def run(
        self,
        executable: str,
        arguments: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        environment: Any = None,
        check: bool = True,
    ) -> CommandResult:
        assert executable == "kubectl"
        assert cwd == Path("/")
        if "get" in arguments:
            namespace = arguments[arguments.index("namespace") + 1]
            labels = {
                "engineering.gntik.ai/managed-by": (
                    "engineering-control" if self.managed else "someone-else"
                ),
                "engineering.gntik.ai/deployment-id": str(self.deployment_id),
                "engineering.gntik.ai/change-set-id": str(self.change_set_id),
            }
            return CommandResult(
                0,
                json.dumps(
                    {
                        "kind": "Namespace",
                        "metadata": {
                            "name": namespace,
                            "uid": self.uid,
                            "resourceVersion": self.resource_version,
                            "labels": labels,
                        },
                    }
                ),
                "",
            )
        namespace = arguments[arguments.index("namespace") + 1]
        assert "--wait=true" in arguments
        assert "--timeout=120s" in arguments
        assert timeout_seconds == 180
        self.deleted.append(namespace)
        return CommandResult(0, "namespace deleted", "")


def candidate(
    *, deployment_id: object, change_set_id: object, kubeconfig: Path
) -> dict[str, object]:
    namespace = f"falcone-preview-{str(deployment_id)[:8]}"
    value = PreviewNamespaceCleanupCandidate(
        candidate_id=hashlib.sha256(f"preview:{deployment_id}".encode()).hexdigest(),
        deployment_id=deployment_id,
        change_set_id=change_set_id,
        project="falcone",
        namespace=namespace,
        namespace_prefix="falcone-preview-",
        target_context="falcone-preview",
        kube_context=f"context-{deployment_id}",
        kubeconfig_path=kubeconfig,
        finished_at=datetime.now(UTC) - timedelta(days=30),
        reason="TERMINAL_CHANGESET_PREVIEW_RETENTION_EXPIRED",
    )
    return value.model_dump(mode="json")


def authorize(payload: dict[str, Any]) -> dict[str, Any]:
    previews = payload["inspected_preview_namespace_candidates"]
    workflow_id = "cleanup-preview-test"
    return {
        **payload,
        "orchestration": {"workflow_id": workflow_id},
        "authorized_candidates": [],
        "authorized_file_candidates": [],
        "authorized_component_lease_candidates": [],
        "authorized_preview_namespace_candidates": previews,
        "cleanup_authorization_sha256": cleanup_authorization_sha256(
            workflow_id=workflow_id,
            worktrees=[],
            files=[],
            component_leases=[],
            preview_namespaces=previews,
        ),
    }


def handler(tmp_path: Path, runner: FakeKubectlRunner) -> PreviewCleanupActivityHandler:
    credential_root = tmp_path / "release"
    credential_root.mkdir(exist_ok=True)
    return PreviewCleanupActivityHandler(
        Settings(
            release_credential_root=credential_root,
            cleanup_execution_enabled=True,
        ),
        runner=runner,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_preview_cleanup_requires_managed_labels_and_unchanged_identity(
    tmp_path: Path,
) -> None:
    deployment_id = uuid4()
    change_set_id = uuid4()
    runner = FakeKubectlRunner(deployment_id, change_set_id)
    activity = handler(tmp_path, runner)
    kubeconfig = activity.settings.release_credential_root / "preview.kubeconfig"
    kubeconfig.write_text("scoped", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    raw = candidate(
        deployment_id=deployment_id,
        change_set_id=change_set_id,
        kubeconfig=kubeconfig,
    )

    inspected = await activity.handle(
        "cleanup.inspect_preview_namespaces",
        {"preview_namespace_cleanup_candidates": [raw]},
    )
    assert inspected["inspected_preview_namespace_candidates"][0]["inspection"] == "CLEAN"
    result = await activity.handle("cleanup.execute_preview_namespaces", authorize(inspected))

    assert runner.deleted == [raw["namespace"]]
    assert result["removed_preview_namespace_candidate_ids"] == [raw["candidate_id"]]


@pytest.mark.asyncio
async def test_preview_cleanup_protects_unmanaged_namespace(tmp_path: Path) -> None:
    deployment_id = uuid4()
    change_set_id = uuid4()
    runner = FakeKubectlRunner(deployment_id, change_set_id, managed=False)
    activity = handler(tmp_path, runner)
    kubeconfig = activity.settings.release_credential_root / "preview.kubeconfig"
    kubeconfig.write_text("scoped", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    raw = candidate(
        deployment_id=deployment_id,
        change_set_id=change_set_id,
        kubeconfig=kubeconfig,
    )

    inspected = await activity.handle(
        "cleanup.inspect_preview_namespaces",
        {"preview_namespace_cleanup_candidates": [raw]},
    )

    assert inspected["inspected_preview_namespace_candidates"][0]["inspection"] == "PROTECTED"
    assert runner.deleted == []


@pytest.mark.asyncio
async def test_preview_cleanup_rejects_resource_drift(tmp_path: Path) -> None:
    deployment_id = uuid4()
    change_set_id = uuid4()
    runner = FakeKubectlRunner(deployment_id, change_set_id)
    activity = handler(tmp_path, runner)
    kubeconfig = activity.settings.release_credential_root / "preview.kubeconfig"
    kubeconfig.write_text("scoped", encoding="utf-8")
    os.chmod(kubeconfig, 0o600)
    raw = candidate(
        deployment_id=deployment_id,
        change_set_id=change_set_id,
        kubeconfig=kubeconfig,
    )
    inspected = await activity.handle(
        "cleanup.inspect_preview_namespaces",
        {"preview_namespace_cleanup_candidates": [raw]},
    )
    authorized = authorize(inspected)
    runner.resource_version = "8"

    with pytest.raises(ApplicationError, match="bounded policy validation"):
        await activity.handle("cleanup.execute_preview_namespaces", authorized)
    assert runner.deleted == []
