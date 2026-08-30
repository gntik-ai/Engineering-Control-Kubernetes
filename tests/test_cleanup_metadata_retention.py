from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select

from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.db.models import (
    AgentInvocation,
    ChangeSet,
    Deployment,
    OperationExport,
    OperationRequest,
    Run,
)


@pytest.mark.asyncio
async def test_expired_operation_exports_are_bounded_cleanup_metadata(tmp_path: Path) -> None:
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'operation-exports.sqlite'}",
            cleanup_execution_enabled=True,
        )
    )
    Base.metadata.create_all(handler.engine)
    now = datetime.now(UTC)
    operation_ids = [uuid4(), uuid4()]
    with handler.sessions.begin() as session:
        for index, operation_id in enumerate(operation_ids):
            session.add(
                OperationRequest(
                    id=operation_id,
                    action="evidence_export_workflow",
                    project="falcone",
                    target_type="workflow",
                    target_id=f"workflow-{index}",
                    payload_json={},
                    source_channel="dashboard",
                    operator_class="password-reauthenticated-operator",
                    idempotency_key=f"{index + 1:064x}",
                    binding_hash=f"{index + 3:064x}",
                    workflow_id=None,
                    temporal_run_id=None,
                    status="COMPLETED",
                    safe_error=None,
                    created_at=now,
                    confirmed_at=now,
                    started_at=now,
                    completed_at=now,
                )
            )
            session.add(
                OperationExport(
                    operation_id=operation_id,
                    export_kind="workflow",
                    target_id=f"workflow-{index}",
                    content_json={"safe": True},
                    content_sha256=f"{index + 5:064x}",
                    size_bytes=13,
                    created_at=now,
                    expires_at=now + timedelta(days=-1 if index == 0 else 1),
                )
            )
    discovered = await handler.handle(
        "cleanup.discover",
        {
            "dry_run": False,
            "retention_policy": "standard",
            "orchestration": {"workflow_id": "operation-export-cleanup-test"},
        },
    )
    assert discovered["operation_export_cleanup_count"] == 1
    authorized = await handler.handle(
        "cleanup.authorize",
        {
            **discovered,
            "eligible_candidates": [],
            "inspected_file_candidates": [],
            "inspected_preview_namespace_candidates": [],
        },
    )
    executed = await handler.handle(
        "cleanup.execute_metadata",
        {
            **authorized,
            "removed_file_candidate_ids": [],
            "absent_file_candidate_ids": [],
            "removed_preview_namespace_candidate_ids": [],
            "absent_preview_namespace_candidate_ids": [],
        },
    )
    assert executed["deleted_operation_export_count"] == 1
    with handler.sessions() as session:
        remaining = list(session.scalars(select(OperationExport)).all())
    assert len(remaining) == 1
    assert remaining[0].operation_id == operation_ids[1]


@pytest.mark.asyncio
async def test_preview_retention_requires_terminal_changeset_and_tombstones_idempotently(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "targets"
    target_root.mkdir()
    kubeconfig = tmp_path / "release" / "preview.kubeconfig"
    target = {
        "version": 1,
        "enabled": True,
        "project": "falcone",
        "environment": "preview",
        "strategy": "helm",
        "target_context": "falcone-preview",
        "target_namespace": "falcone-preview-deadbeef",
        "blocker": None,
        "configuration": {
            "values_files": ["values-preview.yaml"],
            "release_name": "falcone-preview-deadbeef",
            "kube_context": "falcone-preview-releaser",
            "kubeconfig_path": str(kubeconfig),
            "cleanup_enabled": True,
            "cleanup_namespace_prefix": "falcone-preview-",
            "cleanup_kube_context": "falcone-preview-releaser",
            "cleanup_kubeconfig_path": str(kubeconfig),
        },
    }
    (target_root / "falcone-preview.yaml").write_text(yaml.safe_dump(target), encoding="utf-8")
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'metadata.sqlite'}",
            deployment_target_root=target_root,
            cleanup_execution_enabled=True,
            cleanup_retention_days=1,
        )
    )
    Base.metadata.create_all(handler.engine)
    change_set_id = uuid4()
    deployment_id = uuid4()
    old = datetime.now(UTC) - timedelta(days=2)
    with handler.sessions.begin() as session:
        session.add(
            ChangeSet(
                id=change_set_id,
                finding_id=uuid4(),
                issue_id=None,
                project="falcone",
                risk_class="R1",
                state="ISSUE_CLOSED",
                version=1,
                source_repository="falcone",
                source_branch=f"agent/falcone/1/{change_set_id}",
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                source_pr_url=None,
                deployment_repository="falcone-charts",
                deployment_branch=f"agent/falcone-charts/1/{change_set_id}",
                deployment_base_sha="c" * 40,
                deployment_head_sha="d" * 40,
                deployment_pr_url=None,
                image_digests={},
                chart_version=None,
                recovery_json={},
            )
        )
        session.add(
            Deployment(
                id=deployment_id,
                change_set_id=change_set_id,
                project="falcone",
                environment="preview",
                strategy="helm",
                target_context="falcone-preview",
                target_namespace="falcone-preview-deadbeef",
                desired_sha="d" * 40,
                status="PREVIEW_VERIFIED",
                transaction_id="preview-transaction",
                started_at=old,
                finished_at=old,
                recovery_json={},
            )
        )

    workflow_id = "preview-retention-test"
    discovered = await handler.handle(
        "cleanup.discover",
        {
            "dry_run": False,
            "retention_policy": "standard",
            "orchestration": {"workflow_id": workflow_id},
        },
    )
    preview = discovered["preview_namespace_cleanup_candidates"][0]
    assert preview["deployment_id"] == str(deployment_id)
    authorized = await handler.handle(
        "cleanup.authorize",
        {
            **discovered,
            "eligible_candidates": [],
            "inspected_file_candidates": [],
            "inspected_preview_namespace_candidates": [{**preview, "inspection": "ABSENT"}],
        },
    )
    execution_payload = {
        **authorized,
        "removed_file_candidate_ids": [],
        "absent_file_candidate_ids": [],
        "removed_preview_namespace_candidate_ids": [],
        "absent_preview_namespace_candidate_ids": [preview["candidate_id"]],
    }
    executed = await handler.handle("cleanup.execute_metadata", execution_payload)
    retried = await handler.handle("cleanup.execute_metadata", execution_payload)
    assert retried["cleaned_preview_deployment_ids"] == [str(deployment_id)]
    finalized = await handler.handle(
        "cleanup.finalize",
        {
            **executed,
            "removed_lease_ids": [],
            "already_absent_lease_ids": [],
            "terminal_status": "COMPLETE",
        },
    )
    assert finalized["status"] == "COMPLETE"
    with handler.sessions() as session:
        deployment = session.get(Deployment, deployment_id)
        cleanup_run = session.scalar(select(Run).where(Run.workflow_id == workflow_id))
        assert deployment is not None and deployment.status == "PREVIEW_EXPIRED"
        assert deployment.recovery_json["cleanup_candidate_id"] == preview["candidate_id"]
        assert cleanup_run is not None and cleanup_run.status == "SUCCESS"


@pytest.mark.asyncio
async def test_provider_temp_retention_requires_terminal_non_rate_limited_codex_run(
    tmp_path: Path,
) -> None:
    handler = MetadataActivityHandler(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'provider-temp.sqlite'}",
            cleanup_retention_days=1,
        )
    )
    Base.metadata.create_all(handler.engine)
    old = datetime.now(UTC) - timedelta(days=2)
    eligible_run_id = uuid4()
    protected_run_id = uuid4()
    with handler.sessions.begin() as session:
        for run_id, status in (
            (eligible_run_id, "SUCCESS"),
            (protected_run_id, "RATE_LIMITED"),
        ):
            session.add(
                Run(
                    id=run_id,
                    campaign_id=None,
                    change_set_id=None,
                    workflow_id=f"provider-run-{run_id}",
                    run_type="implementation",
                    status=status,
                    project="falcone",
                    worker="codex-runner",
                    started_at=old,
                    finished_at=old,
                    error_code="PROVIDER_RATE_LIMIT" if status == "RATE_LIMITED" else None,
                    summary="bounded provider run",
                )
            )
            session.add(
                AgentInvocation(
                    run_id=run_id,
                    provider="codex",
                    role="implementer",
                    model_class="worker",
                    provider_session_id=None,
                    status=status,
                    summary="bounded provider invocation",
                    structured_result={},
                    error_code=("PROVIDER_RATE_LIMIT" if status == "RATE_LIMITED" else None),
                    started_at=old,
                    finished_at=old,
                )
            )

    discovered = await handler.handle(
        "cleanup.discover",
        {
            "dry_run": True,
            "retention_policy": "standard",
            "orchestration": {"workflow_id": "provider-temp-retention-test"},
        },
    )

    candidates = discovered["file_cleanup_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["kind"] == "provider-temp"
    assert candidates[0]["worker"] == "codex"
    assert candidates[0]["run_id"] == str(eligible_run_id)
