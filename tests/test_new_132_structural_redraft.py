"""NEW-132: gap `blocks` holds only IDs, and a draft Hermes rejects gets the part's redraft.

Run 76684f83 blocked after its frame call: three frame gaps put prose in `blocks`, and a
first-attempt draft rejected by Hermes's own schema or structure check was terminal. Workflow
tests run on a real local Temporal and replay old and new batched histories.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime
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

from engineering_control.activities.provider import ProviderActivityHandler
from engineering_control.agents.instructions import InstructionBundle
from engineering_control.config import Settings
from engineering_control.engineering.ui_blueprint import BlueprintRejected, PlanInputs
from engineering_control.engineering.ui_blueprint_parts import FRAME, check_part_draft
from engineering_control.models.core import AgentRequest, AgentResult, ProviderUsage
from engineering_control.models.enums import ModelClass, Project, Provider, RunStatus
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


# Through the real provider boundary (NEW-132 round 1) -------------------------------------


class ProseThenValidClaude:
    """A fake Claude CLI: the first `prose` frame drafts put prose in a gap's blocks."""

    provider = Provider.CLAUDE

    def __init__(self, prose: int) -> None:
        self.prose = prose
        self.calls: list[str] = []

    async def authenticated(self) -> bool:
        return True

    async def execute(self, request: AgentRequest, on_event: Any = None) -> AgentResult:
        part = self.part_of(request)
        output = draft_for(part, part["part_id"], ROUTED)
        if part["kind"] == FRAME and self.prose > 0:
            self.prose -= 1
            output = copy.deepcopy(output)
            output["frame"]["gaps"][1]["blocks"] = [PROSE]
        self.calls.append(part["kind"])
        now = datetime.now(UTC)
        return AgentResult(
            run_id=request.run_id,
            provider=Provider.CLAUDE,
            role=request.role,
            status=RunStatus.SUCCESS,
            summary="drafted",
            structured_result=output,
            usage=ProviderUsage(input_tokens=10_000, output_tokens=30_000, cached_input_tokens=0),
            started_at=now,
            finished_at=now,
        )

    parts: dict[str, dict[str, Any]] = {}

    def part_of(self, request: AgentRequest) -> dict[str, Any]:
        return self.parts[str(request.run_id)]


class PassThroughPack:
    def enrich(self, request: AgentRequest, _step: str) -> tuple[AgentRequest, InstructionBundle]:
        return request, InstructionBundle(
            prompt_prefix="",
            sha256="a" * 64,
            revision=None,
            sources=("skills/plan-ui-migration/SKILL.md",),
            allowed_tools=("Read", "Glob", "Grep"),
        )


def provider_boundary(
    tmp_path: Path, harness: Harness, prose: int
) -> tuple[ProseThenValidClaude, Callable[[dict[str, Any]], Any]]:
    worktree = tmp_path / "worktrees" / "falcone" / "snapshot"
    worktree.mkdir(parents=True)
    adapter = ProseThenValidClaude(prose)
    handler = ProviderActivityHandler(
        Settings(worktree_root=tmp_path / "worktrees", run_root=tmp_path / "runs"),
        "claude",
        adapter,  # type: ignore[arg-type]
        PassThroughPack(),  # type: ignore[arg-type]
    )
    harness.responses["project.snapshot"] = lambda payload: {
        **payload,
        "status": "SNAPSHOT_VERIFIED",
        "source_worktree": str(worktree),
        "source_sha": SOURCE_SHA,
    }

    async def draft(payload: dict[str, Any]) -> dict[str, Any]:
        request = payload["draft_request"]
        adapter.parts[str(request["run_id"])] = {**payload["part"], "part_id": payload["part_id"]}
        return await handler.handle("ui_plan.draft", payload)

    return adapter, draft


async def test_a_provider_schema_refusal_is_redrafted_with_its_findings(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    adapter, draft = provider_boundary(tmp_path, harness, prose=1)
    harness.responses["ui_plan.draft"] = draft
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert result["status"] == "CANDIDATE"
    assert adapter.calls[:2] == [FRAME, FRAME] and len(adapter.calls) == 9
    redraft = frame_drafts(harness)[1]
    assert redraft["draft_attempt"] == 2
    prompt = redraft["draft_request"]["prompt"]
    assert "Hermes rejected your previous draft deterministically (DRAFT_SCHEMA_INVALID)" in prompt
    assert "frame/gaps/1/blocks/0 [pattern]" in prompt
    assert any(PATCH in marker for marker in markers(harness.history))
    view = harness.repository.get_sync(UUID(request.blueprint_id))
    assert view is not None
    # The refused call counts: 9 Claude drafts (one refused) and 8 Codex reviews.
    assert view["usage"]["providers"]["claude"]["calls"] == 9


async def test_a_second_provider_schema_refusal_blocks_with_its_findings(tmp_path: Path) -> None:
    harness, request, workflow_id = batched_harness(tmp_path)
    adapter, draft = provider_boundary(tmp_path, harness, prose=2)
    harness.responses["ui_plan.draft"] = draft
    result = await harness.run(UiMigrationPlanWorkflow, request, workflow_id)
    assert (result["status"], result["error_code"]) == ("BLOCKED", "DRAFT_SCHEMA_INVALID")
    assert adapter.calls == [FRAME, FRAME]
    assert any("frame/gaps/1/blocks/0 [pattern]" in item for item in result["blockers"])
    assert "ui_plan.review" not in harness.calls
    view = harness.repository.get_sync(UUID(request.blueprint_id))
    assert view is not None and view["usage"]["providers"]["claude"]["calls"] == 2


def test_schema_refusal_details_carry_bounded_redacted_findings(tmp_path: Path) -> None:
    from temporalio.exceptions import ApplicationError

    handler = ProviderActivityHandler(
        Settings(worktree_root=tmp_path / "worktrees", run_root=tmp_path / "runs"),
        "claude",
        ProseThenValidClaude(0),  # type: ignore[arg-type]
        PassThroughPack(),  # type: ignore[arg-type]
    )
    names = [f"field{index:02d}" for index in range(30)]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [*names, "token"],
        "properties": {**{name: {"type": "string"} for name in names},
                       "token": {"type": "integer"}},
    }  # fmt: skip
    secret = "ghp_" + "A" * 36
    request = AgentRequest(
        project=Project.FALCONE,
        role="ui-migration-architect",
        provider=Provider.CLAUDE,
        model_class=ModelClass.WORKER,
        prompt="Draft.",
        working_directory=str(tmp_path),
        output_schema=schema,
    )
    now = datetime.now(UTC)
    result = AgentResult(
        run_id=request.run_id,
        provider=Provider.CLAUDE,
        role=request.role,
        status=RunStatus.SUCCESS,
        structured_result={"token": secret + " " + "x" * 600},
        usage=ProviderUsage(output_tokens=5),
        started_at=now,
        finished_at=now,
    )
    with pytest.raises(ApplicationError) as refused:
        handler._validate_result_schema(request, result)
    details = refused.value.details[0]
    assert refused.value.type == "SchemaViolation" and details["usage"]["output_tokens"] == 5
    findings = details["findings"]
    assert len(findings) == 20
    assert all(len(item) <= 420 for item in findings)
    # The model's own text in a message is bounded and redacted.
    only_token = request.model_copy(update={"output_schema": {**schema, "required": ["token"]}})
    with pytest.raises(ApplicationError) as again:
        handler._validate_result_schema(only_token, result)
    (finding,) = again.value.details[0]["findings"]
    assert finding.startswith("token [type]: ") and len(finding) <= 330
    assert secret not in finding


def test_a_secret_straddling_the_finding_cutoff_is_fully_redacted(tmp_path: Path) -> None:
    # NEW-132 review 2: redact the complete message before truncating it.
    from temporalio.exceptions import ApplicationError

    handler = ProviderActivityHandler(
        Settings(worktree_root=tmp_path / "worktrees", run_root=tmp_path / "runs"),
        "claude",
        ProseThenValidClaude(0),  # type: ignore[arg-type]
        PassThroughPack(),  # type: ignore[arg-type]
    )
    schema = {"type": "object", "additionalProperties": False, "properties": {}}
    secret = "ghp_" + "B" * 36
    # The unexpected property name puts the token across the 300-character cutoff.
    name = "p" * 213 + "-" + secret  # the token starts at offset 285 of the finding
    request = AgentRequest(
        project=Project.FALCONE,
        role="ui-migration-architect",
        provider=Provider.CLAUDE,
        model_class=ModelClass.WORKER,
        prompt="Draft.",
        working_directory=str(tmp_path),
        output_schema=schema,
    )
    now = datetime.now(UTC)
    result = AgentResult(
        run_id=request.run_id,
        provider=Provider.CLAUDE,
        role=request.role,
        status=RunStatus.SUCCESS,
        structured_result={name: 1},
        usage=ProviderUsage(output_tokens=1),
        started_at=now,
        finished_at=now,
    )
    with pytest.raises(ApplicationError) as refused:
        handler._validate_result_schema(request, result)
    findings = refused.value.details[0]["findings"]
    assert findings and all(len(item) <= 300 for item in findings)
    assert not any("ghp_" in item or "BBBBBBBBBB" in item for item in findings)
