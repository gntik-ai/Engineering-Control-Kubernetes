"""NEW-142 (operator, 2026-10-06): a UI blueprint part continues across plan runs.

Live: batch 2 (b002-026e46fe87ab11f5) blocked in consecutive plans after three drafts
(27 -> 5 -> 3 and 20 -> 9 -> 2 findings); each new plan restarted it from scratch and never
closed the last findings (~260k output tokens per restart). Attempt 1 of a part an earlier
plan drafted and reviewed but never approved now revises that run's last reviewed draft
(staged, hash-verified) against all of that run's findings. Workflow tests run on a real
local Temporal (skipped without the `temporal` CLI) and replay pre-change histories.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from new_123_fixtures import KIT_SHA, SOURCE_SHA, build_fake_pack
from new_131_fixtures import batch_content, frame_content
from pre_new_142_ui_plan_workflow import PreNew142UiMigrationPlanWorkflow
from sqlalchemy import create_engine
from temporalio.worker import Replayer
from test_new_131_ui_batched_workflow import (
    _TEMPORAL_CLI,
    batched_harness,
    markers,
    stage_part_document,
)
from test_new_139_review_convergence import rule_reviewer

from engineering_control.activities import ui_plan
from engineering_control.config import Settings
from engineering_control.db import models  # noqa: F401
from engineering_control.db.base import Base
from engineering_control.db.ui_blueprints import UiBlueprintRepository
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.ui_blueprint_parts import (
    BATCH,
    batch_part_id,
    frame_part_id,
)
from engineering_control.workflows.definitions import UiMigrationPlanWorkflow

temporal = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH (local dev server)"
)
PATCH = UiMigrationPlanWorkflow._CONTINUATION_PATCH


def staging(payload: dict[str, Any]) -> dict[str, Any]:
    """The scan worker's staging, with the real file hash for the continued draft."""
    name = str(payload.get("name") or "")
    if name.endswith("-previous.json"):
        text = json.dumps(payload["document"], indent=2, sort_keys=True) + "\n"
        data = text.encode()
        document = {
            "path": f"/srv/engineering/worktrees/ui-plan-inputs/x/{name}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
        return {"status": "STAGED", "document": document}
    return stage_part_document(payload)


def _prompts(harness: Any, step: str, index: int) -> list[dict[str, Any]]:
    return [p for p in harness.payloads[step] if p["part"]["index"] == index]


@temporal
async def test_a_part_unresolved_in_plan_a_continues_in_plan_b(tmp_path: Path) -> None:
    harness, first, first_id = batched_harness(tmp_path)
    harness.responses["ui_plan.stage_document"] = staging
    harness.responses["ui_plan.review"] = rule_reviewer({2: 3})
    result = await harness.run(UiMigrationPlanWorkflow, first, first_id)
    assert (result["status"], result["error_code"]) == (
        "BLOCKED",
        "BLUEPRINT_PART_REVIEW_UNRESOLVED",
    )
    stuck = _prompts(harness, "ui_plan.draft", 2)[-1]["part_id"]
    harness.calls.clear()
    harness.payloads.clear()
    harness, second, second_id = batched_harness(tmp_path, harness)
    harness.responses["ui_plan.stage_document"] = staging
    harness.responses["ui_plan.review"] = rule_reviewer({})
    result = await harness.run(UiMigrationPlanWorkflow, second, second_id)
    assert result["status"] == "CANDIDATE"
    drafts = _prompts(harness, "ui_plan.draft", 2)
    assert len(drafts) == 1 and drafts[0]["part_id"] == stuck  # part ids are unchanged
    prompt = drafts[0]["draft_request"]["prompt"]
    short = first.blueprint_id[:8]
    assert f"This part continues an earlier plan run (blueprint {first.blueprint_id})" in prompt
    assert f"part-{stuck}-previous.json" in prompt and "do not draft from scratch" in prompt
    for round_ in (1, 2, 3):
        assert f"previous run {short}, round {round_} (review of draft {round_})" in prompt
        assert f"rule R{round_})" in prompt
    staged = [
        p for p in harness.payloads["ui_plan.stage_document"] if "previous" in str(p.get("name"))
    ]
    assert len(staged) == 1
    document = staged[0]["document"]
    assert document["previous_blueprint_id"] == first.blueprint_id
    assert document["previous_attempt"] == 3
    assert document["content_sha256"] == canonical_sha256(document[BATCH])
    review = _prompts(harness, "ui_plan.review", 2)[0]["review_request"]["prompt"]
    assert "First verify each one" in review and f"previous run {short}, round 3" in review
    assert any(f"{PATCH}-batch-2" in marker for marker in markers(harness.history))
    with harness.repository.sessions() as session:
        from engineering_control.db.models import UiBlueprint

        row = session.get(UiBlueprint, UUID(second.blueprint_id))
        assert row is not None
        entry = next(e for e in row.drafts_json if e.get("part") == stuck)
    assert entry["continued_from"] == {
        "blueprint_id": first.blueprint_id,
        "attempt": 3,
        "content_sha256": document["content_sha256"],
    }
    # Other parts were adopted (NEW-136 reuse), never continued.
    assert [p["part"]["index"] for p in harness.payloads["ui_plan.draft"]] == [2, 3, 4, 5, 6, 7]
    await Replayer(workflows=[UiMigrationPlanWorkflow]).replay_workflow(harness.history)


@temporal
async def test_pre_new_142_histories_replay_and_new_ones_need_the_patch(tmp_path: Path) -> None:
    harness, first, first_id = batched_harness(tmp_path)
    harness.responses["ui_plan.stage_document"] = staging
    harness.responses["ui_plan.review"] = rule_reviewer({2: 3})
    assert (await harness.run(UiMigrationPlanWorkflow, first, first_id))["status"] == "BLOCKED"
    # The deployed workflow never staged a continuation, even when the activity offers one;
    # replayed on the new code, the per-part marker is absent (false): unchanged.
    harness, old, old_id = batched_harness(tmp_path, harness)
    harness.responses["ui_plan.stage_document"] = staging
    harness.responses["ui_plan.review"] = rule_reviewer({})
    result = await harness.run(
        PreNew142UiMigrationPlanWorkflow, old, old_id, replay=UiMigrationPlanWorkflow
    )
    assert result["status"] == "CANDIDATE"
    assert not any(PATCH in marker for marker in markers(harness.history))
    # NEW-142 review: a workflow that never stages a continuation is never offered one.
    b002 = _prompts(harness, "ui_plan.draft", 2)[0]["draft_request"]["prompt"]
    assert "continues an earlier plan run" not in b002 and "previous run" not in b002
    assert not any(
        "previous" in str(p.get("name")) for p in harness.payloads["ui_plan.stage_document"]
    )
    await Replayer(workflows=[UiMigrationPlanWorkflow]).replay_workflow(harness.history)
    # A new history that staged the continuation needs the new code.
    (tmp_path / "again").mkdir()
    newer, third, third_id = batched_harness(tmp_path / "again")
    newer.responses["ui_plan.stage_document"] = staging
    newer.responses["ui_plan.review"] = rule_reviewer({2: 3})
    await newer.run(UiMigrationPlanWorkflow, third, third_id)
    newer.calls.clear()
    newer, fourth, fourth_id = batched_harness(tmp_path / "again", newer)
    newer.responses["ui_plan.stage_document"] = staging
    newer.responses["ui_plan.review"] = rule_reviewer({})
    assert (await newer.run(UiMigrationPlanWorkflow, fourth, fourth_id))["status"] == "CANDIDATE"
    assert any(PATCH in marker for marker in markers(newer.history))
    with pytest.raises(Exception):  # noqa: B017 - any nondeterminism error
        await Replayer(workflows=[PreNew142UiMigrationPlanWorkflow]).replay_workflow(newer.history)


def many_findings_reviewer(index: int, last: int) -> Any:
    """REVISE part `index` three times: one finding in rounds 1 and 2, `last` in round 3."""
    rounds = {"n": 0}

    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        from new_131_fixtures import part_review
        from test_new_123_ui_workflows import agent
        from test_new_131_ui_batched_workflow import with_usage

        part = payload["part"]
        decision = "APPROVE"
        count = 0
        if int(part["index"]) == index and rounds["n"] < 3:
            rounds["n"] += 1
            decision = "REVISE"
            count = 1 if rounds["n"] < 3 else last
        output = part_review(part["kind"], payload["part_id"], payload["draft_sha256"], decision)
        if decision == "REVISE":
            output["review"]["findings"] = [
                {
                    "category": "other",
                    "subject": f"page-{n}",
                    "detail": f"Round {rounds['n']} finding {n} of {count}.",
                    "reference": "pages[0]",
                }
                for n in range(count)
            ]
        return with_usage(agent(output), 4_000)(payload)

    return respond


@temporal
async def test_every_prior_finding_is_staged_even_past_the_prompt_bound(tmp_path: Path) -> None:
    # Review: 61 final-review findings; the bounded prompt drops the oldest, so the
    # complete list must be in the staged, hash-bound continuation file.
    harness, first, first_id = batched_harness(tmp_path)
    harness.responses["ui_plan.review"] = many_findings_reviewer(2, 61)
    assert (await harness.run(UiMigrationPlanWorkflow, first, first_id))["status"] == "BLOCKED"
    harness, second, second_id = batched_harness(tmp_path, harness)
    harness.responses["ui_plan.review"] = rule_reviewer({})
    assert (await harness.run(UiMigrationPlanWorkflow, second, second_id))["status"] == (
        "CANDIDATE"
    )
    prompt = _prompts(harness, "ui_plan.draft", 2)[-1]["draft_request"]["prompt"]
    staged = next(
        p for p in harness.payloads["ui_plan.stage_document"] if "previous" in str(p["name"])
    )
    path = f"/srv/engineering/worktrees/ui-plan-inputs/x/{staged['name']}"
    assert "Round 3 finding 0 of 61." not in prompt  # past the bound of 60 (63 in all)
    assert "Round 3 finding 60 of 61." in prompt
    assert "3 older findings are omitted" in prompt
    assert f"the complete list from the previous run is in {path} (field prior_findings)" in (
        prompt
    )
    prior = "\n".join(staged["document"]["prior_findings"])
    assert len(staged["document"]["prior_findings"]) == 63
    for text in ("Round 1 finding 0 of 1.", "Round 2 finding 0 of 1.", "Round 3 finding 0 of 61."):
        assert text in prior
    assert [r["attempt"] for r in staged["document"]["prior_reviews"]] == [1, 2, 3]


@temporal
async def test_a_final_provider_schema_refusal_is_kept_and_continued(tmp_path: Path) -> None:
    # Review: REVISE -> REVISE -> provider SchemaViolation left no durable rejection.
    from test_new_132_structural_redraft import provider_boundary

    harness, first, first_id = batched_harness(tmp_path)
    harness.responses["ui_plan.stage_document"] = staging
    adapter, draft = provider_boundary(tmp_path, harness, prose=0)

    async def drafter(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["part"]["kind"] == "frame" and payload["draft_attempt"] == 3:
            adapter.prose = 1
        result: dict[str, Any] = await draft(payload)
        return result

    harness.responses["ui_plan.draft"] = drafter
    harness.responses["ui_plan.review"] = rule_reviewer({0: 2})
    result = await harness.run(UiMigrationPlanWorkflow, first, first_id)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "DRAFT_SCHEMA_INVALID")
    frame_id = harness.payloads["ui_plan.draft"][0]["part_id"]
    durable = harness.repository.part_rejections_sync(UUID(first.blueprint_id), frame_id)
    assert [(item["attempt"], item["code"]) for item in durable] == [(3, "DRAFT_SCHEMA_INVALID")]
    assert any("frame/gaps/1/blocks/0 [pattern]" in item for item in durable[0]["details"])
    # The next plan continues draft 2 with every finding, the refusal included.
    harness, second, second_id = batched_harness(tmp_path, harness)
    harness.responses["ui_plan.stage_document"] = staging
    harness.responses["ui_plan.review"] = rule_reviewer({})
    assert (await harness.run(UiMigrationPlanWorkflow, second, second_id))["status"] == (
        "CANDIDATE"
    )
    prompt = _prompts(harness, "ui_plan.draft", 0)[-1]["draft_request"]["prompt"]
    short = first.blueprint_id[:8]
    assert f"previous run {short}, round 3 (Hermes rejection DRAFT_SCHEMA_INVALID" in prompt
    assert f"previous run {short}, round 2 (review of draft 2)" in prompt
    staged = next(
        p for p in harness.payloads["ui_plan.stage_document"] if "previous" in str(p["name"])
    )
    assert staged["document"]["previous_attempt"] == 2
    assert staged["document"]["prior_rejections"][0]["attempt"] == 3


# Activities and repository ------------------------------------------------------------------

PART = "b002-" + "a" * 16
OTHER = "b002-" + "b" * 16
PAGES = ["console-page00-page"]


@pytest.fixture
def repository(tmp_path: Path) -> UiBlueprintRepository:
    engine = create_engine(f"sqlite:///{tmp_path / 'ui.sqlite'}")
    Base.metadata.create_all(engine)
    return UiBlueprintRepository(engine=engine)


def _drafting(repository: UiBlueprintRepository) -> UUID:
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    identity = UUID(created["id"])
    repository.register_sync(identity, created["workflow_id"])
    repository.record_inputs_sync(
        identity,
        {"source_sha": SOURCE_SHA, "inventory_sha256": "9" * 64, "kit_snapshot_sha256": KIT_SHA},
    )
    return identity


def _review(decision: str, detail: str) -> dict[str, Any]:
    findings = [{"category": "other", "subject": "s", "detail": detail, "reference": "r"}]
    return {
        "decision": decision,
        "summary": decision,
        "findings": findings if decision == "REVISE" else [],
    }


def _unresolved_run(
    repository: UiBlueprintRepository, content: dict[str, Any], *, approve: bool = False
) -> UUID:
    run = _drafting(repository)
    for attempt in (1, 2):
        digest = repository.record_part_sync(run, PART, BATCH, attempt, content)
        decision = "APPROVE" if approve and attempt == 2 else "REVISE"
        repository.mark_part_reviewed_sync(
            run, PART, attempt, digest, _review(decision, f"finding {attempt}")
        )
        if approve:
            content = copy.deepcopy(content)
    repository.record_part_rejection_sync(run, PART, 3, "DRAFT_SCHEMA_INVALID", ["bad blocks"])
    repository.record_terminal_sync(run, "BLOCKED", "BLUEPRINT_PART_REVIEW_UNRESOLVED", [])
    return run


def _handler(tmp_path: Path, repository: UiBlueprintRepository) -> ui_plan.UiPlanActivityHandler:
    return ui_plan.UiPlanActivityHandler(
        Settings(agent_pack_root=build_fake_pack(tmp_path / "pack", version="1.5.1")),
        repository=repository,
        change_requests=Mock(),
    )


def _context(content: dict[str, Any]) -> dict[str, Any]:
    frame = frame_content(PAGES[0])
    return {"all_page_ids": PAGES, "index": 2, "page_ids": PAGES, "frame": frame}


def test_the_earlier_run_is_found_by_the_same_part_id_only(
    tmp_path: Path, repository: UiBlueprintRepository
) -> None:
    content = batch_content(2, PAGES)
    first = _unresolved_run(repository, content)
    current = _drafting(repository)
    found = repository.previous_unresolved_sync(current, PART)
    assert found is not None and found["blueprint_id"] == str(first)
    assert found["attempt"] == 2 and found["content"] == content
    assert [item["attempt"] for item in found["reviews"]] == [1, 2]
    assert found["rejections"] == [
        {"attempt": 3, "code": "DRAFT_SCHEMA_INVALID", "details": ["bad blocks"]}
    ]
    assert repository.previous_unresolved_sync(current, OTHER) is None  # another part id
    handler = _handler(tmp_path, repository)
    payload = {"inputs": {"input_directory": "/in/x"}}
    continuation, document, skipped = handler._continuation(
        current, BATCH, PART, _context(content), payload
    )
    assert skipped is None and continuation is not None and document is not None
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    assert continuation["file"] == {
        "name": f"part-{PART}-previous.json",
        "path": f"/in/x/part-{PART}-previous.json",
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "bytes": len(text.encode()),
    }


def test_an_approved_part_is_never_continued(
    tmp_path: Path, repository: UiBlueprintRepository
) -> None:
    _unresolved_run(repository, batch_content(2, PAGES), approve=True)
    assert repository.previous_unresolved_sync(_drafting(repository), PART) is None


def test_an_invalid_or_tampered_prior_draft_drafts_from_scratch(
    tmp_path: Path, repository: UiBlueprintRepository
) -> None:
    content = batch_content(2, PAGES)
    first = _unresolved_run(repository, content)
    current = _drafting(repository)
    handler = _handler(tmp_path, repository)
    payload = {"inputs": {"input_directory": "/in/x"}}
    # No longer valid against the current references (the page left this batch).
    context = {**_context(content), "page_ids": ["console-page01-page"]}
    continuation, document, skipped = handler._continuation(current, BATCH, PART, context, payload)
    assert continuation is None and document is None
    assert skipped is not None and skipped["blueprint_id"] == str(first)
    assert "no longer validates" in skipped["reason"]
    # The stored draft no longer matches its recorded hash.
    with repository.sessions.begin() as session:
        from engineering_control.db.models import UiBlueprint

        row = session.get(UiBlueprint, first)
        assert row is not None
        drafts = copy.deepcopy(row.drafts_json)
        for entry in drafts:
            if entry.get("part") == PART and entry.get("part_attempt") == 2:
                entry["content"]["pages"][0]["disposition_reason"] = "tampered"
        row.drafts_json = drafts
    continuation, _document, skipped = handler._continuation(
        current, BATCH, PART, _context(content), payload
    )
    assert continuation is None and skipped is not None
    assert "recorded hashes" in skipped["reason"]


def test_previous_run_findings_are_labelled_and_bounded() -> None:
    def review(attempt: int, count: int) -> dict[str, Any]:
        findings = [{"category": "c", "subject": "s", "detail": f"r{attempt}-{n}"}
                    for n in range(count)]  # fmt: skip
        return {"attempt": attempt, "decision": "REVISE", "findings": findings}

    previous = {
        "blueprint_id": "7d08ffd4-0000-4000-8000-000000000000",
        "reviews": [review(1, 40), review(2, 30)],
        "rejections": [{"attempt": 3, "code": "DRAFT_STRUCTURE_INVALID", "details": ["x"]}],
    }
    lines, omitted = ui_plan.cumulative_findings([], [], 1, previous)
    assert len(lines) == ui_plan.PRIOR_FINDINGS_MAX and omitted == 71 - 60
    assert lines[-1] == (
        "- [previous run 7d08ffd4, round 3 (Hermes rejection DRAFT_STRUCTURE_INVALID of draft 3)] x"
    )
    assert all("previous run 7d08ffd4, round" in line for line in lines)
    # In the new plan's own redrafts, its rounds follow the previous run's.
    own = [{"attempt": 1, "decision": "REVISE", "findings": [{"detail": "now"}]}]
    lines, _omitted = ui_plan.cumulative_findings(own, [], 2, previous)
    assert lines[-1].startswith("- [round 1 (review of draft 1)]")


def test_part_ids_are_unchanged() -> None:
    # Pinned from origin/main ed92f1d (as in NEW-139): continuation changes no id input.
    # NEW-149 (2026-10-07) deliberately changed the operator decisions and seed findings
    # (gntik-ui stable 0.2.0 replaces the canary pin), so part ids of FUTURE plans change;
    # the approved blueprint is final data and its fan-out never recomputes part ids.
    provenance = ui_plan.part_provenance("p" * 64)
    assert provenance == "f913a843f1baf9376bf876720e7aa5024c647bda5cf414075337e5b222c47a20"
    assert frame_part_id("c" * 64, "k" * 64, ["a-page", "b-page"], provenance) == (
        "frame-2f38a18b6a93c05e"
    )
    assert batch_part_id(1, "c" * 64, "k" * 64, ["a-page"], "f" * 64, provenance) == (
        "b001-9b508acf616d7c28"
    )
