from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.retention import (
    FileCleanupCandidate,
    RetentionActivityHandler,
    cleanup_authorization_sha256,
)
from engineering_control.config import Settings


def delivery_candidate(run_id: UUID, cutoff: datetime) -> dict[str, object]:
    identity = f"delivery:{run_id}"
    return FileCleanupCandidate(
        candidate_id=hashlib.sha256(identity.encode()).hexdigest(),
        kind="delivery-run",
        worker="delivery",
        run_id=run_id,
        reason="TERMINAL_RUN_RETENTION_EXPIRED",
        not_modified_after=cutoff,
    ).model_dump(mode="json")


def codex_temp_candidate(run_id: UUID, cutoff: datetime) -> dict[str, object]:
    identity = f"codex-temp:{run_id}"
    return FileCleanupCandidate(
        candidate_id=hashlib.sha256(identity.encode()).hexdigest(),
        kind="provider-temp",
        worker="codex",
        run_id=run_id,
        reason="PROVIDER_TEMP_RETENTION_EXPIRED",
        not_modified_after=cutoff,
    ).model_dump(mode="json")


def authorize(payload: dict[str, object]) -> dict[str, object]:
    workflow_id = "cleanup-retention-test"
    files = payload["inspected_file_candidates"]
    assert isinstance(files, list)
    return {
        **payload,
        "orchestration": {"workflow_id": workflow_id},
        "authorized_candidates": [],
        "authorized_file_candidates": files,
        "authorized_component_lease_candidates": [],
        "authorized_preview_namespace_candidates": [],
        "cleanup_authorization_sha256": cleanup_authorization_sha256(
            workflow_id=workflow_id,
            worktrees=[],
            files=files,
            component_leases=[],
            preview_namespaces=[],
        ),
    }


@pytest.mark.asyncio
async def test_retention_removes_only_unchanged_exact_owned_run_directory(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    run_id = uuid4()
    run_directory = run_root / "delivery" / str(run_id)
    run_directory.mkdir(parents=True)
    (run_directory / "result.json").write_text("safe evidence", encoding="utf-8")
    handler = RetentionActivityHandler(
        Settings(run_root=run_root, cleanup_execution_enabled=True), "delivery"
    )
    candidate = delivery_candidate(run_id, datetime.now(UTC) + timedelta(hours=1))

    inspected = await handler.handle(
        "cleanup.inspect_delivery_files", {"file_cleanup_candidates": [candidate]}
    )
    result = await handler.handle("cleanup.execute_delivery_files", authorize(inspected))

    assert not run_directory.exists()
    assert result["removed_file_candidate_ids"] == [candidate["candidate_id"]]


@pytest.mark.asyncio
async def test_retention_rejects_manifest_drift_after_authorization(tmp_path: Path) -> None:
    run_root = tmp_path / "runs"
    run_id = uuid4()
    run_directory = run_root / "delivery" / str(run_id)
    run_directory.mkdir(parents=True)
    output = run_directory / "result.json"
    output.write_text("before", encoding="utf-8")
    handler = RetentionActivityHandler(
        Settings(run_root=run_root, cleanup_execution_enabled=True), "delivery"
    )
    candidate = delivery_candidate(run_id, datetime.now(UTC) + timedelta(hours=1))
    inspected = await handler.handle(
        "cleanup.inspect_delivery_files", {"file_cleanup_candidates": [candidate]}
    )
    authorized = authorize(inspected)
    output.write_text("after authorization", encoding="utf-8")

    with pytest.raises(ApplicationError, match="bounded policy validation"):
        await handler.handle("cleanup.execute_delivery_files", authorized)
    assert run_directory.exists()


@pytest.mark.asyncio
async def test_retention_refuses_symlinks_inside_cleanup_tree(tmp_path: Path) -> None:
    run_root = tmp_path / "runs"
    run_id = uuid4()
    run_directory = run_root / "delivery" / str(run_id)
    run_directory.mkdir(parents=True)
    outside = tmp_path / "protected.txt"
    outside.write_text("do not follow", encoding="utf-8")
    (run_directory / "escape").symlink_to(outside)
    handler = RetentionActivityHandler(Settings(run_root=run_root), "delivery")
    candidate = delivery_candidate(run_id, datetime.now(UTC) + timedelta(hours=1))

    with pytest.raises(ApplicationError, match="bounded policy validation"):
        await handler.handle(
            "cleanup.inspect_delivery_files", {"file_cleanup_candidates": [candidate]}
        )
    assert outside.read_text(encoding="utf-8") == "do not follow"


@pytest.mark.asyncio
async def test_retention_removes_only_exact_codex_run_temp_directory(tmp_path: Path) -> None:
    temporary_root = tmp_path / "tmp"
    run_id = uuid4()
    run_directory = temporary_root / "codex" / str(run_id)
    sibling = temporary_root / "codex" / str(uuid4())
    run_directory.mkdir(parents=True)
    sibling.mkdir()
    (run_directory / "orphan.json").write_text("bounded", encoding="utf-8")
    handler = RetentionActivityHandler(
        Settings(temporary_root=temporary_root, cleanup_execution_enabled=True), "codex"
    )
    candidate = codex_temp_candidate(run_id, datetime.now(UTC) + timedelta(hours=1))

    inspected = await handler.handle(
        "cleanup.inspect_codex_files", {"file_cleanup_candidates": [candidate]}
    )
    result = await handler.handle("cleanup.execute_codex_files", authorize(inspected))

    assert not run_directory.exists()
    assert sibling.is_dir()
    assert result["removed_file_candidate_ids"] == [candidate["candidate_id"]]


@pytest.mark.asyncio
async def test_retention_refuses_symlinked_candidate_parent(tmp_path: Path) -> None:
    temporary_root = tmp_path / "tmp"
    provider_root = temporary_root / "codex"
    outside = tmp_path / "outside"
    provider_root.mkdir(parents=True)
    outside.mkdir()
    run_id = uuid4()
    (provider_root / str(run_id)).symlink_to(outside, target_is_directory=True)
    handler = RetentionActivityHandler(Settings(temporary_root=temporary_root), "codex")
    candidate = codex_temp_candidate(run_id, datetime.now(UTC) + timedelta(hours=1))

    with pytest.raises(ApplicationError, match="bounded policy validation"):
        await handler.handle(
            "cleanup.inspect_codex_files", {"file_cleanup_candidates": [candidate]}
        )
    assert outside.is_dir()
