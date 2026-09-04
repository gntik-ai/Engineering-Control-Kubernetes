from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

from engineering_control.activities.core import ControlActivities
from engineering_control.config import Settings


class FakeReleaseHandler:
    supported_steps = frozenset(
        {"deployment.render", "preview.preflight", "preview.verify", "staging.preflight"}
    )

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle(self, step: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(step)
        return {**payload, "status": "RELEASE_HANDLER_REACHED", "step": step}


class FakeCleanupScanHandler:
    async def handle(self, step: str, payload: dict[str, object]) -> dict[str, object]:
        return {**payload, "status": "SCAN_HANDLER_REACHED", "step": step}


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


@pytest.mark.asyncio
async def test_cleanup_aggregate_survives_control_activity_redaction_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(Settings(synthetic_mode=False), "scan")
    activities.scan_handler = FakeCleanupScanHandler()  # type: ignore[assignment]

    result = await activities.execute_control_step(
        {
            "step": "cleanup.protect_active",
            "payload": {
                "cleanup_candidates": [],
                "dashboard_session_revocation_cleanup_count": 28,
                "dashboard_session_revocation_cleanup_truncated": False,
            },
        }
    )

    assert result["status"] == "SCAN_HANDLER_REACHED"
    assert result["dashboard_session_revocation_cleanup_count"] == 28
    assert result["dashboard_session_revocation_cleanup_truncated"] is False


def test_audit_blocker_persistence_is_controller_only() -> None:
    for step in ("campaign.persist_cell_blocker", "campaign.persist_snapshot_blockers"):
        assert step in ControlActivities._worker_steps["controller"]
        for worker in ("scan", "codex", "claude", "browser", "delivery", "release"):
            assert step not in ControlActivities._worker_steps[worker]


def test_provider_telemetry_persistence_is_controller_only() -> None:
    for step in ("agent.record_start", "agent.persist_result"):
        assert step in ControlActivities._worker_steps["controller"]
        for worker in ("scan", "codex", "claude", "browser", "delivery", "release"):
            assert step not in ControlActivities._worker_steps[worker]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step",
    ("deployment.render", "preview.preflight", "preview.verify", "staging.preflight"),
)
async def test_non_mutating_release_steps_reach_release_handler(
    monkeypatch: pytest.MonkeyPatch,
    step: str,
) -> None:
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(Settings(synthetic_mode=False), "release")
    handler = FakeReleaseHandler()
    activities.release_handler = handler  # type: ignore[assignment]

    result = await activities.execute_control_step({"step": step, "payload": {"dry_run": True}})

    assert result["status"] == "RELEASE_HANDLER_REACHED"
    assert handler.calls == [step]
