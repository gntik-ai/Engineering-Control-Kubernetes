from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.core import ControlActivities
from engineering_control.config import Settings


@pytest.mark.asyncio
async def test_production_activity_rejects_invalid_authoritative_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(Settings(synthetic_mode=False), "controller")

    with pytest.raises(ApplicationError, match="invalid metadata activity input"):
        await activities.execute_control_step(
            {"step": "campaign.finalize", "payload": {"campaign_id": "test"}}
        )


@pytest.mark.asyncio
async def test_synthetic_activity_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(Settings(synthetic_mode=True), "controller")

    result = await activities.execute_control_step(
        {"step": "campaign.finalize", "payload": {"campaign_id": "test"}}
    )

    assert result["status"] == "PASS"


def test_audit_blocker_persistence_is_controller_only() -> None:
    assert "campaign.persist_cell_blocker" in ControlActivities._worker_steps["controller"]
    for worker in ("scan", "codex", "claude", "browser", "delivery", "release"):
        assert "campaign.persist_cell_blocker" not in ControlActivities._worker_steps[worker]
