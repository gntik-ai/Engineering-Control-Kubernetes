"""NEW-132: gap `blocks` holds only IDs, and a draft Hermes rejects gets the part's redraft.

Run 76684f83 blocked after its frame call: three frame gaps put prose in `blocks`, and a
first-attempt draft rejected by Hermes's own schema or structure check was terminal. Workflow
tests run on a real local Temporal and replay old and new batched histories.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_123_fixtures import INVENTORY_SHA, KIT_SHA, KIT_VERSION, SOURCE_SHA
from new_131_fixtures import draft_for, frame_content, part_output
from pre_new_132_ui_plan_workflow import ParentBatchedUiMigrationPlanWorkflow
from temporalio.worker import Replayer
from test_new_123_ui_workflows import Harness, agent
from test_new_131_ui_batched_workflow import (
    ROUTED,
    batched_harness,
    markers,
    with_usage,
)

from engineering_control.engineering.ui_blueprint import BlueprintRejected, PlanInputs
from engineering_control.engineering.ui_blueprint_parts import FRAME, check_part_draft
from engineering_control.workflows.definitions import UiMigrationPlanWorkflow

PATCH = UiMigrationPlanWorkflow._STRUCTURAL_REDRAFT_PATCH
INPUTS = PlanInputs("falcone", SOURCE_SHA, INVENTORY_SHA, KIT_SHA, KIT_VERSION)
PROSE = "every page with a server-paginated list (each batch names its pages)"


def frame_with_blocks(blocks: list[str]) -> dict[str, Any]:
    frame = frame_content(ROUTED[0])
    frame["gaps"][1]["blocks"] = blocks
    return frame


def test_prose_in_gap_blocks_fails_the_schema_and_an_epic_passes() -> None:
    all_ids = [*ROUTED]
    with pytest.raises(BlueprintRejected) as rejected:
        check_part_draft(
            part_output(FRAME, "frame-" + "0" * 16, frame_with_blocks([PROSE])),
            FRAME,
            INPUTS,
            "frame-" + "0" * 16,
            context={"all_page_ids": all_ids},
        )
    assert rejected.value.code == "DRAFT_SCHEMA_INVALID"
    assert any("blocks" in item for item in rejected.value.details)
    for blocks in (["u0-scaffold"], ["u5-cutover"], [ROUTED[3]]):
        frame = check_part_draft(
            part_output(FRAME, "frame-" + "0" * 16, frame_with_blocks(blocks)),
            FRAME,
            INPUTS,
            "frame-" + "0" * 16,
            context={"all_page_ids": all_ids},
        )
        assert frame["gaps"][1]["blocks"] == blocks


def frame_drafter(*first_blocks: list[str]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Frame drafts use these blocks in turn (then valid ones); batches are valid."""
    queue = [list(blocks) for blocks in first_blocks]

    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        output = draft_for(payload["part"], payload["part_id"], ROUTED)
        if payload["part"]["kind"] == FRAME and queue:
            output = copy.deepcopy(output)
            output["frame"]["gaps"][1]["blocks"] = queue.pop(0)
        return with_usage(agent(output), 30_000)(payload)

    return respond


def frame_drafts(harness: Harness) -> list[dict[str, Any]]:
    return [p for p in harness.payloads["ui_plan.draft"] if p["part"]["kind"] == FRAME]


async def test_a_schema_rejected_frame_is_redrafted_with_hermes_findings(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.draft"] = frame_drafter([PROSE])
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    drafts = frame_drafts(harness)
    assert [d["draft_attempt"] for d in drafts] == [1, 2]
    prompt = drafts[1]["draft_request"]["prompt"]
    assert "Hermes rejected your previous draft deterministically (DRAFT_SCHEMA_INVALID)" in prompt
    assert "blocks" in prompt and "--- BEGIN HERMES FINDINGS ---" in prompt
    frame_reviews = [p for p in harness.payloads["ui_plan.review"] if p["part"]["kind"] == FRAME]
    assert len(frame_reviews) == 1  # the rejected draft is never reviewed
    assert any(PATCH in marker for marker in markers(harness.history))
    view = harness.repository.get_sync(UUID(request.blueprint_id))
    assert view is not None
    assert view["usage"]["providers"]["claude"]["calls"] == 9  # the rejected call counts


async def test_a_structure_rejected_frame_rejected_again_blocks(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.draft"] = frame_drafter(["u0-unknown"], ["u0-unknown"])
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "DRAFT_STRUCTURE_INVALID")
    assert any("blocks unknown u0-unknown" in item for item in result["blockers"])
    assert [d["draft_attempt"] for d in frame_drafts(harness)] == [1, 2]
    assert "ui_plan.review" not in harness.calls


async def test_a_pre_new_132_batched_history_replays_unchanged(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.draft"] = frame_drafter([PROSE])
    result = await harness.run(
        ParentBatchedUiMigrationPlanWorkflow, request, workflow_id, replay=UiMigrationPlanWorkflow
    )
    # The deployed (pre-NEW-132) behaviour: the rejected first draft was terminal.
    assert (result["status"], result["error_code"]) == ("BLOCKED", "DRAFT_SCHEMA_INVALID")
    assert not any(PATCH in marker for marker in markers(harness.history))
    await Replayer(workflows=[UiMigrationPlanWorkflow]).replay_workflow(harness.history)
    # A redrafted history needs the new code: the patch is real.
    (tmp_path / "new").mkdir()
    newer, new_request, new_id = batched_harness(tmp_path / "new")
    newer.responses["ui_plan.draft"] = frame_drafter([PROSE])
    assert (await newer.run(UiMigrationPlanWorkflow, new_request, new_id))["status"] == "CANDIDATE"
    with pytest.raises(Exception):  # noqa: B017 - any nondeterminism error
        await Replayer(workflows=[ParentBatchedUiMigrationPlanWorkflow]).replay_workflow(
            newer.history
        )
