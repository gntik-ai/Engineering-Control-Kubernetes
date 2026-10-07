"""NEW-139: UI blueprint part reviews converge.

Live plan ee4f7a59: batch 2 blocked after three drafts and three reviews, each review
finding different violations of the reviewed frame's numbered global decisions; the
drafter fixed what it was told and the next review sampled other rules. Every review is now
an exhaustive checklist pass, and every redraft and re-review carries all earlier findings
on the part, labelled by round. Prompt text only: part ids and provenance are unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from new_131_fixtures import part_review
from test_new_123_ui_workflows import agent
from test_new_131_ui_batched_workflow import (
    _TEMPORAL_CLI,
    batched_harness,
    with_usage,
)
from test_new_132_structural_redraft import PROSE, frame_drafter

from engineering_control.activities import ui_plan
from engineering_control.engineering.ui_blueprint_parts import (
    BATCH,
    FRAME,
    batch_part_id,
    frame_part_id,
)
from engineering_control.workflows.definitions import UiMigrationPlanWorkflow

temporal = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH (local dev server)"
)


def rule_reviewer(revise: dict[int, int]) -> Any:
    """REVISE the first n reviews of part index i, each round citing a different rule."""
    remaining = dict(revise)
    seen: dict[int, int] = {}

    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        part = payload["part"]
        index = int(part["index"])
        decision = "APPROVE"
        output = part_review(part["kind"], payload["part_id"], payload["draft_sha256"])
        if remaining.get(index, 0) > 0:
            remaining[index] -= 1
            seen[index] = seen.get(index, 0) + 1
            decision = "REVISE"
            output = part_review(
                part["kind"], payload["part_id"], payload["draft_sha256"], decision
            )
            output["review"]["findings"] = [
                {
                    "category": "invented_kit_item",
                    "subject": f"page-of-part-{index}",
                    "detail": f"Global decision {seen[index]} is violated (rule R{seen[index]}).",
                    "reference": "pages[0].target",
                }
            ]
        return with_usage(agent(output), 4_000)(payload)

    return respond


def _prompts(harness: Any, step: str, kind: str, index: int) -> list[str]:
    key = "draft_request" if step == "ui_plan.draft" else "review_request"
    return [
        payload[key]["prompt"]
        for payload in harness.payloads[step]
        if payload["part"]["kind"] == kind and payload["part"]["index"] == index
    ]


@temporal
async def test_the_third_draft_and_reviews_carry_every_earlier_round(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.review"] = rule_reviewer({2: 2})
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    drafts = _prompts(harness, "ui_plan.draft", BATCH, 2)
    assert len(drafts) == 3
    third = drafts[2]
    assert "Global decision 1 is violated (rule R1)" in third
    assert "Global decision 2 is violated (rule R2)" in third
    assert "[round 1 (review of draft 1)]" in third and "[round 2 (review of draft 2)]" in third
    assert "do not regress any earlier fix" in third
    second = drafts[1]
    assert "rule R1" in second and "rule R2" not in second
    reviews = _prompts(harness, "ui_plan.review", BATCH, 2)
    assert len(reviews) == 3
    for prompt in reviews:
        assert "Review exhaustively, never by sampling" in prompt
        assert "page x decision" in prompt
    assert "--- BEGIN PRIOR FINDINGS ---" not in reviews[0]
    assert "rule R1" in reviews[1] and "rule R2" not in reviews[1]
    assert "First verify each one against this draft" in reviews[2]
    assert "rule R1" in reviews[2] and "rule R2" in reviews[2]
    frame_review = _prompts(harness, "ui_plan.review", FRAME, 0)[0]
    assert "check every foundation epic, every gap" in frame_review


@temporal
async def test_a_hermes_rejection_stays_in_the_findings_after_a_revise(tmp_path: Path) -> None:
    # Draft 1 refused by Hermes (schema), draft 2 revised, draft 3 must see both.
    harness, request, workflow_id = batched_harness(tmp_path)
    harness.responses["ui_plan.draft"] = frame_drafter([PROSE])
    harness.responses["ui_plan.review"] = rule_reviewer({0: 1})
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    third = _prompts(harness, "ui_plan.draft", FRAME, 0)[2]
    assert "round 1 (Hermes rejection DRAFT_SCHEMA_INVALID of draft 1)" in third
    assert "[round 2 (review of draft 2)]" in third and "rule R1" in third
    re_review = _prompts(harness, "ui_plan.review", FRAME, 0)[0]  # of draft 2
    assert "Hermes rejection DRAFT_SCHEMA_INVALID of draft 1" in re_review


def test_findings_are_deduplicated_labelled_and_bounded() -> None:
    def review(attempt: int, details: list[str]) -> dict[str, Any]:
        findings = [{"category": "c", "subject": "s", "detail": item} for item in details]
        return {"attempt": attempt, "decision": "REVISE", "findings": findings}

    lines, omitted = ui_plan.cumulative_findings(
        [review(1, ["same", "one"]), review(2, ["same", "two"]), review(3, ["later"])],
        [{"attempt": 1, "code": "DRAFT_STRUCTURE_INVALID", "details": ["gap blocks x"]}],
        before=3,
    )
    assert omitted == 0
    assert "- [round 1 (review of draft 1); round 2 (review of draft 2)] [c] s: same" in lines
    assert lines[0].endswith("s: one")  # ordered by latest occurrence
    assert any("Hermes rejection DRAFT_STRUCTURE_INVALID of draft 1" in line for line in lines)
    assert not any("later" in line for line in lines)  # draft 3 is the one being written
    many = [review(1, [f"old {n}" for n in range(50)]), review(2, [f"new {n}" for n in range(30)])]
    lines, omitted = ui_plan.cumulative_findings(many, [], before=3)
    assert len(lines) == ui_plan.PRIOR_FINDINGS_MAX and omitted == 20
    assert all(f"new {n}" in "\n".join(lines) for n in range(30))  # newest rounds kept whole
    assert "old 19" not in "\n".join(lines) and "old 20" in "\n".join(lines)
    large = [review(1, ["x" * 1_900 + str(n) for n in range(40)])]
    lines, omitted = ui_plan.cumulative_findings(large, [], before=2)
    assert sum(len(line) + 1 for line in lines) <= ui_plan.PRIOR_FINDINGS_CHARS and omitted > 0
    block = ui_plan._findings_block("review findings", lines, omitted)
    assert f"({omitted} older findings are omitted past the bound" in block


def test_a_finding_repeated_in_the_latest_round_survives_the_bound() -> None:
    # NEW-139 review: 60 findings in round 1, 31 in round 2 of which one repeats a round-1
    # finding (still unresolved). Ordered by latest occurrence, it is kept with round 2.
    def review(attempt: int, details: list[str]) -> dict[str, Any]:
        findings = [{"category": "c", "subject": "s", "detail": item} for item in details]
        return {"attempt": attempt, "decision": "REVISE", "findings": findings}

    first = [f"old {n}" for n in range(60)]
    second = [f"new {n}" for n in range(30)] + ["old 0"]
    lines, omitted = ui_plan.cumulative_findings(
        [review(1, first), review(2, second)], [], before=3
    )
    text = "\n".join(lines)
    assert len(lines) == ui_plan.PRIOR_FINDINGS_MAX and omitted == 30
    assert "- [round 1 (review of draft 1); round 2 (review of draft 2)] [c] s: old 0" in lines
    assert all(f"[c] s: new {n}\n" in text + "\n" for n in range(30))
    assert lines[-1].endswith("old 0")  # last raised most recently
    # The 30 findings last raised longest ago (round 1 only, old 1..old 30) are omitted.
    assert "[c] s: old 30\n" not in text + "\n" and "[c] s: old 31\n" in text + "\n"


def test_part_ids_and_provenance_are_unchanged() -> None:
    # Pinned from origin/main 62400f4: NEW-139 changes prompt text only.
    # NEW-149 (2026-10-07) deliberately changed the decisions and seed findings (gntik-ui
    # 0.2.0 replaces the canary pin): new values below; the approved blueprint is final data.
    assert ui_plan.decisions_sha256() == (
        "d7b472a4c8c34c868888d7a2835eebe3f44d4cec2dbf437e69df42514f7d86da"
    )
    provenance = ui_plan.part_provenance("p" * 64)
    assert provenance == "f913a843f1baf9376bf876720e7aa5024c647bda5cf414075337e5b222c47a20"
    assert frame_part_id("c" * 64, "k" * 64, ["a-page", "b-page"], provenance) == (
        "frame-2f38a18b6a93c05e"
    )
    assert batch_part_id(1, "c" * 64, "k" * 64, ["a-page"], "f" * 64, provenance) == (
        "b001-9b508acf616d7c28"
    )
