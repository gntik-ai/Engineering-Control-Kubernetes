from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.core import ControlActivities
from engineering_control.config import Settings


@pytest.mark.asyncio
async def test_production_activity_without_handler_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(Settings(synthetic_mode=False), "controller")

    with pytest.raises(ApplicationError, match="no configured production activity handler"):
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
