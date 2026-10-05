"""NEW-138 (operator, 2026-10-05): each UI blueprint part gets up to two redrafts.

Live plan 45d072e5 blocked on batch 3's second review with one small finding; with a
single redraft each batch was close to a coin flip and every block forced a restart. Parts
now get three drafts and three reviews (per-part patch new-138-two-redrafts-<kind>-<index>);
Hermes's structural rejections share the same budget. Workflow tests run on a real local
Temporal (skipped without the `temporal` CLI) and replay pre-change histories.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pre_new_138_ui_plan_workflow import PreNew138UiMigrationPlanWorkflow
from temporalio.worker import Replayer
from test_new_131_ui_batched_workflow import (
    _TEMPORAL_CLI,
    batched_harness,
    drafted_parts,
    markers,
    part_reviewer,
)
from test_new_132_structural_redraft import PROSE, frame_drafter

from engineering_control.engineering.ui_blueprint_parts import BATCH, FRAME
from engineering_control.workflows.definitions import UiMigrationPlanWorkflow

temporal = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH (local dev server)"
)

PATCH = UiMigrationPlanWorkflow._TWO_REDRAFTS_PATCH
PARTS = 8  # the frame and seven page batches of the fixture inventory


def _usage(harness: Any, blueprint_id: str) -> dict[str, Any]:
    view = harness.repository.get_sync(UUID(blueprint_id))
    assert view is not None
    usage: dict[str, Any] = view["usage"]
    return usage


def _drafts(harness: Any, kind: str, index: int) -> list[dict[str, Any]]:
    return [
        payload
        for payload in harness.payloads["ui_plan.draft"]
        if payload["part"]["kind"] == kind and payload["part"]["index"] == index
    ]


@temporal
async def test_revise_revise_approve_gives_a_candidate(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.review"] = part_reviewer({2: 2})
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    parts = drafted_parts(harness)
    assert [part.split("-")[0] for part in parts].count("b002") == 3
    drafts = _drafts(harness, BATCH, 2)
    assert [d["draft_attempt"] for d in drafts] == [1, 2, 3]
    assert {d["max_draft_attempts"] for d in drafts[1:]} == {3}
    third = drafts[2]["draft_request"]["prompt"]
    assert "This is redraft 2 of at most 2 of this part" in third
    assert "--- BEGIN REVIEW FINDINGS ---" in third
    assert any(f"{PATCH}-batch-2" in marker for marker in markers(harness.history))
    # Usage counts every call: 8 parts plus two redrafts, each drafted and reviewed.
    usage = _usage(harness, request.blueprint_id)
    assert usage["providers"]["claude"]["calls"] == PARTS + 2
    assert usage["providers"]["codex"]["calls"] == PARTS + 2
    assert usage["total"]["calls"] == 2 * (PARTS + 2)
    await Replayer(workflows=[UiMigrationPlanWorkflow]).replay_workflow(harness.history)


@temporal
async def test_three_revises_block_and_every_call_counts(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.review"] = part_reviewer({0: 3})
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == (
        "BLOCKED",
        "BLUEPRINT_PART_REVIEW_UNRESOLVED",
    )
    assert [d["draft_attempt"] for d in _drafts(harness, FRAME, 0)] == [1, 2, 3]
    assert result["blockers"][0].startswith("part frame-")
    usage = _usage(harness, request.blueprint_id)
    assert usage["providers"]["claude"]["calls"] == 3
    assert usage["providers"]["codex"]["calls"] == 3


@temporal
async def test_a_schema_reject_then_revise_then_approve_works(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.draft"] = frame_drafter([PROSE])
    harness.responses["ui_plan.review"] = part_reviewer({0: 1})
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    drafts = _drafts(harness, FRAME, 0)
    assert [d["draft_attempt"] for d in drafts] == [1, 2, 3]
    second, third = drafts[1]["draft_request"]["prompt"], drafts[2]["draft_request"]["prompt"]
    assert "Hermes rejected your previous draft deterministically (DRAFT_SCHEMA_INVALID)" in second
    assert "Another rejection uses the next redraft." in second
    # The third draft answers the review, not the earlier rejection.
    assert "Hermes rejected" not in third and "--- BEGIN REVIEW FINDINGS ---" in third
    frame_reviews = [p for p in harness.payloads["ui_plan.review"] if p["part"]["kind"] == FRAME]
    assert [p["draft_attempt"] for p in frame_reviews] == [2, 3]
    usage = _usage(harness, request.blueprint_id)
    assert usage["providers"]["claude"]["calls"] == PARTS + 2  # the rejected call counts
    assert usage["providers"]["codex"]["calls"] == PARTS + 1


@temporal
async def test_pre_new_138_histories_replay_unchanged(tmp_path: Path) -> None:
    # The deployed behaviour: a second REVISE blocked the plan.
    (tmp_path / "blocked").mkdir()
    harness, request, workflow_id = batched_harness(tmp_path / "blocked")
    harness.responses["ui_plan.review"] = part_reviewer({2: 2})
    result = await harness.run(
        PreNew138UiMigrationPlanWorkflow, request, workflow_id, replay=UiMigrationPlanWorkflow
    )
    assert (result["status"], result["error_code"]) == (
        "BLOCKED",
        "BLUEPRINT_PART_REVIEW_UNRESOLVED",
    )
    assert [part.split("-")[0] for part in drafted_parts(harness)].count("b002") == 2
    assert not any(PATCH in marker for marker in markers(harness.history))
    await Replayer(workflows=[UiMigrationPlanWorkflow]).replay_workflow(harness.history)
    # A pre-change history in which several parts used their single redraft and the plan
    # went on: every part evaluates its own marker (false on replay), unchanged.
    (tmp_path / "redrafted").mkdir()
    old, old_request, old_id = batched_harness(tmp_path / "redrafted")
    old.responses["ui_plan.review"] = part_reviewer({0: 1, 2: 1, 5: 1})
    finished = await old.run(
        PreNew138UiMigrationPlanWorkflow, old_request, old_id, replay=UiMigrationPlanWorkflow
    )
    assert finished["status"] == "CANDIDATE"
    await Replayer(workflows=[UiMigrationPlanWorkflow]).replay_workflow(old.history)
    # A history with a third attempt needs the new code: the patch is real.
    (tmp_path / "new").mkdir()
    newer, new_request, new_id = batched_harness(tmp_path / "new")
    newer.responses["ui_plan.review"] = part_reviewer({2: 2})
    assert (await newer.run(UiMigrationPlanWorkflow, new_request, new_id))["status"] == (
        "CANDIDATE"
    )
    with pytest.raises(Exception):  # noqa: B017 - any nondeterminism error
        await Replayer(workflows=[PreNew138UiMigrationPlanWorkflow]).replay_workflow(newer.history)


# Activities: every bound on the draft attempt accepts 3 under the patch --------------------


def test_part_document_names_and_draft_bounds_accept_a_third_attempt(tmp_path: Path) -> None:
    from unittest.mock import Mock

    from new_123_fixtures import build_fake_pack

    from engineering_control.activities import ui_plan
    from engineering_control.activities.ui_plan_scan import _PART_DOCUMENT_NAME
    from engineering_control.config import Settings

    for name in ("draft", "review"):
        assert _PART_DOCUMENT_NAME.fullmatch(f"part-b003-{'a' * 16}-{name}-3.json")
        assert not _PART_DOCUMENT_NAME.fullmatch(f"part-b003-{'a' * 16}-{name}-4.json")
    handler = ui_plan.UiPlanActivityHandler(
        Settings(agent_pack_root=build_fake_pack(tmp_path / "pack", version="1.5.1")),
        repository=Mock(),
        change_requests=Mock(),
    )
    maximum = handler._max_part_attempts
    assert maximum({}) == 2 and maximum({"max_draft_attempts": 3}) == 3
    assert maximum({"max_draft_attempts": 4}) is None
    assert maximum({"max_draft_attempts": True}) is None


@temporal
async def test_a_schema_reject_revise_then_schema_reject_blocks_with_its_findings(
    tmp_path: Path,
) -> None:
    # NEW-138 round 1: the REVISE clears the first rejection; the third (final) provider
    # schema refusal still ends BLOCKED DRAFT_SCHEMA_INVALID with its own findings.
    from test_new_132_structural_redraft import provider_boundary

    harness, request, workflow_id = batched_harness(tmp_path)
    adapter, draft = provider_boundary(tmp_path, harness, prose=1)

    async def drafter(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["part"]["kind"] == FRAME and payload["draft_attempt"] == 3:
            adapter.prose = 1  # refused again on the last attempt
        result: dict[str, Any] = await draft(payload)
        return result

    harness.responses["ui_plan.draft"] = drafter
    harness.responses["ui_plan.review"] = part_reviewer({0: 1})
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "DRAFT_SCHEMA_INVALID")
    assert adapter.calls == [FRAME, FRAME, FRAME]
    assert result["blockers"] and all(item.startswith("frame-") for item in result["blockers"])
    assert any("frame/gaps/1/blocks/0 [pattern]" in item for item in result["blockers"])
    third = _drafts(harness, FRAME, 0)[2]["draft_request"]["prompt"]
    assert "--- BEGIN REVIEW FINDINGS ---" in third and "Hermes rejected" not in third
    usage = _usage(harness, request.blueprint_id)
    assert usage["providers"]["claude"]["calls"] == 3  # both refused calls count
    assert usage["providers"]["codex"]["calls"] == 1
    await Replayer(workflows=[UiMigrationPlanWorkflow]).replay_workflow(harness.history)
