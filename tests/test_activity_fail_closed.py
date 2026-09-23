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
    for step in ("campaign.persist_cell_blocker", "campaign.persist_snapshot_blockers"):
        assert step in ControlActivities._worker_steps["controller"]
        for worker in ("scan", "codex", "claude", "browser", "delivery", "release"):
            assert step not in ControlActivities._worker_steps[worker]


@pytest.mark.asyncio
async def test_delivery_release_leases_is_wired_to_a_production_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEW-20 regression, 2026-09-22: delivery.release_leases was added to
    ControlActivities._allowed_steps (and so to steps_for_worker('controller'))
    but NOT to MetadataActivityHandler.supported_steps -- so it passed both
    allowlist gates and then fell through every handler branch to "has no
    configured production activity handler", crashing the very first real
    delivery that ever hit an exception between acquire_leases and
    persist_publication -- exactly the scenario NEW-20 was meant to fix.
    Every existing test for this step (the workflow's own except-handler
    replay test, and MetadataActivityHandler.handle() unit tests) called the
    handler directly, bypassing this exact dispatch gate; this one goes
    through the real execute_control_step path instead.
    """
    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    activities = ControlActivities(Settings(synthetic_mode=False), "controller")

    result = await activities.execute_control_step(
        {"step": "delivery.release_leases", "payload": {}}
    )

    assert result["status"] == "NO_RUN_TO_RELEASE_LEASES_FOR"


@pytest.mark.asyncio
async def test_delivery_prepare_toolchain_is_wired_to_the_scan_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delivery.prepare_toolchain must pass both allowlist gates on the scan worker only,
    route to the scan queue, and reach ScanActivityHandler through execute_control_step."""
    from pathlib import Path

    from engineering_control.workflows.definitions import IssueDeliveryWorkflow
    from engineering_control.workflows.worker_registry import task_queue_for

    step = "delivery.prepare_toolchain"
    assert step in ControlActivities._worker_steps["scan"]
    for worker in ("controller", "codex", "claude", "browser", "delivery", "release"):
        assert step not in ControlActivities._worker_steps[worker]
    assert IssueDeliveryWorkflow._activity_queue(step, {}) == task_queue_for("scan")

    monkeypatch.setattr("engineering_control.activities.core.activity.heartbeat", lambda _: None)
    projects = Path(__file__).resolve().parents[1] / "config" / "projects"  # noqa: ASYNC240
    activities = ControlActivities(
        Settings(synthetic_mode=False, project_config_root=projects), "scan"
    )

    result = await activities.execute_control_step(
        {"step": step, "payload": {"project": "falcone", "source_worktree": "/nonexistent"}}
    )

    assert result["status"] == "TOOLCHAIN_SKIPPED"


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
