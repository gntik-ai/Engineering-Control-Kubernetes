from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.config import Settings
from engineering_control.worktrees.manager import WorktreeAllocation


class FakeWorktreeManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.snapshots: list[str] = []

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
