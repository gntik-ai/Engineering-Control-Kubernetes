"""NEW-131: UiMigrationPlanWorkflow drafts the blueprint in parts, on a real local Temporal.

Controller steps run the real UiPlanActivityHandler against SQLite; the scan, provider and
snapshot steps are scripted (the scan worker's part staging runs the real stage_plan). Every
history, recorded by the current class or by the pre-NEW-131 class, replays against the
current code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_123_fixtures import draft_output
from new_131_fixtures import draft_for, inventory_document, kit_document, part_review
from pre_new_131_ui_plan_workflow import ParentUiMigrationPlanWorkflow
from temporalio.worker import Replayer
from test_new_123_ui_workflows import (
    _TEMPORAL_CLI,
    Harness,
    agent,
    inventory,
    kit,
    provider_steps,
    reviewer,
    snapshot,
    stage_document,
    staged,
)

from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.ui_blueprint import ui_plan_workflow_id
from engineering_control.engineering.ui_blueprint_parts import (
    BATCH,
    FRAME,
    PART_TIMEOUT_SECONDS,
    part_schema,
    routed_page_ids,
    stage_plan,
)
from engineering_control.workflows.definitions import UiMigrationPlanWorkflow
from engineering_control.workflows.inputs import UiMigrationPlanInput

pytestmark = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH (local dev server)"
)
PATCH = UiMigrationPlanWorkflow._BATCHED_PATCH
PART_LOOP = [
    "ui_plan.prepare_part",
    "ui_plan.draft",
    "ui_plan.record_part_draft",
    "ui_plan.stage_document",
    "ui_plan.prepare_part_review",
    "ui_plan.review",
    "ui_plan.record_part_review",
]
STAGED_PLAN = stage_plan(inventory_document(), kit_document(), 8)
ROUTED = routed_page_ids(STAGED_PLAN["mechanical"])


def _meta(name: str, document: Any) -> dict[str, Any]:
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    return {"path": f"/in/{name}", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def stage_parts(payload: dict[str, Any]) -> dict[str, Any]:
    plan = stage_plan(inventory_document(), kit_document(), int(payload["batch_pages"]))
    return {
        "status": "STAGED",
        "blueprint_id": payload["blueprint_id"],
        "mechanical": plan["mechanical"],
        "mechanical_sha256": plan["mechanical_sha256"],
        "parts": [
            {**part, "input": _meta(part["input"], plan["files"][part["input"]])}
            for part in plan["parts"]
        ],
        "kit_slice": _meta(plan["kit_slice"], plan["files"][plan["kit_slice"]]),
    }


def stage_part_document(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "")
    if not name.startswith("part-"):
        return stage_document(payload)
    result = {"path": f"/in/{name}", "sha256": "3" * 64, "bytes": 1}
    if "-draft-" in name:
        # As the scan worker does: Hermes hashes exactly the part content the reviewer reads.
        kind = FRAME if name.startswith("part-frame-") else BATCH
        result["part_sha256"] = canonical_sha256(payload["document"][kind])
    return {"status": "STAGED", "document": result}


def with_usage(
    responder: Callable[[dict[str, Any]], dict[str, Any]], output: int
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        result = responder(payload)
        result["agent_result"]["usage"] = {
            "input_tokens": 10_000,
            "output_tokens": output,
            "cached_input_tokens": 2_000,
            "estimated_cost": None,
            "source": "provider-cli",
        }
        return result

    return respond


def part_drafter(payload: dict[str, Any]) -> dict[str, Any]:
    return with_usage(agent(draft_for(payload["part"], payload["part_id"], ROUTED)), 30_000)(
        payload
    )


def part_reviewer(revise: dict[int, int] | None = None) -> Callable[..., dict[str, Any]]:
    """REVISE the first `n` reviews of part index i (revise = {i: n}), APPROVE otherwise."""
    remaining = dict(revise or {})

    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        part = payload["part"]
        index = int(part["index"])
        decision = "APPROVE"
        if remaining.get(index, 0) > 0:
            remaining[index] -= 1
            decision = "REVISE"
        output = part_review(part["kind"], payload["part_id"], payload["draft_sha256"], decision)
        return with_usage(agent(output), 4_000)(payload)

    return respond


def batched_harness(
    tmp_path: Path, harness: Harness | None = None
) -> tuple[Harness, UiMigrationPlanInput, str]:
    harness = harness or Harness(tmp_path)
    created = harness.repository.create_sync("falcone", "cli:engineering-ctl", None)
    request = UiMigrationPlanInput(
        blueprint_id=created["id"], project="falcone", requested_by="cli:engineering-ctl"
    )
    harness.responses.update(
        {
            "project.snapshot": snapshot,
            "ui.inventory": inventory,
            "ui.kit_snapshot": kit,
            "ui_plan.stage_inputs": staged,
            "ui_plan.stage_parts": stage_parts,
            "ui_plan.stage_document": stage_part_document,
            "ui_plan.draft": part_drafter,
            "ui_plan.review": part_reviewer(),
        }
    )
    return harness, request, ui_plan_workflow_id("falcone", created["id"])


def markers(history: Any) -> list[str]:
    return [
        event.marker_recorded_event_attributes.SerializeToString().decode("utf-8", "replace")
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
    ]


def drafted_parts(harness: Harness) -> list[str]:
    return [payload["part_id"] for payload in harness.payloads.get("ui_plan.draft", [])]


# Happy path ---------------------------------------------------------------------------------


async def test_frame_then_seven_batches_each_reviewed_become_one_candidate(
    tmp_path: Path,
) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["parts"]) == ("CANDIDATE", 8)
    assert provider_steps(harness.calls) == [
        "ui_plan.register",
        "project.snapshot",
        "ui.inventory",
        "ui.kit_snapshot",
        "ui_plan.record_inputs",
        "ui_plan.stage_inputs",
        "ui_plan.stage_parts",
        "ui_plan.record_parts",
        *PART_LOOP,
        "ui_plan.stage_document",  # the complete reviewed frame, as a file
        *PART_LOOP * 7,
        "ui_plan.assemble",
        "ui_plan.stage_document",
        "ui_plan.persist",
        "ui_plan.finalize_run",
    ]
    assert any(PATCH in marker for marker in markers(harness.history))
    drafts = [payload["draft_request"] for payload in harness.payloads["ui_plan.draft"]]
    reviews = [payload["review_request"] for payload in harness.payloads["ui_plan.review"]]
    assert {(d["provider"], d["effort"], d["timeout_seconds"]) for d in drafts} == {
        ("claude", "high", PART_TIMEOUT_SECONDS)
    }
    assert {(r["provider"], r["effort"], r["timeout_seconds"]) for r in reviews} == {
        ("codex", "high", PART_TIMEOUT_SECONDS)
    }
    assert drafts[0]["output_schema"] == part_schema(FRAME)
    assert all(d["output_schema"] == part_schema(BATCH) for d in drafts[1:])
    # Each batch reads only its own slice and names only its own pages.
    third = harness.payloads["ui_plan.draft"][3]
    prompt = third["draft_request"]["prompt"]
    assert "/in/part-b003-input.json" in prompt and "/in/kit-catalogue.json" in prompt
    assert all(page in prompt for page in third["part"]["page_ids"])
    assert "/in/part-b002-input.json" not in prompt and "/in/ui-inventory.json" not in prompt
    frame_id = harness.payloads["ui_plan.draft"][0]["part_id"]
    assert f"/in/part-{frame_id}-brief.json" in prompt
    assert "--- BEGIN FRAME ---" not in prompt and "Hermes fills every mechanical field" in prompt
    assert "/in/ui-kit-snapshot.json" in drafts[0]["prompt"]
    view = harness.repository.get_sync(UUID(request.blueprint_id))
    assert view is not None
    assert (view["status"], view["approval_binding"]) == ("CANDIDATE", result["approval_binding"])
    assert view["pages"] == len(STAGED_PLAN["mechanical"]["pages"])
    with harness.repository.sessions() as session:
        from engineering_control.db.models import UiBlueprint

        row = session.get(UiBlueprint, UUID(request.blueprint_id))
        assert row is not None
        stored = row.blueprint_json
    assert stored is not None
    filled = {page["page_id"]: page for page in STAGED_PLAN["mechanical"]["pages"]}
    assert all(
        page["endpoints"] == filled[page["page_id"]]["endpoints"] for page in stored["pages"]
    )
    # Usage: 8 Claude drafts and 8 Codex reviews, recorded per call and totalled.
    assert view["usage"]["total"] == {
        "calls": 16,
        "input_tokens": 160_000,
        "output_tokens": 8 * 30_000 + 8 * 4_000,
        "cached_input_tokens": 32_000,
    }
    assert view["usage"]["providers"]["claude"]["calls"] == 8
    markdown = harness.repository.markdown_sync(UUID(request.blueprint_id))
    assert markdown is not None and "8 parts (the frame and 7 page batches)" in markdown


# Per-part review ----------------------------------------------------------------------------


async def test_a_revise_redrafts_only_that_batch(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.review"] = part_reviewer({3: 1})
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    parts = drafted_parts(harness)
    assert len(parts) == 9
    third = [part for part in parts if part.startswith("b003-")]
    assert len(third) == 2 and len(set(parts)) == 8
    redraft = harness.payloads["ui_plan.draft"][4]
    assert redraft["part_id"] == third[0] and redraft["draft_attempt"] == 2
    prompt = redraft["draft_request"]["prompt"]
    assert f"The 403 state is missing in {third[0]}." in prompt
    assert f"/in/part-{third[0]}-review-1.json" in prompt
    names = [p.get("name") for p in harness.payloads["ui_plan.stage_document"]]
    assert f"part-{third[0]}-draft-2.json" in names
    assert sum(1 for name in names if name and "-review-" in name) == 1
    assert result["review_rounds"] == 9


async def test_a_second_revise_blocks_with_the_batch_and_its_findings(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.review"] = part_reviewer({2: 2})
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == (
        "BLOCKED",
        "BLUEPRINT_PART_REVIEW_UNRESOLVED",
    )
    parts = drafted_parts(harness)
    assert [part.split("-")[0] for part in parts] == ["frame", "b001", "b002", "b002"]
    assert result["blockers"][0].startswith(f"part {parts[-1]}, batch 2 (")
    assert any(f"The 403 state is missing in {parts[-1]}." in item for item in result["blockers"])
    assert "ui_plan.assemble" not in harness.calls and "ui_plan.persist" not in harness.calls
    view = harness.repository.get_sync(UUID(request.blueprint_id))
    assert view is not None and view["status"] == "BLOCKED"
    assert view["blockers"][0].startswith(f"part {parts[-1]}")


async def test_a_new_plan_reuses_every_part_already_reviewed(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.review"] = part_reviewer({4: 2})
    first = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert first["status"] == "BLOCKED"
    reviewed = drafted_parts(harness)[:4]
    harness.calls.clear()
    harness.payloads.clear()
    harness, again, again_id = batched_harness(tmp_path, harness)
    result = await harness.run(UiMigrationPlanWorkflow, again, again_id)
    assert result["status"] == "CANDIDATE"
    drafted = drafted_parts(harness)
    assert [part.split("-")[0] for part in drafted] == ["b004", "b005", "b006", "b007"]
    reused = [p for p in harness.payloads["ui_plan.prepare_part"] if p.get("draft_attempt") == 1]
    assert len(reused) == 8
    view = harness.repository.get_sync(UUID(again.blueprint_id))
    assert view is not None and view["usage"]["total"]["calls"] == 8
    with harness.repository.sessions() as session:
        from engineering_control.db.models import UiBlueprint

        row = session.get(UiBlueprint, UUID(again.blueprint_id))
        assert row is not None
        adopted = [e for e in row.drafts_json if e.get("reused_from") == first["blueprint_id"]]
    assert sorted(entry["part"] for entry in adopted) == sorted(reviewed)


# Replay -------------------------------------------------------------------------------------


async def test_a_pre_new_131_history_replays_unchanged_and_new_histories_carry_the_patch(
    tmp_path: Path,
) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.stage_document"] = stage_document
    harness.responses["ui_plan.draft"] = agent(draft_output())
    harness.responses["ui_plan.review"] = reviewer("APPROVE")
    result = await harness.run(
        ParentUiMigrationPlanWorkflow, request, workflow_id, replay=UiMigrationPlanWorkflow
    )
    assert result["status"] == "CANDIDATE"
    old = harness.history
    assert not any(PATCH in marker for marker in markers(old))
    assert "ui_plan.stage_parts" not in harness.calls
    # The recorded whole-blueprint history replays against the current class (also above).
    await Replayer(workflows=[UiMigrationPlanWorkflow]).replay_workflow(old)
    # A batched history does not replay against the pre-NEW-131 code: the patch is real.
    (tmp_path / "new").mkdir()
    newer, new_request, new_id = batched_harness(tmp_path / "new")
    assert (await newer.run(UiMigrationPlanWorkflow, new_request, new_id))["status"] == "CANDIDATE"
    with pytest.raises(Exception):  # noqa: B017 - any nondeterminism error
        await Replayer(workflows=[ParentUiMigrationPlanWorkflow]).replay_workflow(newer.history)


async def test_a_large_frame_reaches_every_batch_complete_as_a_file(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)

    def large_frame(payload: dict[str, Any]) -> dict[str, Any]:
        output = draft_for(payload["part"], payload["part_id"], ROUTED)
        if payload["part"]["kind"] == FRAME:
            output["frame"]["gaps"] += [
                {
                    **output["frame"]["gaps"][0],
                    "id": f"gap-cross-{index:02d}",
                    "summary": "A cross-page capability is missing. " * 20,
                    "blocks": [],
                }
                for index in range(43)
            ]
            output["frame"]["global_decisions"] = [f"Decision {i}: " + "x" * 400 for i in range(30)]
        return with_usage(agent(output), 30_000)(payload)

    harness.responses["ui_plan.draft"] = large_frame
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    briefs = [
        payload
        for payload in harness.payloads["ui_plan.stage_document"]
        if str(payload.get("name") or "").endswith("-brief.json")
    ]
    assert len(briefs) == 1
    frame = briefs[0]["document"]["frame"]
    assert len(json.dumps(briefs[0]["document"])) > 40_000
    assert len(frame["gaps"]) == 45
    assert [gap["id"] for gap in frame["gaps"][2:]] == [f"gap-cross-{i:02d}" for i in range(43)]
    assert len(frame["global_decisions"]) == 30
    for payload in harness.payloads["ui_plan.draft"][1:] + harness.payloads["ui_plan.review"][1:]:
        request_key = "draft_request" if "draft_request" in payload else "review_request"
        prompt = payload[request_key]["prompt"]
        assert f"/in/{briefs[0]['name']}" in prompt
        assert "45 gaps, 30 global decisions" in prompt
        # NEW-135: every global decision is restated verbatim and numbered, as a checklist.
        for index, decision in enumerate(frame["global_decisions"], start=1):
            assert f"\n{index}. {decision}" in prompt


async def test_a_failed_provider_call_still_counts_in_the_usage(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    approve = part_reviewer()

    def rate_limited_second_review(payload: dict[str, Any]) -> dict[str, Any]:
        result = approve(payload)
        if payload["part"]["kind"] == BATCH:
            result["status"] = "RATE_LIMITED"
            result["blocker"] = "RATE_LIMITED"
            result["agent_result"]["status"] = "RATE_LIMITED"
        return result

    harness.responses["ui_plan.review"] = rate_limited_second_review
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "RATE_LIMITED")
    view = harness.repository.get_sync(UUID(request.blueprint_id))
    assert view is not None
    assert view["usage"]["total"]["calls"] == 4  # frame draft and review, b001 draft and review
    assert view["usage"]["providers"]["codex"] == {
        "calls": 2,
        "input_tokens": 20_000,
        "output_tokens": 8_000,
        "cached_input_tokens": 4_000,
    }
