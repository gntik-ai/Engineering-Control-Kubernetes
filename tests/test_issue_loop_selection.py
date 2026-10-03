from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from test_metadata_activity import add_campaign

from engineering_control.activities.engineering import EngineeringActivityHandler
from engineering_control.activities.metadata import MetadataActivityHandler
from engineering_control.config import Settings
from engineering_control.db.base import Base
from engineering_control.workflows import definitions
from engineering_control.workflows.inputs import IssueSolutionLoopInput


async def run_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: dict[str, Any] | None = None,
    *,
    empty_campaign: bool = False,
    patched: bool = True,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]], dict[str, Any]]:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'selection.sqlite'}",
        state_root=tmp_path / "state",
        project_config_root=Path(__file__).parents[1] / "config/projects",
    )
    handler = EngineeringActivityHandler(settings)
    metadata = MetadataActivityHandler(settings)
    Base.metadata.create_all(handler.engine)
    campaign_id = str(add_campaign(metadata).id) if empty_campaign else str(uuid4())
    steps: list[str] = []
    children: list[dict[str, Any]] = []
    instance = definitions.IssueSolutionLoopWorkflow()

    async def step(name: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        steps.append(name)
        if name == "loop.load_selection" and selection is not None:
            return {**payload, **selection}
        return await handler.handle(name, payload)

    async def child(name: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert name == "IssueDeliveryWorkflow"
        children.append(payload)
        return {"status": "DRY_RUN"}

    monkeypatch.setattr(instance, "_step", step)
    monkeypatch.setattr(definitions.workflow, "patched", lambda _name: patched)
    monkeypatch.setattr(definitions.workflow, "execute_child_workflow", child)
    monkeypatch.setattr(
        definitions.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="selection-unit-fixture", task_queue="fixture-only"),
    )
    try:
        result = await instance.run(
            IssueSolutionLoopInput(project="falcone", campaign_id=campaign_id)
        )
        return result, steps, children, instance.status()
    finally:
        handler.engine.dispose()
        metadata.engine.dispose()


@pytest.mark.asyncio
async def test_missing_campaign_remains_blocked_without_finalization(tmp_path, monkeypatch):
    result, steps, children, status = await run_selection(tmp_path, monkeypatch)
    assert result == {"status": "BLOCKED", "blocker": "CAMPAIGN_NOT_FOUND", "results": []}
    assert steps == ["loop.load_selection"] and children == []
    assert status["phase"] == "SELECTION_BLOCKED"


@pytest.mark.asyncio
async def test_empty_selection_is_not_successful_work(tmp_path, monkeypatch):
    result, steps, children, status = await run_selection(
        tmp_path, monkeypatch, empty_campaign=True
    )
    assert result == {"status": "EMPTY", "reason": "NO_ELIGIBLE_FINDINGS", "results": []}
    assert steps == ["loop.load_selection"] and children == []
    assert status["phase"] == "EMPTY"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "expected", "blocker"),
    [
        ({"status": {}}, "FAILED", "LOOP_SELECTION_STATUS_INVALID"),
        (
            {"status": "SELECTED", "items": [{"finding_id": "inert", "risk_class": []}]},
            "FAILED",
            "LOOP_SELECTION_SCHEMA_INVALID",
        ),
        (
            {"status": "BLOCKED", "blocker": {"inert": "not-a-code"}},
            "BLOCKED",
            "LOOP_SELECTION_BLOCKED",
        ),
        (
            {"status": "BLOCKED", "blocker": "MAXIMUM_RISK_INVALID", "items": [{}]},
            "BLOCKED",
            "MAXIMUM_RISK_INVALID",
        ),
        ({"status": "FAILED", "blocker": "SELECTION_FAILED"}, "FAILED", "SELECTION_FAILED"),
        ({"status": "UNEXPECTED", "items": []}, "FAILED", "LOOP_SELECTION_STATUS_INVALID"),
        ({"status": "SELECTED", "items": None}, "FAILED", "LOOP_SELECTION_SCHEMA_INVALID"),
        ({"status": "SELECTED", "items": ["invalid"]}, "FAILED", "LOOP_SELECTION_SCHEMA_INVALID"),
    ],
)
async def test_selection_failure_does_not_reach_child_or_finalize(
    tmp_path, monkeypatch, selection, expected, blocker
):
    result, steps, children, _ = await run_selection(tmp_path, monkeypatch, selection)
    assert result == {"status": expected, "blocker": blocker, "results": []}
    assert steps == ["loop.load_selection"] and children == []


@pytest.mark.asyncio
@pytest.mark.parametrize("selection_status", ["SELECTED", "PASS"])
async def test_valid_selection_still_reaches_bounded_dry_run_child(
    tmp_path, monkeypatch, selection_status
):
    finding_id = str(uuid4())
    result, steps, children, _ = await run_selection(
        tmp_path,
        monkeypatch,
        {"status": selection_status, "items": [{"finding_id": finding_id, "risk_class": "R1"}]},
    )
    assert result["status"] == "PASS" and len(children) == 1
    assert children[0] == {"finding_id": finding_id, "risk_class": "R1", "dry_run": True}
    assert steps == [
        "loop.load_selection",
        "loop.acquire_component",
        "loop.record_item",
        "loop.release_component",
        "loop.finalize",
    ]


@pytest.mark.asyncio
async def test_unpatched_branch_keeps_legacy_command_order_only(tmp_path, monkeypatch):
    result, steps, children, _ = await run_selection(tmp_path, monkeypatch, patched=False)
    assert result["status"] == "PASS" and children == []
    assert steps == ["loop.load_selection", "loop.finalize"]
    # This preserves historical replay, not an acceptance of that false success.
