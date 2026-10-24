"""NEW-234 cross-run part continuation (as NEW-142 for the UI blueprint): a part an
earlier, ended plan left unresolved starts the next plan from its last reviewed draft plus
every finding of that plan on it, instead of a fresh draft. The lookup and the staged file
live in the activities (the workflow stages and carries any file prepare_part_input
returns), so the workflow and its replay are unchanged; the budgets are unchanged.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from new_234_plan_fixtures import PlanHarness, drive, stage_inputs
from sqlalchemy import update

from engineering_control.db.models import BacklogDecomposition
from engineering_control.engineering.decomposition import part_content_sha256
from engineering_control.engineering.decomposition_inventory import (
    _STAGED_NAME,
    part_previous_name,
    piece_name,
)

PROJECT = "falcone-serverless"


def _run_epic(payload: dict[str, Any]) -> bool:
    return payload["part"]["kind"] == "epic" and payload["part"]["number"] == 2


def _end(harness: PlanHarness, created: dict[str, Any], result: dict[str, Any]) -> None:
    """As the workflow's _part_terminal does: the plan ends BLOCKED on the part."""
    ended = harness.step(
        "decomposition.record_terminal",
        {
            "decomposition_id": created["id"],
            "project": PROJECT,
            "terminal_status": "BLOCKED",
            "blocker": result["blocker"],
            "blockers": result.get("blockers") or [],
        },
    )
    assert ended["status"] == "BLOCKED", ended


def _unresolved_run(harness: PlanHarness) -> dict[str, Any]:
    """A plan whose RUN epic batch ends unresolved after three REVISE rounds."""
    created = harness.create()

    def decide(payload: dict[str, Any], attempt: int) -> str:
        return "REVISE" if _run_epic(payload) else "APPROVE"

    result = drive(harness, created["id"], decide=decide)
    assert result["blocker"] == "DECOMPOSITION_PART_REVIEW_UNRESOLVED", result
    _end(harness, created, result)
    return created


def _part(harness: PlanHarness, created: dict[str, Any], number: int) -> dict[str, Any]:
    view = harness.repository.get_sync(UUID(created["id"]))
    assert view is not None
    return next(item for item in view["parts"] if item["number"] == number)


def _run_prompts(harness: PlanHarness, provider: str) -> list[str]:
    return [
        item["prompt"]
        for item in harness.requests
        if item["provider"] == provider and "exactly these stories: RUN-01" in item["prompt"]
    ]


def test_an_unresolved_part_continues_from_its_last_draft_and_every_finding(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    earlier = harness.repository.part_sync(UUID(first["id"]), _part(harness, first, 2)["part_id"])
    assert earlier is not None and earlier["approved_attempt"] is None
    harness.requests.clear()
    second = harness.create()
    result = drive(harness, second["id"])
    assert result["status"] == "CANDIDATE", result
    # The frame is reused; only the unresolved RUN batch continues; API drafts fresh.
    frame = next(
        item
        for item in harness.repository.get_sync(UUID(second["id"]))["parts"]  # type: ignore[index]
        if item["kind"] == "frame"
    )
    assert frame["reused_from"]["decomposition_id"] == first["id"]
    assert frame["continued_from"] is None
    run = _part(harness, second, 2)
    assert run["continued_from"] == {
        "decomposition_id": first["id"],
        "attempt": 3,
        "content_sha256": earlier["versions"][-1]["content_sha256"],
    }
    assert _part(harness, second, 1)["continued_from"] is None
    # The first draft call of this plan is a redraft of draft 3 of the first plan, with
    # every round's finding labelled by that run; budgets are unchanged (1 draft, 1 review
    # each for RUN and API).
    view = harness.repository.get_sync(UUID(second["id"]))
    assert view is not None and view["provider_calls"] == 4
    [draft] = _run_prompts(harness, "claude")
    assert f"This part continues an earlier plan run (decomposition {first['id']})" in draft
    assert "draft 3 of this exact part" in draft and "do not draft from scratch" in draft
    previous = f"part-{run['part_id']}-previous.json"
    assert previous in draft
    label = f"previous run {first['id'][:8]}, round 1 (review of draft 1)"
    assert label in draft and "round 3 (review of draft 3)" in draft
    assert "Cover the trap error path explicitly." in draft
    [review] = _run_prompts(harness, "codex")
    assert "rounds of the earlier plan run this part continues are labelled" in review
    assert label in review
    api = [
        item["prompt"]
        for item in harness.requests
        if item["provider"] == "claude" and "exactly these stories: API-01" in item["prompt"]
    ]
    assert len(api) == 1 and "continues an earlier plan run" not in api[0]


def test_the_staged_continuation_carries_the_draft_and_the_complete_findings(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    part_id = _part(harness, first, 2)["part_id"]
    earlier = harness.repository.part_sync(UUID(first["id"]), part_id)
    assert earlier is not None
    second = harness.create()
    captured: list[dict[str, Any]] = []

    def stage(payload: dict[str, Any]) -> dict[str, Any]:
        captured.append(payload["files"])
        return stage_inputs(payload)

    harness.requests.clear()
    drive(harness, second["id"], stage_files=stage)
    name = part_previous_name(part_id)
    [files] = [item for item in captured if name in item]
    assert _STAGED_NAME.fullmatch(name) and _STAGED_NAME.fullmatch(piece_name(name, 1, 2))
    document = json.loads(files[name])
    assert document["kind"] == "previous-part-draft"
    assert (document["previous_decomposition_id"], document["previous_attempt"]) == (
        first["id"],
        3,
    )
    assert document["epic"] == earlier["versions"][-1]["content"]
    assert part_content_sha256(document["epic"]) == document["content_sha256"]
    assert [item["attempt"] for item in document["prior_reviews"]] == [1, 2, 3]
    assert len(document["prior_findings"]) == 1 and "round 3" in document["prior_findings"][0]


def test_a_continued_redraft_keeps_the_earlier_runs_findings(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    harness.requests.clear()
    second = harness.create()

    def decide(payload: dict[str, Any], attempt: int) -> str:
        return "REVISE" if _run_epic(payload) and attempt == 1 else "APPROVE"

    assert drive(harness, second["id"], decide=decide)["status"] == "CANDIDATE"
    drafts = _run_prompts(harness, "claude")
    assert len(drafts) == 2
    # Draft 2 is an ordinary redraft (the budget is per plan), still carrying run 1's rounds.
    assert "redraft 1 of at most 2" in drafts[1]
    assert f"previous run {first['id'][:8]}, round 3 (review of draft 3)" in drafts[1]
    assert "continues an earlier plan run" not in drafts[1]
    reviews = _run_prompts(harness, "codex")
    assert all(f"previous run {first['id'][:8]}" in text for text in reviews)


def test_the_most_recent_unresolved_plan_is_continued(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    second = _unresolved_run(harness)
    assert _part(harness, second, 2)["continued_from"]["decomposition_id"] == first["id"]
    harness.requests.clear()
    third = harness.create()
    assert drive(harness, third["id"])["status"] == "CANDIDATE"
    assert _part(harness, third, 2)["continued_from"]["decomposition_id"] == second["id"]
    [draft] = _run_prompts(harness, "claude")
    assert f"decomposition {second['id']}" in draft and first["id"] not in draft


def test_a_plan_still_drafting_is_not_continued(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    result = drive(harness, created["id"], decide=lambda payload, attempt: "REVISE")
    assert result["blocker"] == "DECOMPOSITION_PART_REVIEW_UNRESOLVED"
    # Not ended (still DRAFTING): the next plan drafts the frame from scratch.
    harness.requests.clear()
    second = harness.create()
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    view = harness.repository.get_sync(UUID(second["id"]))
    assert view is not None and all(item["continued_from"] is None for item in view["parts"])
    assert not any("continues an earlier plan run" in item["prompt"] for item in harness.requests)


def test_an_earlier_draft_failing_the_current_checks_is_not_continued(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    part_id = _part(harness, first, 2)["part_id"]
    with harness.repository.sessions() as session:
        row = session.get(BacklogDecomposition, UUID(first["id"]))
        assert row is not None
        parts = copy.deepcopy(row.parts_json)
    version = parts["parts"][part_id]["versions"][-1]
    version["content"]["stories"] = []  # no longer covers the batch's stories
    version["content_sha256"] = version["reviewed_sha256"] = part_content_sha256(version["content"])
    with harness.repository.sessions.begin() as session:
        session.execute(
            update(BacklogDecomposition)
            .where(BacklogDecomposition.id == UUID(first["id"]))
            .values(parts_json=parts)
        )
    harness.requests.clear()
    second = harness.create()
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    assert _part(harness, second, 2)["continued_from"] is None
    assert "continues an earlier plan run" not in _run_prompts(harness, "claude")[0]


def test_a_continuation_needs_its_staged_receipts(tmp_path: Path) -> None:
    """A draft step whose payload does not carry the staged file (an in-flight plan whose
    part input predates the change), or carries another file, drafts from scratch."""
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    second = harness.create()
    seen: list[dict[str, Any]] = []
    original = harness.step

    def step(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name == "decomposition.prepare_part" and _run_epic(payload):
            seen.append(copy.deepcopy(payload))
        return original(name, payload)

    harness.step = step  # type: ignore[method-assign]
    drive(harness, second["id"])
    harness.step = original  # type: ignore[method-assign]
    payload = seen[0]
    name = part_previous_name(payload["part_id"])
    assert name in payload["reference_files"]
    assert (
        "continues an earlier plan run"
        in original("decomposition.prepare_part", payload)["draft_request"]["prompt"]
    )
    without = {**payload, "reference_files": {}}
    tampered = copy.deepcopy(payload)
    tampered["reference_files"][name]["sha256"] = "0" * 64
    for variant in (without, tampered):
        prompt = original("decomposition.prepare_part", variant)["draft_request"]["prompt"]
        assert "continues an earlier plan run" not in prompt
        assert f"previous run {first['id'][:8]}" not in prompt
