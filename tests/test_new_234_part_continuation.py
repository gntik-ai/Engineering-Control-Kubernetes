"""NEW-234 cross-run part continuation (as NEW-142 for the UI blueprint): a part an
earlier, ended plan left unresolved starts the next plan from its last reviewed draft plus
every finding of that plan on it, instead of a fresh draft. The lookup and the staged file
live in the activities (the workflow stages and carries any file prepare_part_input
returns), so the workflow and its replay are unchanged; the budgets are unchanged.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_234_plan_fixtures import PlanHarness, draft_for, drive, stage_inputs
from sqlalchemy import update

from engineering_control.db import backlog_decompositions as decompositions_db
from engineering_control.db.backlog_decompositions import REUSE_SEARCH
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


def _capture(harness: PlanHarness, name: str) -> tuple[list[dict[str, Any]], Any]:
    """Record the payloads of step `name` for the RUN epic; returns (seen, original step)."""
    seen: list[dict[str, Any]] = []
    original = harness.step

    def step(called: str, payload: dict[str, Any]) -> dict[str, Any]:
        if called == name and _run_epic(payload):
            seen.append(copy.deepcopy(payload))
        return original(called, payload)

    harness.step = step  # type: ignore[method-assign]
    return seen, original


def _set_parts(harness: PlanHarness, identity: str, parts: dict[str, Any]) -> None:
    with harness.repository.sessions.begin() as session:
        session.execute(
            update(BacklogDecomposition)
            .where(BacklogDecomposition.id == UUID(identity))
            .values(parts_json=parts)
        )


def _parts(harness: PlanHarness, identity: str) -> dict[str, Any]:
    with harness.repository.sessions() as session:
        row = session.get(BacklogDecomposition, UUID(identity))
        assert row is not None
        return copy.deepcopy(row.parts_json)


def test_a_continuation_is_used_only_as_pinned_and_staged(tmp_path: Path) -> None:
    """A draft step carrying a staged file that differs from the pinned source, or a part
    that pinned nothing (an in-flight plan whose part input predates the change), drafts
    from scratch."""
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    second = harness.create()
    seen, original = _capture(harness, "decomposition.prepare_part")
    drive(harness, second["id"])
    harness.step = original  # type: ignore[method-assign]
    payload = seen[0]
    name = part_previous_name(payload["part_id"])
    assert name in payload["reference_files"]
    assert (
        "continues an earlier plan run"
        in original("decomposition.prepare_part", payload)["draft_request"]["prompt"]
    )
    tampered = copy.deepcopy(payload)
    tampered["reference_files"][name]["sha256"] = "0" * 64
    parts = _parts(harness, second["id"])
    parts["parts"][payload["part_id"]].pop("continued_from")
    prompts = [original("decomposition.prepare_part", tampered)["draft_request"]["prompt"]]
    _set_parts(harness, second["id"], parts)
    prompts.append(original("decomposition.prepare_part", payload)["draft_request"]["prompt"])
    for prompt in prompts:
        assert "continues an earlier plan run" not in prompt
        assert f"previous run {first['id'][:8]}" not in prompt


# Review findings (Codex, ae4c70c) ---------------------------------------------------------


def test_an_assembly_redraft_keeps_the_previous_runs_findings(tmp_path: Path) -> None:
    """Finding 1: the workflow's assembly redraft carries no staged receipts; the pinned
    continuation is re-read from the database for the redraft and its review."""
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    second = harness.create()
    seen, original = _capture(harness, "decomposition.prepare_part_review")
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    harness.step = original  # type: ignore[method-assign]
    reviewed = seen[0]
    # As BacklogDecompositionPlanWorkflow's assembly redraft builds it: no reference_files.
    redraft = {
        key: value
        for key, value in reviewed.items()
        if key not in {"reference_files", "draft_file", "draft_sha256", "status"}
    }
    redraft["rejection"] = {"code": "ASSEMBLY_INVALID", "details": ["RUN-01.t1 depends on x"]}
    label = f"previous run {first['id'][:8]}, round 3 (review of draft 3)"
    prepared = original("decomposition.prepare_part", {**redraft, "draft_attempt": 2})
    assert label in prepared["draft_request"]["prompt"]
    review = original(
        "decomposition.prepare_part_review",
        {**redraft, "draft_attempt": 2, "draft_sha256": "1" * 64, "draft_file": {"path": "d"}},
    )
    assert label in review["review_request"]["prompt"]


def test_the_pinned_source_survives_an_older_plan_ending_later(tmp_path: Path) -> None:
    """Finding 2: the source selected when the input is staged is pinned; a plan that
    ends meanwhile (and would now be the latest eligible one) does not replace it."""
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    middle = harness.create()

    def decide(payload: dict[str, Any], attempt: int) -> str:
        return "REVISE" if _run_epic(payload) else "APPROVE"

    middle_result = drive(harness, middle["id"], decide=decide)  # still DRAFTING
    second = harness.create()
    seen, original = _capture(harness, "decomposition.prepare_part")

    def step(called: str, payload: dict[str, Any]) -> dict[str, Any]:
        if called == "decomposition.prepare_part" and _run_epic(payload) and not seen:
            _end(harness, middle, middle_result)
        return original(called, payload)

    harness.step = step  # type: ignore[method-assign]
    harness.requests.clear()
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    harness.step = original  # type: ignore[method-assign]
    run = _part(harness, second, 2)
    assert run["continued_from"]["decomposition_id"] == first["id"]
    [draft] = _run_prompts(harness, "claude")
    assert f"decomposition {first['id']}" in draft
    [review] = _run_prompts(harness, "codex")
    assert f"previous run {first['id'][:8]}" in review
    assert f"previous run {middle['id'][:8]}" not in review


@pytest.mark.parametrize("ended_before_deploy", [False, True])
def test_the_final_schema_refusal_is_kept_for_a_continuation(
    tmp_path: Path, ended_before_deploy: bool
) -> None:
    """Finding 3: REVISE on draft 1, schema refusals of drafts 2-4; the last refusal ends
    the part before any next draft records it, so record_terminal records it. A plan that
    ended before that (round 2, finding 1) keeps it only in its terminal blockers, and the
    continuation recovers it from there."""
    harness = PlanHarness(tmp_path)
    created = harness.create()

    def decide(payload: dict[str, Any], attempt: int) -> str:
        return "REVISE" if _run_epic(payload) else "APPROVE"

    original = harness.step

    def step(called: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Stop where the workflow would prepare draft 2 (after the REVISE of draft 1).
        if called == "decomposition.prepare_part" and _run_epic(payload):
            if payload["draft_attempt"] == 2:
                raise _Stop(payload)
        return original(called, payload)

    harness.step = step  # type: ignore[method-assign]
    with pytest.raises(_Stop) as stopped:
        drive(harness, created["id"], decide=decide)
    current = {key: value for key, value in stopped.value.payload.items() if key != "draft_attempt"}
    harness.step = original  # type: ignore[method-assign]
    part_id = current["part_id"]
    budget = {"reviewed": 1, "rejected": 0}
    for attempt in (2, 3, 4):
        refusal = {"code": "DRAFT_SCHEMA_INVALID", "details": [f"draft {attempt - 1} schema"]}
        prepared = original(
            "decomposition.prepare_part",
            {
                **current,
                "draft_attempt": attempt,
                "part_budget": budget,
                **({"rejection": refusal} if attempt > 2 else {}),
            },
        )
        assert prepared["status"] == "DRAFT_READY", prepared
        budget = {**budget, "rejected": budget["rejected"] + (attempt > 2)}
    # The workflow's _rejections_exhausted and _part_terminal for draft 4.
    _end(
        harness,
        created,
        {
            "blocker": "DECOMPOSITION_PART_REJECTIONS_EXHAUSTED",
            "blockers": [
                f"part {part_id}: 3 drafts were refused before review (DRAFT_SCHEMA_INVALID)",
                "draft 4 schema",
            ],
        },
    )
    entry = harness.repository.part_sync(UUID(created["id"]), part_id)
    assert entry is not None
    assert [(item["attempt"], item["code"]) for item in entry["rejections"]] == [
        (2, "DRAFT_SCHEMA_INVALID"),
        (3, "DRAFT_SCHEMA_INVALID"),
        (4, "DRAFT_SCHEMA_INVALID"),
    ]
    assert entry["rejections"][-1]["details"] == ["draft 4 schema"]
    if ended_before_deploy:
        parts = _parts(harness, created["id"])
        parts["parts"][part_id]["rejections"].pop()
        _set_parts(harness, created["id"], parts)
    harness.requests.clear()
    second = harness.create()
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    [draft] = _run_prompts(harness, "claude")
    assert "Hermes rejection DRAFT_SCHEMA_INVALID of draft 4" in draft
    assert draft.count("draft 4 schema") == 1


class _Stop(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("stop")
        self.payload = payload


def test_plans_that_never_drafted_the_part_do_not_hide_an_older_one(tmp_path: Path) -> None:
    """Finding 4: more than REUSE_SEARCH newer ended plans without the part."""
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    fillers = [harness.create() for _ in range(REUSE_SEARCH + 5)]
    second = harness.create()
    with harness.repository.sessions.begin() as session:
        for offset, item in enumerate([first, *fillers, second]):
            values: dict[str, Any] = {"created_at": base + timedelta(minutes=offset)}
            if item in fillers:
                values["status"] = "FAILED"
            session.execute(
                update(BacklogDecomposition)
                .where(BacklogDecomposition.id == UUID(item["id"]))
                .values(**values)
            )
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    assert _part(harness, second, 2)["continued_from"]["decomposition_id"] == first["id"]


def test_continued_from_is_recorded_when_the_continued_draft_is_refused(
    tmp_path: Path,
) -> None:
    """Finding 5: the provenance is pinned when the continuation is selected."""
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    second = harness.create()

    def draft(payload: dict[str, Any], attempt: int) -> Any:
        value = draft_for(payload)
        if _run_epic(payload):
            value["inputs"]["design_sha256"] = "0" * 64  # bound to another design
        return value

    result = drive(harness, second["id"], draft=draft)
    assert result["blocker"] == "DRAFT_BINDING_MISMATCH", result
    assert _part(harness, second, 2)["continued_from"]["decomposition_id"] == first["id"]


# Review findings (Codex round 2, d9b4201) -----------------------------------------------


def test_the_search_is_not_capped_by_a_plan_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2: the part filter applies to every earlier ended plan, so any number of
    newer plans without the part (here 12 pages of 2) never hides the unresolved one."""
    monkeypatch.setattr(decompositions_db, "REUSE_SEARCH", 2)
    # d9b4201 capped the search at CONTINUATION_SEARCH plans; no cap may apply now.
    monkeypatch.setattr(decompositions_db, "CONTINUATION_SEARCH", 10, raising=False)
    harness = PlanHarness(tmp_path)
    first = _unresolved_run(harness)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    fillers = [harness.create() for _ in range(23)]
    second = harness.create()
    with harness.repository.sessions.begin() as session:
        for offset, item in enumerate([first, *fillers, second]):
            values: dict[str, Any] = {"created_at": base + timedelta(minutes=offset)}
            if item in fillers:
                values["status"] = "FAILED"
            session.execute(
                update(BacklogDecomposition)
                .where(BacklogDecomposition.id == UUID(item["id"]))
                .values(**values)
            )
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    assert _part(harness, second, 2)["continued_from"]["decomposition_id"] == first["id"]


def test_the_files_come_from_the_winning_pin(tmp_path: Path) -> None:
    """Finding 3: an overlapping activity attempt pinned an older source first; the
    attempt that selected the newer one stages the winner, matching continued_from."""
    harness = PlanHarness(tmp_path)
    older = _unresolved_run(harness)
    newer = _unresolved_run(harness)
    second = harness.create()
    part_id = next(
        key
        for key, entry in _parts(harness, older["id"])["parts"].items()
        if entry.get("number") == 2
    )
    source = harness.repository.previous_unresolved_sync(UUID(newer["id"]), PROJECT, part_id)
    assert source is not None and source["decomposition_id"] == older["id"]
    repository = harness.repository
    read = repository.pinned_continuation_sync
    calls = {"n": 0}

    def racing(decomposition_id: UUID, wanted: str) -> tuple[bool, dict[str, Any] | None]:
        if str(decomposition_id) == second["id"] and wanted == part_id and calls["n"] == 0:
            calls["n"] += 1
            # Read before the other attempt pinned; that attempt pins `older` now.
            repository.pin_continuation_sync(UUID(second["id"]), part_id, "epic", 2, source)
            return False, None
        return read(decomposition_id, wanted)

    repository.pinned_continuation_sync = racing  # type: ignore[method-assign]
    harness.requests.clear()
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    assert calls["n"] == 1
    assert _part(harness, second, 2)["continued_from"]["decomposition_id"] == older["id"]
    [review] = _run_prompts(harness, "codex")
    assert f"previous run {older['id'][:8]}" in review
    assert f"previous run {newer['id'][:8]}" not in review
