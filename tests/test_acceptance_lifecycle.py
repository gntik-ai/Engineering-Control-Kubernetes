from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from engineering_control.validation.lifecycle import AcceptanceActivities


@pytest.mark.asyncio
async def test_acceptance_lifecycle_uses_real_bounded_git_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "engineering_control.validation.lifecycle.activity.heartbeat", lambda _: None
    )
    activities = AcceptanceActivities(tmp_path)
    payload: dict[str, object] = {
        "change_set_id": str(uuid4()),
        "validation_root": str(tmp_path / "run"),
        "hold_seconds": 1,
    }
    payload = await activities.project_snapshot(payload)
    payload = activities.worktree(payload)
    payload = activities.maker(payload)
    payload = activities.checker(payload)
    payload = activities.evidence(payload)
    payload = activities.transition(payload)

    assert payload["status"] == "PASS"
    assert payload["change_set_state"] == "AWAITING_STAGING_APPROVAL"
    assert payload["maker_provider"] != payload["checker_provider"]
    assert Path(str(payload["evidence_path"])).is_file()  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_acceptance_snapshot_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "engineering_control.validation.lifecycle.activity.heartbeat", lambda _: None
    )
    activities = AcceptanceActivities(tmp_path)
    request = {
        "change_set_id": str(uuid4()),
        "validation_root": str(tmp_path / "run"),
        "hold_seconds": 1,
    }
    first = await activities.project_snapshot(request)
    second = await activities.project_snapshot(request)
    assert first["source_sha"] == second["source_sha"]
