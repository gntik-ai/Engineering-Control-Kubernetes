"""NEW-154: operator-driven consolidation of duplicate UI blueprint gap change requests.

A bounded Claude step proposes clusters (pinned schema), Hermes checks the plan and stores
it with its sha256; the operator applies it by that sha in one audited transaction (members
MERGED with merged_into, one canonical request per cluster, every blocked_by re-pointed),
and the fan-out drafts the canonical requests (Claude) and reviews them (Codex). Workflow
tests run on a real local Temporal (skipped without the `temporal` CLI) and replay every
history.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from new_123_fixtures import (
    INVENTORY_SHA,
    KIT_SHA,
    KIT_VERSION,
    SOURCE_SHA,
    blueprint,
    gap_output,
)
from sqlalchemy import create_engine, select, update
from temporalio.worker import Replayer
from test_new_123_ui_surfaces import CLI, TOKEN
from test_new_123_ui_workflows import (
    _TEMPORAL_CLI,
    Harness,
    dedupe,
    item_reviewer,
    snapshot,
)

from engineering_control.agents.instructions import AgentInstructionPack
from engineering_control.api.main import create_app
from engineering_control.api.schemas import OperationAccepted
from engineering_control.config import Settings
from engineering_control.db import models
from engineering_control.db.base import Base
from engineering_control.db.change_requests import ChangeRequestRepository
from engineering_control.db.models import ChangeRequest, DashboardEvent, UiBlueprint
from engineering_control.db.ui_blueprints import UiBlueprintRepository, UiFanoutActiveError
from engineering_control.db.ui_gap_consolidations import (
    UiGapConsolidationNotFoundError,
    UiGapConsolidationRepository,
)
from engineering_control.engineering.change_requests import (
    ChangeReview,
    ChangeReviewRecord,
    approval_phrase,
    validate_proposal,
)
from engineering_control.engineering.ui_blueprint import (
    APPROVAL_PHRASE,
    fanout_items,
    item_change_request_id,
    render_item_proposal,
    ui_fanout_workflow_id,
)
from engineering_control.engineering.ui_gap_consolidation import (
    CONSOLIDATE_PHRASE,
    ConsolidationRejected,
    canonical_key,
    check_plan,
    plan_schema,
    plan_sha256,
    prompt_data,
)
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider
from engineering_control.workflows.definitions import (
    ControlledWorkflow,
    UiBlueprintFanOutWorkflow,
    UiGapConsolidationPlanWorkflow,
)
from engineering_control.workflows.inputs import (
    UiBlueprintFanOutInput,
    UiGapConsolidationPlanInput,
)
from engineering_control.workflows.worker_registry import ROLE_TASK_QUEUES

temporal = pytest.mark.skipif(
    _TEMPORAL_CLI is None, reason="requires the `temporal` CLI on PATH (local dev server)"
)
OPERATOR = "telegram:42"
FALCONE = "gntik-ai/falcone"
KIT = "gntik-ai/gntik-ui"
B1, B2 = "gap:gap-b001-cursor-pagination", "gap:gap-b002-cursor-pagination"
W1, W2 = "gap:gap-b001-wizard-overlay", "gap:gap-b002-modal-wizard"
PLANS, USAGE, TABLE = "gap:gap-plans-api", "gap:gap-usage-api", "gap:gap-datatable-pagination"
EPIC = "epic:u0-console-api"


def document() -> dict[str, Any]:
    """The fixture blueprint plus per-batch duplicates: cursor pagination (backend, twice)
    and a create wizard (kit, twice); an epic and both pages link them."""
    value = copy.deepcopy(blueprint())

    def gap(identity: str, kind: str, repository: str, page: str, summary: str) -> dict[str, Any]:
        return {
            "id": identity,
            "type": kind,
            "target_repository": repository,
            "summary": summary,
            "evidence": [f"evidence for {identity}"],
            "blocks": [page],
            "interim": f"interim for {identity}",
            "seed_ref": None,
        }

    value["gaps"] += [
        gap(B1[4:], "BACKEND_GAP", FALCONE, "tenants", "Tenant lists need cursor pagination."),
        gap(B2[4:], "BACKEND_GAP", FALCONE, "workspaces", "Workspace lists need a cursor."),
        gap(W1[4:], "KIT_GAP", KIT, "tenants", "The wizard has no overlay mode."),
        gap(W2[4:], "KIT_GAP", KIT, "workspaces", "There is no modal wizard."),
    ]
    pages = {page["page_id"]: page for page in value["pages"]}
    pages["tenants"]["gap_ids"] += [B1[4:], W1[4:]]
    pages["workspaces"]["gap_ids"] += [B2[4:], W2[4:]]
    value["foundation_epics"][1]["gap_ids"] = [B2[4:]]
    return value


def approved(harness: Harness, value: dict[str, Any] | None = None) -> tuple[str, str]:
    repository = harness.repository
    created = repository.create_sync("falcone", "cli:engineering-ctl", None)
    blueprint_id = UUID(created["id"])
    repository.register_sync(blueprint_id, created["workflow_id"])
    repository.record_inputs_sync(
        blueprint_id,
        {
            "source_sha": SOURCE_SHA,
            "inventory_sha256": INVENTORY_SHA,
            "kit_snapshot_sha256": KIT_SHA,
            "gntik_ui_version": KIT_VERSION,
        },
    )
    digest = repository.record_draft_sync(blueprint_id, 1, value or document(), [])
    repository.mark_reviewed_sync(blueprint_id, 1, digest)
    persisted = repository.persist_candidate_sync(blueprint_id, 1, digest, [], {})
    repository.approve_sync(blueprint_id, persisted["approval_binding"], APPROVAL_PHRASE, OPERATOR)
    return str(blueprint_id), digest


def gap_change_request(key: str, repository: str) -> dict[str, Any]:
    finding = "KIT_GAP" if repository == KIT else "BACKEND_GAP"
    proposal = dict(gap_output(key, finding, repository)["change_request"])
    proposal["what_changes"] = [f"Add {key.removeprefix('gap:')}."]
    proposal["acceptance_criteria"] = [f"{key.removeprefix('gap:')} passes its contract test."]
    return proposal


def gap_drafter(payload: dict[str, Any]) -> dict[str, Any]:
    repository = payload["target_repository"]
    finding = "KIT_GAP" if repository == KIT else "BACKEND_GAP"
    output = gap_output(payload["item_key"], finding, repository)
    output["change_request"] = gap_change_request(payload["item_key"], repository)
    return respond(output, "draft_request")(payload)


def respond(structured: Any, key: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def answer(payload: dict[str, Any]) -> dict[str, Any]:
        request = payload[key]
        now = datetime.now(UTC).isoformat()
        value = structured(payload) if callable(structured) else structured
        result = {
            "run_id": request["run_id"],
            "provider": request["provider"],
            "role": request["role"],
            "status": "SUCCESS",
            "structured_result": value,
            "started_at": now,
            "finished_at": now,
        }
        return {**payload, "status": "SUCCESS", "agent_result": result}

    return answer


REVIEW = ChangeReviewRecord.from_rounds(
    [ChangeReview(decision="APPROVE", summary="Matches the item.", findings=())]
)


def fanned_out(
    harness: Harness,
    blueprint_id: str,
    digest: str,
    value: dict[str, Any] | None = None,
    proposals: dict[str, dict[str, Any]] | None = None,
) -> dict[str, UUID]:
    """Every item drafted to CANDIDATE through the real prepare_item and persist steps
    (no Temporal): the state the live fan-out left behind."""
    writer = ui_fanout_workflow_id(blueprint_id, 1)
    harness.claim(blueprint_id, writer)  # type: ignore[attr-defined]
    ids: dict[str, UUID] = {}
    for item in fanout_items(value or document()):
        prepared = harness.ui._prepare_item(
            {
                "blueprint_id": blueprint_id,
                "item_key": item.key,
                "source_worktree": "/srv/x",
                "orchestration": {"workflow_id": writer},
            }
        )
        assert prepared["status"] in {"DRAFT_READY", "PROPOSAL_READY"}, prepared
        proposal = (
            (proposals or {}).get(item.key) or gap_change_request(item.key, item.target_repository)
            if item.is_gap
            else render_item_proposal(item)
        )
        persisted = harness.change_requests.persist_candidate_sync(
            item_change_request_id(digest, item.key),
            1,
            validate_proposal(proposal, "falcone", item.target_repository),
            REVIEW,
            {},
            fanout_writer=writer,
        )
        assert persisted["status"] == "CANDIDATE", (item.key, persisted)
        ids[item.key] = item_change_request_id(digest, item.key)
    harness.repository.release_fanout_sync(UUID(blueprint_id), writer)
    return ids


def publish(harness: Harness, change_request_id: UUID, number: int) -> None:
    with harness.change_requests.sessions.begin() as session:
        session.execute(
            update(ChangeRequest)
            .where(ChangeRequest.id == change_request_id)
            .values(
                status="PUBLISHED",
                issue_url=f"https://github.com/gntik-ai/falcone/issues/{number}",
                issue_number=number,
            )
        )


def plan_output(
    input_sha256: str, clusters: list[tuple[str, list[str]]], unclustered: list[str]
) -> dict[str, Any]:
    return {
        "input_sha256": input_sha256,
        "clusters": [
            {
                "title": title,
                "member_keys": members,
                "rationale": f"{title}: the same capability drafted once per page batch.",
            }
            for title, members in clusters
        ],
        "unclustered_keys": unclustered,
        "notes": [],
    }


def proposed(
    consolidations: UiGapConsolidationRepository,
    blueprint_id: str,
    clusters: list[tuple[str, list[str]]],
    *,
    kind: str | None = None,
) -> dict[str, Any]:
    """A PROPOSED plan through the real repository steps (the model output scripted)."""
    requested = consolidations.request_plan_sync(UUID(blueprint_id), OPERATOR, kind, False)
    assert requested["action"] == "START"
    identity = UUID(requested["consolidation"]["id"])
    loaded = consolidations.load_sync(identity, requested["consolidation"]["workflow_id"])
    assert loaded["status"] == "READY"
    recorded = consolidations.record_plan_sync(
        identity,
        plan_output(loaded["input_sha256"], clusters, []),
        {"provider": "claude", "run_id": "fixture"},
    )
    assert recorded["status"] == "PROPOSED", recorded
    view = consolidations.get_sync(identity)
    assert view is not None
    return view


def setup(tmp_path: Path) -> tuple[Harness, UiGapConsolidationRepository, str, str, dict]:
    harness = Harness(tmp_path)
    blueprint_id, digest = approved(harness)
    ids = fanned_out(harness, blueprint_id, digest)
    return harness, UiGapConsolidationRepository(engine=harness.engine), blueprint_id, digest, ids


def status(harness: Harness, change_request_id: UUID) -> str:
    view = harness.change_requests.get_sync(change_request_id)
    assert view is not None
    return str(view["status"])


# Plan validation (pure) ----------


def candidates() -> list[dict[str, Any]]:
    def one(key: str, repository: str, kind: str) -> dict[str, Any]:
        return {
            "change_request_id": str(UUID(int=len(key))),
            "item_key": key,
            "item_kind": kind,
            "target_repository": repository,
            "wave": "u1",
            "status": "CANDIDATE",
            "revision": 1,
            "summary": f"summary of {key}",
            "title": f"Close {key}",
            "why": "Needed.",
            "what_changes": ["Add it."],
            "acceptance_criteria": ["It works."],
            "linked": ["page:tenants"],
        }

    return [
        one(B1, FALCONE, "backend-gap"),
        one(B2, FALCONE, "backend-gap"),
        one(PLANS, FALCONE, "backend-gap"),
        one(W1, KIT, "kit-gap"),
        one(W2, KIT, "kit-gap"),
    ]


KNOWN = {B1: "CANDIDATE", B2: "CANDIDATE", PLANS: "CANDIDATE", W1: "CANDIDATE",
         W2: "CANDIDATE", USAGE: "PUBLISHED", TABLE: "REJECTED"}  # fmt: skip
SHA = "a" * 64


def test_a_valid_plan_is_normalized_with_standalone_gaps_unclustered() -> None:
    checked = check_plan(
        plan_output(SHA, [("Wizard", [W2, W1]), ("Cursor pagination", [B2, B1])], [PLANS]),
        input_sha256=SHA,
        candidates=candidates(),
        known=KNOWN,
    )
    # Clusters and members follow blueprint order; unmentioned keys stay unclustered.
    assert [cluster["member_keys"] for cluster in checked["clusters"]] == [[B1, B2], [W1, W2]]
    assert checked["unclustered"] == [PLANS]
    assert canonical_key([B2, B1]) == canonical_key([B1, B2])
    assert canonical_key([B1, B2]).startswith("gap:merged-")


@pytest.mark.parametrize(
    ("clusters", "unclustered", "problem"),
    [
        ([("Pagination", [B1, "gap:gap-b009-unknown"])], [], "is not a gap request of this plan"),
        ([("Pagination", [B1, USAGE])], [], "is not eligible (status PUBLISHED)"),
        ([("Pagination", [B1, TABLE])], [], "is not eligible (status REJECTED)"),
        ([("Pagination", [B1, B2]), ("Again", [B2, PLANS])], [], "is already in cluster 1"),
        ([("Pagination", [B1, B1])], [], "is already in cluster 1"),
        ([("Pagination", [B1, B2])], [B1], "is already in cluster 1"),
        ([("Mixed", [B1, W1])], [], "mixes repositories gntik-ai/falcone, gntik-ai/gntik-ui"),
    ],
)
def test_the_deterministic_check_refuses_bad_plans(
    clusters: list[tuple[str, list[str]]], unclustered: list[str], problem: str
) -> None:
    with pytest.raises(ConsolidationRejected) as refused:
        check_plan(
            plan_output(SHA, clusters, unclustered),
            input_sha256=SHA,
            candidates=candidates(),
            known=KNOWN,
        )
    assert refused.value.code == "UI_GAP_PLAN_REJECTED"
    assert any(problem in detail for detail in refused.value.details), refused.value.details


def test_schema_and_input_binding_are_checked_first() -> None:
    with pytest.raises(ConsolidationRejected) as refused:
        check_plan({"clusters": []}, input_sha256=SHA, candidates=candidates(), known=KNOWN)
    assert refused.value.code == "UI_GAP_PLAN_SCHEMA_INVALID"
    with pytest.raises(ConsolidationRejected) as refused:
        check_plan(
            plan_output("b" * 64, [], []), input_sha256=SHA, candidates=candidates(), known=KNOWN
        )
    assert refused.value.code == "UI_GAP_PLAN_INPUT_MISMATCH"
    # A one-member "cluster" is a schema violation: a lone gap stays unclustered.
    with pytest.raises(ConsolidationRejected) as refused:
        check_plan(
            plan_output(SHA, [("Lone", [B1])], []),
            input_sha256=SHA,
            candidates=candidates(),
            known=KNOWN,
        )
    assert refused.value.code == "UI_GAP_PLAN_SCHEMA_INVALID"


def test_plan_data_is_bounded_deterministically() -> None:
    many = [
        {**candidates()[0], "item_key": f"gap:gap-b{index:03d}-x", "why": "w" * 5000,
         "what_changes": ["c" * 600] * 15, "acceptance_criteria": ["a" * 600] * 15}
        for index in range(30)
    ]  # fmt: skip
    text = prompt_data(many)
    assert len(text) <= 120_000 and text == prompt_data(many)
    with pytest.raises(ConsolidationRejected) as refused:
        prompt_data(many * 20)
    assert refused.value.code == "UI_GAP_PLAN_INPUT_TOO_LARGE"


# Plan records ----------


def test_preview_reuses_the_stored_plan_and_supersedes_it_for_new_inputs(tmp_path: Path) -> None:
    harness, consolidations, blueprint_id, _digest, ids = setup(tmp_path)
    view = proposed(consolidations, blueprint_id, [("Cursor pagination", [B1, B2])])
    assert view["status"] == "PROPOSED" and view["plan_sha12"] == view["plan_sha256"][:12]
    assert view["plan_sha256"] == plan_sha256(view["plan"])
    assert view["apply_command"] == (
        f"engineering-ctl ui-blueprint-gap-consolidate {blueprint_id} --apply "
        f"{view['plan_sha12']} {CONSOLIDATE_PHRASE}"
    )
    assert "Cursor pagination" in view["preview"][0] and B1 in view["preview"][0]
    # Every eligible gap of every kind was asked about; published or rejected ones never.
    asked = {item["item_key"] for item in harness_inputs(consolidations, view["id"])}
    assert asked == {B1, B2, W1, W2, PLANS, USAGE, TABLE}
    again = consolidations.request_plan_sync(UUID(blueprint_id), OPERATOR, None, False)
    assert (again["action"], again["consolidation"]["id"]) == ("REUSE", view["id"])
    # A gap that changes state changes the inputs: a new plan replaces the stored one.
    harness.change_requests.reject_sync(ids[TABLE], "Not needed any more.", OPERATOR)
    fresh = consolidations.request_plan_sync(UUID(blueprint_id), OPERATOR, None, False)
    assert fresh["action"] == "START" and fresh["consolidation"]["id"] != view["id"]
    stale = consolidations.get_sync(UUID(view["id"]))
    assert stale is not None and stale["status"] == "SUPERSEDED"
    asked = {
        item["item_key"] for item in harness_inputs(consolidations, fresh["consolidation"]["id"])
    }
    assert TABLE not in asked
    with pytest.raises(ValueError, match="allowlisted operator"):
        consolidations.request_plan_sync(UUID(blueprint_id), "cli:somebody", None, False)


def harness_inputs(consolidations: UiGapConsolidationRepository, identity: str) -> list[Any]:
    return list(consolidations.plan_inputs_sync(UUID(identity))["candidates"])


def test_an_invalid_plan_is_kept_with_its_problems_and_cannot_be_applied(tmp_path: Path) -> None:
    _harness, consolidations, blueprint_id, _digest, _ids = setup(tmp_path)
    requested = consolidations.request_plan_sync(UUID(blueprint_id), OPERATOR, None, False)
    identity = UUID(requested["consolidation"]["id"])
    loaded = consolidations.load_sync(identity, requested["consolidation"]["workflow_id"])
    recorded = consolidations.record_plan_sync(
        identity, plan_output(loaded["input_sha256"], [("Mixed", [B1, W1])], []), {}
    )
    assert recorded["status"] == "INVALID"
    assert any("mixes repositories" in problem for problem in recorded["problems"])
    view = consolidations.get_sync(identity)
    assert view is not None and view["apply_command"] is None and view["plan_sha256"] is None


# Apply ----------


def test_apply_merges_members_creates_canonicals_and_repoints_blockers(tmp_path: Path) -> None:
    harness, consolidations, blueprint_id, digest, ids = setup(tmp_path)
    workspaces = ids["page:workspaces"]
    before = harness.change_requests.get_sync(workspaces)
    assert before is not None
    # One page is approved (unpublished), the epic is published.
    harness.change_requests.approve_sync(
        workspaces,
        before["approval_binding"],
        approval_phrase("falcone", before["approval_binding"]),
        OPERATOR,
    )
    publish(harness, ids[EPIC], 101)
    epic_before = harness.change_requests.get_sync(ids[EPIC])
    assert epic_before is not None
    view = proposed(
        consolidations,
        blueprint_id,
        [("Cursor pagination", [B1, B2]), ("Create wizard", [W1, W2])],
    )
    applied = consolidations.apply_sync(
        UUID(blueprint_id), view["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR
    )
    assert (applied["status"], applied["replayed"]) == ("APPLIED", False)
    result = applied["result"]
    pagination = item_change_request_id(digest, canonical_key([B1, B2]))
    wizard = item_change_request_id(digest, canonical_key([W1, W2]))
    assert [item["canonical_id"] for item in result["canonicals"]] == [str(pagination), str(wizard)]
    assert sorted(result["merged"]) == sorted(str(ids[key]) for key in (B1, B2, W1, W2))
    # Members: MERGED (terminal, never deleted) with merged_into.
    for key, canonical in ((B1, pagination), (B2, pagination), (W1, wizard), (W2, wizard)):
        member = harness.change_requests.get_sync(ids[key])
        assert member is not None
        assert (member["status"], member["merged_into_id"]) == ("MERGED", str(canonical))
        assert member["merged_into"]["change_request_id"] == str(canonical)
        assert member["consolidation"]["merged"]["plan_sha256"] == view["plan_sha256"]
        assert member["proposal"]["what_changes"]  # the drafted content is kept
    # Canonicals: DRAFTING revision 1 (the next fan-out drafts them), bound to their cluster.
    canonical = harness.change_requests.get_sync(pagination)
    assert canonical is not None
    assert (canonical["status"], canonical["revision"]) == ("DRAFTING", 1)
    assert canonical["target_repository"] == FALCONE and canonical["requested_by"] == OPERATOR
    context = canonical["ui_context"]
    assert context["item_key"] == canonical_key([B1, B2])
    # The earliest wave of any member (B2 blocks a u0 epic).
    assert context["item_kind"] == "backend-gap" and context["wave"] == "u0"
    assert [entry["item_key"] for entry in context["merged_from"]] == [B1, B2]
    assert set(context["needed_by"]) == {"page:tenants", "page:workspaces", EPIC}
    continued = {section["field"]: section["entries"] for section in context["continued"]}
    assert continued["acceptance_criteria"] == [
        f"[{B1}] gap-b001-cursor-pagination passes its contract test.",
        f"[{B2}] gap-b002-cursor-pagination passes its contract test.",
    ]
    assert continued["what_changes"] == [
        f"[{B1}] Add gap-b001-cursor-pagination.",
        f"[{B2}] Add gap-b002-cursor-pagination.",
    ]
    wizard_view = harness.change_requests.get_sync(wizard)
    assert wizard_view is not None and wizard_view["target_repository"] == KIT
    # A CANDIDATE page is re-pointed and rendered again (new binding, still approvable).
    tenants = harness.change_requests.get_sync(ids["page:tenants"])
    assert tenants is not None and tenants["status"] == "CANDIDATE"
    blocked = [entry["item_key"] for entry in tenants["ui_context"]["blocked_by"]]
    assert blocked == [PLANS, canonical_key([B1, B2]), "epic:u0-scaffold"]
    assert f"change request `{pagination}`" in tenants["issue_body"]
    assert str(ids[B1]) not in tenants["issue_body"]
    harness.change_requests.approve_sync(
        ids["page:tenants"],
        tenants["approval_binding"],
        approval_phrase("falcone", tenants["approval_binding"]),
        OPERATOR,
    )
    # An APPROVED (unpublished) page goes back to CANDIDATE for a fresh approval.
    after = harness.change_requests.get_sync(workspaces)
    assert after is not None
    assert after["status"] == "CANDIDATE" and after["approval_snapshot"] is None
    assert after["approval_binding"] != before["approval_binding"]
    assert after["decision_sequence"] == before["decision_sequence"] + 2
    assert str(workspaces) in result["reopened"]
    # A PUBLISHED epic keeps its bound body and context; the merged reference is recorded and
    # blocker resolution maps the merged id to its canonical request.
    epic = harness.change_requests.get_sync(ids[EPIC])
    assert epic is not None
    assert epic["issue_body"] == epic_before["issue_body"]
    assert epic["ui_context"] == epic_before["ui_context"]
    reference = epic["consolidation"]["merged_references"][0]
    assert reference["entries"][0]["from"] == str(ids[B2])
    assert reference["entries"][0]["to"] == str(pagination)
    resolved = {entry["item_key"]: entry for entry in epic["blocked_by_resolved"]}
    assert resolved[B2]["merged"] is True
    assert resolved[B2]["resolved_id"] == str(pagination)
    assert resolved[B2]["resolved_item_key"] == canonical_key([B1, B2])
    assert resolved["epic:u0-scaffold"]["merged"] is False
    assert [entry["change_request_id"] for entry in result["merged_references"]] == [str(ids[EPIC])]
    # Audited.
    with harness.repository.sessions() as session:
        events = session.scalars(
            select(DashboardEvent).where(
                DashboardEvent.event_type == "ui_gap_consolidation.applied"
            )
        ).all()
    assert len(events) == 1 and events[0].metadata_json["actor"] == OPERATOR
    # ui-plan-status and change lists show MERGED.
    status_view = harness.repository.get_sync(UUID(blueprint_id))
    assert status_view is not None
    assert status_view["change_requests"]["merged"] == 4
    assert status_view["change_requests"]["canonical"] == 2
    assert status_view["change_requests"]["by_kind"]["backend-gap"]["MERGED"] == 2
    assert status_view["gap_consolidations"][0]["status"] == "APPLIED"
    merged = harness.change_requests.list_requests_sync("falcone", "MERGED", 50)
    assert len(merged) == 4


def test_apply_is_idempotent(tmp_path: Path) -> None:
    harness, consolidations, blueprint_id, _digest, _ids = setup(tmp_path)
    view = proposed(consolidations, blueprint_id, [("Cursor pagination", [B1, B2])])
    first = consolidations.apply_sync(
        UUID(blueprint_id), view["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR
    )
    with harness.change_requests.sessions() as session:
        rows = {
            row.id: (row.status, row.updated_at) for row in session.scalars(select(ChangeRequest))
        }
    second = consolidations.apply_sync(
        UUID(blueprint_id), view["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR
    )
    assert second["replayed"] is True and second["result"] == first["result"]
    with harness.change_requests.sessions() as session:
        again = {
            row.id: (row.status, row.updated_at) for row in session.scalars(select(ChangeRequest))
        }
    assert again == rows


def test_apply_refusals(tmp_path: Path) -> None:
    harness, consolidations, blueprint_id, digest, ids = setup(tmp_path)
    view = proposed(consolidations, blueprint_id, [("Cursor pagination", [B1, B2])])
    sha12, identity = view["plan_sha12"], UUID(blueprint_id)
    with pytest.raises(UiGapConsolidationNotFoundError):
        consolidations.apply_sync(identity, "0" * 12, CONSOLIDATE_PHRASE, OPERATOR)
    with pytest.raises(ValueError, match="12 hex"):
        consolidations.apply_sync(identity, sha12[:8], CONSOLIDATE_PHRASE, OPERATOR)
    with pytest.raises(ValueError, match="allowlisted operator"):
        consolidations.apply_sync(identity, sha12, CONSOLIDATE_PHRASE, "cli:somebody")
    with pytest.raises(ValueError, match="typed confirmation"):
        consolidations.apply_sync(identity, sha12, "CONSOLIDATE", OPERATOR)
    # A running fan-out (the NEW-146 marker) fences the apply.
    harness.claim(blueprint_id, ui_fanout_workflow_id(blueprint_id, 2))  # type: ignore[attr-defined]
    with pytest.raises(UiFanoutActiveError):
        consolidations.apply_sync(identity, sha12, CONSOLIDATE_PHRASE, OPERATOR)
    harness.repository.release_fanout_sync(identity, ui_fanout_workflow_id(blueprint_id, 2))
    # A member published (or rejected) after the plan refuses the whole apply.
    publish(harness, ids[B2], 102)
    with pytest.raises(ConsolidationRejected) as refused:
        consolidations.apply_sync(identity, sha12, CONSOLIDATE_PHRASE, OPERATOR)
    assert refused.value.details == [f"{B2}: is PUBLISHED and cannot be merged"]
    with harness.change_requests.sessions.begin() as session:
        session.execute(
            update(ChangeRequest).where(ChangeRequest.id == ids[B2]).values(status="REJECTED")
        )
    with pytest.raises(ConsolidationRejected, match="UI_GAP_APPLY_REFUSED"):
        consolidations.apply_sync(identity, sha12, CONSOLIDATE_PHRASE, OPERATOR)
    # Nothing changed: no member merged, no canonical created.
    assert status(harness, ids[B1]) == "CANDIDATE"
    assert (
        harness.change_requests.get_sync(item_change_request_id(digest, canonical_key([B1, B2])))
        is None
    )


def test_a_cross_repository_plan_is_refused_at_apply(tmp_path: Path) -> None:
    harness, consolidations, blueprint_id, digest, ids = setup(tmp_path)
    view = proposed(consolidations, blueprint_id, [("Cursor pagination", [B1, B2])])
    # A plan that mixes repositories never passes check_plan; even a stored one (written
    # directly, with a matching sha) is refused by the apply's own member check.
    plan = copy.deepcopy(view["plan"])
    plan["clusters"][0]["members"].append(
        {"change_request_id": str(ids[W1]), "item_key": W1, "status": "CANDIDATE", "revision": 1}
    )
    with harness.repository.sessions.begin() as session:
        row = session.get(models.UiGapConsolidation, UUID(view["id"]))
        assert row is not None
        row.plan_json = plan
        row.plan_sha256 = plan_sha256(plan)
    with pytest.raises(ConsolidationRejected) as refused:
        consolidations.apply_sync(
            UUID(blueprint_id), plan_sha256(plan)[:12], CONSOLIDATE_PHRASE, OPERATOR
        )
    assert any("mixes repositories" in detail for detail in refused.value.details)
    assert any(f"{W1}: is not a backend-gap" in detail for detail in refused.value.details)
    # A tampered plan whose sha no longer matches is refused too.
    with harness.repository.sessions.begin() as session:
        row = session.get(models.UiGapConsolidation, UUID(view["id"]))
        assert row is not None
        row.plan_json = {**plan, "notes": ["changed"]}
    with pytest.raises(ValueError, match="no longer matches its sha256"):
        consolidations.apply_sync(
            UUID(blueprint_id), plan_sha256(plan)[:12], CONSOLIDATE_PHRASE, OPERATOR
        )
    assert status(harness, ids[B1]) == "CANDIDATE"
    assert digest


# Blocker resolution ----------


def test_merged_ids_resolve_to_their_canonical_request_hop_by_hop(tmp_path: Path) -> None:
    harness, consolidations, blueprint_id, digest, ids = setup(tmp_path)
    view = proposed(consolidations, blueprint_id, [("Cursor pagination", [B1, B2])])
    consolidations.apply_sync(UUID(blueprint_id), view["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR)
    pagination = item_change_request_id(digest, canonical_key([B1, B2]))
    resolved = harness.change_requests.resolve_sync(ids[B1])
    assert resolved["change_request_id"] == str(pagination)
    assert resolved["merged_via"] == [str(ids[B1])]
    assert harness.change_requests.resolve_sync(ids[PLANS])["merged_via"] == []
    # A canonical request merged again (a later consolidation) resolves over two hops.
    with harness.change_requests.sessions.begin() as session:
        session.execute(
            update(ChangeRequest)
            .where(ChangeRequest.id == pagination)
            .values(status="MERGED", merged_into_id=ids[PLANS])
        )
    resolved = harness.change_requests.resolve_sync(ids[B1])
    assert resolved["change_request_id"] == str(ids[PLANS])
    assert resolved["merged_via"] == [str(ids[B1]), str(pagination)]
    # An item created after the consolidation is blocked by the canonical request.
    with harness.change_requests.sessions.begin() as session:
        session.execute(update(ChangeRequest).where(ChangeRequest.id == pagination)
                        .values(status="DRAFTING", merged_into_id=None))  # fmt: skip
        session.delete(session.get(ChangeRequest, ids["page:workspaces"]))
    writer = ui_fanout_workflow_id(blueprint_id, 2)
    harness.claim(blueprint_id, writer)  # type: ignore[attr-defined]
    prepared = harness.ui._prepare_item(
        {
            "blueprint_id": blueprint_id,
            "item_key": "page:workspaces",
            "source_worktree": "/srv/x",
            "orchestration": {"workflow_id": writer},
        }
    )
    assert prepared["status"] == "PROPOSAL_READY"
    created = harness.change_requests.get_sync(ids["page:workspaces"])
    assert created is not None
    blocked = [entry["item_key"] for entry in created["ui_context"]["blocked_by"]]
    assert canonical_key([B1, B2]) in blocked and B2 not in blocked


# Routing, instructions, API and CLI ----------


def test_the_plan_step_routes_to_claude_with_the_pinned_schema(tmp_path: Path) -> None:
    assert (
        ControlledWorkflow._activity_queue("ui_consolidate.plan", {}) == ROLE_TASK_QUEUES["claude"]
    )
    assert ControlledWorkflow._activity_queue("ui_consolidate.record_plan", {}) == (
        "engineering-control"
    )
    root = Settings().agent_pack_root
    if not (root / "skills" / "triage-ui-gap" / "SKILL.md").is_file():
        pytest.skip("requires the released agent pack with the triage-ui-gap skill")
    pack = AgentInstructionPack(root)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    request = AgentRequest(
        project=Project.FALCONE,
        role="ui-gap-triager",
        provider=Provider.CLAUDE,
        model_class=ModelClass.WORKER,
        prompt="Consolidate.",
        working_directory=str(worktree),
        output_schema=plan_schema(),
        allowed_tools=("Read", "Glob", "Grep"),
    )
    enriched, bundle = pack.enrich(request, "ui_consolidate.plan")
    assert enriched.output_schema == plan_schema()
    assert "Skill: `triage-ui-gap`" in enriched.prompt and bundle.sha256


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, Mock, Harness]:
    harness = Harness(tmp_path)
    fake = Mock()
    fake.start_ui_gap_consolidation = AsyncMock(
        side_effect=lambda identity, workflow_id: OperationAccepted(
            workflow_id=workflow_id, run_id="plan"
        )
    )
    fake.start_ui_fanout = AsyncMock(
        side_effect=lambda blueprint_id, max_items, workflow_id: OperationAccepted(
            workflow_id=workflow_id, run_id="fan"
        )
    )
    fake.ui_fanout_state = AsyncMock(return_value=("RUNNING", OperationAccepted(
        workflow_id="x", run_id="r")))  # fmt: skip
    app = create_app(
        settings=Settings(),
        service=fake,
        token=TOKEN,
        operations_repository=Mock(),
        command_bus=Mock(dispatch_pending=AsyncMock(return_value=False)),
        change_request_repository=harness.change_requests,
        ui_blueprint_repository=harness.repository,
    )
    return TestClient(app), fake, harness


def test_api_previews_then_applies_and_starts_the_drafting_fanout(api: Any) -> None:
    client, fake, harness = api
    blueprint_id, digest = approved(harness)
    ids = fanned_out(harness, blueprint_id, digest)
    base = f"/v1/ui-blueprints/{blueprint_id}/gap-consolidations"
    with client:
        started = client.post(base, json={"kind": "backend-gap"}, headers=CLI)
        assert started.status_code == 200, started.text
        planning = started.json()
        assert planning["status"] == "PLANNING" and planning["kind"] == "backend-gap"
        fake.start_ui_gap_consolidation.assert_awaited_once()
        consolidations = UiGapConsolidationRepository(engine=harness.engine)
        identity = UUID(planning["id"])
        loaded = consolidations.load_sync(identity, planning["workflow_id"])
        consolidations.record_plan_sync(
            identity,
            plan_output(loaded["input_sha256"], [("Cursor pagination", [B1, B2])], []),
            {},
        )
        preview = client.post(base, json={"kind": "backend-gap"}, headers=CLI).json()
        assert preview["status"] == "PROPOSED" and preview["preview"]
        assert fake.start_ui_gap_consolidation.await_count == 1  # reused, no model call
        sha12 = preview["plan_sha12"]
        apply = f"{base}/apply"
        body = {"plan_sha12": sha12, "typed_confirmation": CONSOLIDATE_PHRASE}
        assert client.post(apply, json={**body, "typed_confirmation": "NO"}, headers=CLI)\
            .status_code == 422  # fmt: skip
        assert client.post(apply, json={**body, "plan_sha12": "0" * 12}, headers=CLI)\
            .status_code == 404  # fmt: skip
        # A running fan-out refuses the apply.
        harness.claim(blueprint_id, ui_fanout_workflow_id(blueprint_id, 7))
        refused = client.post(apply, json=body, headers=CLI)
        assert refused.status_code == 409 and "running" in refused.json()["detail"]
        harness.repository.release_fanout_sync(
            UUID(blueprint_id), ui_fanout_workflow_id(blueprint_id, 7)
        )
        applied = client.post(apply, json=body, headers=CLI)
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "APPLIED"
        assert applied.json()["fanout"]["workflow_id"] == ui_fanout_workflow_id(blueprint_id, 1)
        fake.start_ui_fanout.assert_awaited_once()
        shown = client.get(f"/v1/change-requests/{ids[B1]}", headers=CLI).json()
        assert shown["status"] == "MERGED" and shown["merged_into"]["item_key"].startswith(
            "gap:merged-"
        )
        merged = client.get("/v1/change-requests?status=MERGED", headers=CLI).json()["items"]
        assert len(merged) == 2
        status_view = client.get(f"/v1/ui-blueprints/{blueprint_id}", headers=CLI).json()
        assert status_view["change_requests"]["merged"] == 2
        listed = client.get(base, headers=CLI).json()["items"]
        assert listed[0]["status"] == "APPLIED"


def test_cli_gap_consolidate_routes() -> None:
    from test_new_123_ui_surfaces import RecordingClient

    from engineering_control.cli import dispatch, parser

    identifier = "11111111-1111-4111-8111-111111111111"
    base = f"/v1/ui-blueprints/{identifier}/gap-consolidations"
    cases = {
        ("ui-blueprint-gap-consolidate", identifier): ("POST", base, {"replan": False}),
        ("ui-blueprint-gap-consolidate", identifier, "--kind", "kit-gap", "--replan"): (
            "POST",
            base,
            {"replan": True, "kind": "kit-gap"},
        ),
        ("ui-blueprint-gap-consolidate", identifier, "--apply", "a" * 12, CONSOLIDATE_PHRASE): (
            "POST",
            f"{base}/apply",
            {"plan_sha12": "a" * 12, "typed_confirmation": CONSOLIDATE_PHRASE},
        ),
        ("changes", "--status", "MERGED"): (
            "GET",
            "/v1/change-requests?status=MERGED&limit=20",
            None,
        ),
    }
    for argv, (method, path, payload) in cases.items():
        result = dispatch(RecordingClient(), parser().parse_args(list(argv)))  # type: ignore[arg-type]
        assert result == {"method": method, "path": path, "payload": payload}, argv
    with pytest.raises(SystemExit):
        parser().parse_args(["ui-blueprint-gap-consolidate", identifier, "--kind", "page"])


# Workflows on a real local Temporal ----------


@temporal
async def test_plan_workflow_then_apply_then_the_fanout_drafts_and_reviews_canonicals(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    blueprint_id, digest = approved(harness)
    harness.responses.update(
        {
            "project.snapshot": snapshot,
            "ui_fanout.gap_draft": gap_drafter,
            "change_request.review": item_reviewer(set()),
            "change_request.github_dedupe": dedupe,
        }
    )
    first = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id),
        ui_fanout_workflow_id(blueprint_id, 1),
    )
    assert (first["status"], first["failed"]) == ("COMPLETED", 0)
    before_history = harness.history
    consolidations = UiGapConsolidationRepository(engine=harness.engine)
    requested = consolidations.request_plan_sync(UUID(blueprint_id), OPERATOR, None, False)
    identity = requested["consolidation"]["id"]

    def plan(payload: dict[str, Any]) -> dict[str, Any]:
        request = payload["draft_request"]
        assert (request["provider"], request["role"]) == ("claude", "ui-gap-triager")
        assert request["output_schema"] == plan_schema()
        assert B1 in request["prompt"] and "--- BEGIN GAP REQUESTS ---" in request["prompt"]
        sha = requested["consolidation"]["input_sha256"]
        assert f"input_sha256 = {sha}" in request["prompt"]
        return plan_output(
            sha, [("Cursor pagination", [B1, B2]), ("Create wizard", [W1, W2])], [PLANS]
        )

    harness.responses["ui_consolidate.plan"] = respond(plan, "draft_request")
    planned = await harness.run(
        UiGapConsolidationPlanWorkflow,
        UiGapConsolidationPlanInput(consolidation_id=identity),
        requested["consolidation"]["workflow_id"],
    )
    assert (planned["status"], planned["clusters"], planned["merged"]) == ("PROPOSED", 2, 4)
    view = consolidations.get_sync(UUID(identity))
    assert view is not None and view["plan_sha256"] == planned["plan_sha256"]
    assert view["plan"]["drafter"]["provider"] == "claude"
    with harness.repository.sessions() as session:
        runs = session.scalars(
            select(models.Run).where(models.Run.workflow_id == view["workflow_id"])
        ).all()
    assert [(run.run_type, run.status) for run in runs] == [("ui-gap-consolidation", "SUCCESS")]
    consolidations.apply_sync(UUID(blueprint_id), view["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR)
    calls = len(harness.calls)
    second = await harness.run(
        UiBlueprintFanOutWorkflow,
        UiBlueprintFanOutInput(blueprint_id=blueprint_id),
        ui_fanout_workflow_id(blueprint_id, 2),
    )
    # Only the two canonical requests are drafted; merged members never again.
    assert (second["status"], second["drafted"], second["failed"]) == ("COMPLETED", 2, 0)
    drafts = harness.payloads["ui_fanout.gap_draft"][-2:]
    assert harness.calls[calls:].count("ui_fanout.gap_draft") == 2
    for payload in drafts:
        request = payload["draft_request"]
        assert request["provider"] == "claude"
        assert "consolidates several gap change requests" in request["prompt"]
        assert "--- BEGIN MERGED VARIANTS ---" in request["prompt"]
    reviews = harness.payloads["change_request.review"][-2:]
    for payload in reviews:
        request = payload["review_request"]
        assert (request["provider"], request["role"]) == ("codex", "code-reviewer")
        assert "consolidates the duplicate gap requests" in request["prompt"]
        assert "gap-b00" in request["prompt"]  # the verbatim variants are reviewed
    pagination = item_change_request_id(digest, canonical_key([B1, B2]))
    drafted = harness.change_requests.get_sync(pagination)
    assert drafted is not None and drafted["status"] == "CANDIDATE"
    body = drafted["issue_body"]
    assert "Consolidates 2 gap change requests" in body
    assert f"change request `{item_change_request_id(digest, B1)}` (`{B1}`)" in body
    variant = [line for line in body.splitlines() if "b002-cursor-pagination passes" in line]
    assert len(variant) == 1 and variant[0].startswith("- [ ] ") and B2[4:] in variant[0]
    assert "Needed by:" in body
    for key in (B1, B2, W1, W2):
        assert status(harness, item_change_request_id(digest, key)) == "MERGED"
    # Replay safety: the fan-out history recorded before the consolidation replays against
    # the current workflow code, as does the history that drafted the canonicals.
    await Replayer(workflows=[UiBlueprintFanOutWorkflow]).replay_workflow(before_history)
    await Replayer(workflows=[UiBlueprintFanOutWorkflow]).replay_workflow(harness.history)


@temporal
async def test_an_invalid_plan_ends_the_workflow_without_a_plan(tmp_path: Path) -> None:
    harness, consolidations, blueprint_id, _digest, _ids = setup(tmp_path)
    harness.responses["project.snapshot"] = snapshot
    requested = consolidations.request_plan_sync(UUID(blueprint_id), OPERATOR, None, False)
    sha = requested["consolidation"]["input_sha256"]
    harness.responses["ui_consolidate.plan"] = respond(
        plan_output(sha, [("Mixed", [B1, W1])], []), "draft_request"
    )
    result = await harness.run(
        UiGapConsolidationPlanWorkflow,
        UiGapConsolidationPlanInput(consolidation_id=requested["consolidation"]["id"]),
        requested["consolidation"]["workflow_id"],
    )
    assert (result["status"], result["error_code"]) == ("INVALID", "UI_GAP_PLAN_REJECTED")
    assert any("mixes repositories" in problem for problem in result["problems"])
    view = consolidations.get_sync(UUID(requested["consolidation"]["id"]))
    assert view is not None and view["status"] == "INVALID" and view["apply_command"] is None


def test_blueprint_with_no_duplicates_has_nothing_to_consolidate(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    value = blueprint()
    blueprint_id, digest = approved(harness, value)
    consolidations = UiGapConsolidationRepository(engine=harness.engine)
    # Nothing fanned out yet: no gap request exists, so there is nothing to merge.
    with pytest.raises(ValueError, match="UI_GAP_NOTHING_TO_CONSOLIDATE"):
        consolidations.request_plan_sync(UUID(blueprint_id), OPERATOR, None, False)
    assert digest
    engine = create_engine(f"sqlite:///{tmp_path / 'other.sqlite'}")
    Base.metadata.create_all(engine)
    with pytest.raises(ValueError, match="not found"):
        UiGapConsolidationRepository(engine=engine).request_plan_sync(
            UUID(int=1), OPERATOR, None, False
        )
    assert UiBlueprintRepository(engine=engine).get_sync(UUID(int=1)) is None
    assert isinstance(UiBlueprint.__tablename__, str)
    assert ChangeRequestRepository(engine=engine).list_requests_sync(None, "MERGED", 5) == []


# Round 1 (review) -----------------------------------------------------------------------


def test_apply_refuses_a_member_revised_since_the_plan(tmp_path: Path) -> None:
    harness, consolidations, blueprint_id, digest, ids = setup(tmp_path)
    view = proposed(consolidations, blueprint_id, [("Cursor pagination", [B1, B2])])
    member = view["plan"]["clusters"][0]["members"][1]
    assert (member["revision"], len(member["content_sha256"])) == (1, 64)
    # The operator revises B2 into something else; it is a CANDIDATE again at revision 2.
    harness.change_requests.revise_sync(ids[B2], "Make it a search API instead.", OPERATOR)
    other = gap_change_request(B2, FALCONE)
    other["acceptance_criteria"] = ["Search returns ranked results."]
    persisted = harness.change_requests.persist_candidate_sync(
        ids[B2], 2, validate_proposal(other, "falcone", FALCONE), REVIEW, {}
    )
    assert persisted["status"] == "CANDIDATE"
    with pytest.raises(ConsolidationRejected) as refused:
        consolidations.apply_sync(
            UUID(blueprint_id), view["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR
        )
    assert refused.value.code == "UI_GAP_PLAN_STALE"
    assert refused.value.details[0].startswith(f"{B2}: changed since the plan (revision 1 -> 2")
    assert "--replan" in refused.value.details[-1]
    assert status(harness, ids[B1]) == "CANDIDATE" and status(harness, ids[B2]) == "CANDIDATE"
    assert (
        harness.change_requests.get_sync(item_change_request_id(digest, canonical_key([B1, B2])))
        is None
    )


def test_apply_refuses_a_member_whose_content_changed_at_the_same_revision(
    tmp_path: Path,
) -> None:
    harness, consolidations, blueprint_id, _digest, ids = setup(tmp_path)
    view = proposed(consolidations, blueprint_id, [("Cursor pagination", [B1, B2])])
    with harness.change_requests.sessions.begin() as session:
        row = session.get(ChangeRequest, ids[B1])
        assert row is not None
        row.proposal_json = {**row.proposal_json, "why": "Something else entirely."}
    with pytest.raises(ConsolidationRejected) as refused:
        consolidations.apply_sync(
            UUID(blueprint_id), view["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR
        )
    assert refused.value.code == "UI_GAP_PLAN_STALE"
    assert "revision 1 -> 1, content changed" in refused.value.details[0]
    assert status(harness, ids[B1]) == "CANDIDATE"


def test_a_canonical_merged_again_keeps_every_inherited_criterion(tmp_path: Path) -> None:
    harness, consolidations, blueprint_id, digest, ids = setup(tmp_path)
    first = proposed(consolidations, blueprint_id, [("Cursor pagination", [B1, B2])])
    consolidations.apply_sync(UUID(blueprint_id), first["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR)
    middle = canonical_key([B1, B2])
    middle_id = item_change_request_id(digest, middle)
    # The fan-out drafts the canonical request C (its own, merged proposal).
    writer = ui_fanout_workflow_id(blueprint_id, 2)
    harness.claim(blueprint_id, writer)  # type: ignore[attr-defined]
    prepared = harness.ui._prepare_item(
        {
            "blueprint_id": blueprint_id,
            "item_key": middle,
            "source_worktree": "/srv/x",
            "orchestration": {"workflow_id": writer},
        }
    )
    assert prepared["status"] == "DRAFT_READY"
    own = gap_change_request(middle, FALCONE)
    own["acceptance_criteria"] = ["Every list endpoint takes a cursor."]
    harness.change_requests.persist_candidate_sync(
        middle_id, 1, validate_proposal(own, "falcone", FALCONE), REVIEW, {}, fanout_writer=writer
    )
    harness.repository.release_fanout_sync(UUID(blueprint_id), writer)
    # C + D -> E: E carries C's own criterion and, verbatim, A's and B's.
    second = proposed(consolidations, blueprint_id, [("Paged plans", [middle, PLANS])])
    consolidations.apply_sync(
        UUID(blueprint_id), second["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR
    )
    outer = item_change_request_id(digest, canonical_key([middle, PLANS]))
    view = harness.change_requests.get_sync(outer)
    assert view is not None
    sections = {item["field"]: item["entries"] for item in view["ui_context"]["continued"]}
    # Members in blueprint order (the blueprint gap, then the canonical request).
    assert sections["acceptance_criteria"] == [
        f"[{PLANS}] gap-plans-api passes its contract test.",
        f"[{middle}] Every list endpoint takes a cursor.",
        f"[{B1} via {middle}] gap-b001-cursor-pagination passes its contract test.",
        f"[{B2} via {middle}] gap-b002-cursor-pagination passes its contract test.",
    ]
    assert [entry["item_key"] for entry in view["ui_context"]["merged_from"]] == [PLANS, middle]
    assert harness.change_requests.resolve_sync(ids[B1])["change_request_id"] == str(outer)
    assert harness.change_requests.resolve_sync(ids[B1])["merged_via"] == [
        str(ids[B1]),
        str(middle_id),
    ]


def large_document() -> dict[str, Any]:
    """Eight backend duplicates of one need (the review's reproduction)."""
    value = document()
    for index in range(3, 9):
        identity = f"gap-b00{index}-cursor-pagination"
        value["gaps"].append(
            {
                "id": identity,
                "type": "BACKEND_GAP",
                "target_repository": FALCONE,
                "summary": "Lists need a cursor.",
                "evidence": [f"evidence for {identity}"],
                "blocks": ["tenants"],
                "interim": "client paging",
                "seed_ref": None,
            }
        )
        value["pages"][0]["gap_ids"].append(identity)
    return value


LARGE = [f"gap:gap-b00{index}-cursor-pagination" for index in range(1, 9)]


def large_proposals() -> dict[str, dict[str, Any]]:
    proposals = {}
    for key in LARGE:
        proposal = gap_change_request(key, FALCONE)
        proposal["acceptance_criteria"] = [
            f"{key} criterion {number}: " + "x" * (575 - len(key)) for number in range(15)
        ]
        proposals[key] = proposal
    return proposals


def test_a_cluster_too_large_for_one_issue_is_refused_before_anything_merges(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    blueprint_id, digest = approved(harness, large_document())
    ids = fanned_out(harness, blueprint_id, digest, large_document(), large_proposals())
    consolidations = UiGapConsolidationRepository(engine=harness.engine)
    criteria = sum(
        len(f"[{key}] {entry}")
        for key, proposal in large_proposals().items()
        for entry in proposal["acceptance_criteria"]
    )
    assert criteria >= 74_000  # the review's eight-member reproduction (74,370 characters)
    # Plan time: the deterministic check refuses the cluster (INVALID, nothing to apply).
    requested = consolidations.request_plan_sync(UUID(blueprint_id), OPERATOR, "backend-gap", False)
    identity = UUID(requested["consolidation"]["id"])
    loaded = consolidations.load_sync(identity, requested["consolidation"]["workflow_id"])
    recorded = consolidations.record_plan_sync(
        identity, plan_output(loaded["input_sha256"], [("Cursor pagination", LARGE)], []), {}
    )
    assert (recorded["status"], recorded["blocker"]) == ("INVALID", "UI_GAP_CLUSTER_TOO_LARGE")
    assert "split the cluster" in recorded["problems"][0]
    # Apply time: the same cluster in a stored plan (bypassing the plan check) is refused
    # whole, before any member is merged or any canonical created.
    view = consolidations.get_sync(identity)
    assert view is not None
    with harness.repository.sessions.begin() as session:
        row = session.get(models.UiGapConsolidation, identity)
        assert row is not None
        candidates = {item["item_key"]: item for item in row.inputs_json}
        from engineering_control.engineering.ui_gap_consolidation import build_plan

        plan = build_plan(
            {"clusters": [{"title": "Cursor pagination", "rationale": "Same need.",
                           "member_keys": LARGE}], "unclustered": [], "notes": []},
            blueprint_id=blueprint_id,
            blueprint_sha256=digest,
            kind="backend-gap",
            input_sha256=row.input_sha256,
            candidates=[candidates[key] for key in LARGE],
            drafter={},
        )  # fmt: skip
        row.plan_json, row.plan_sha256, row.status = plan, plan_sha256(plan), "PROPOSED"
    with pytest.raises(ConsolidationRejected) as refused:
        consolidations.apply_sync(
            UUID(blueprint_id), plan_sha256(plan)[:12], CONSOLIDATE_PHRASE, OPERATOR
        )
    assert refused.value.code == "UI_GAP_CLUSTER_TOO_LARGE"
    assert all(status(harness, ids[key]) == "CANDIDATE" for key in LARGE)
    assert (
        harness.change_requests.get_sync(item_change_request_id(digest, canonical_key(LARGE)))
        is None
    )
    with harness.repository.sessions.begin() as session:
        row = session.get(models.UiGapConsolidation, identity)
        assert row is not None
        row.status = "INVALID"
    # Half the cluster fits: its criteria are all verbatim, what_changes left to the draft
    # only when needed, and the stored context renders within the body limit.
    half = proposed(consolidations, blueprint_id, [("Cursor pagination", LARGE[:2])],
                    kind="backend-gap")  # fmt: skip
    consolidations.apply_sync(UUID(blueprint_id), half["plan_sha12"], CONSOLIDATE_PHRASE, OPERATOR)
    canonical = harness.change_requests.get_sync(
        item_change_request_id(digest, canonical_key(LARGE[:2]))
    )
    assert canonical is not None
    sections = {item["field"]: item["entries"] for item in canonical["ui_context"]["continued"]}
    assert len(sections["acceptance_criteria"]) == 30


def _review2_record(member_spec: dict[str, Any], own: dict[str, Any]) -> dict[str, Any]:
    from engineering_control.engineering.ui_blueprint import FanoutItem
    from engineering_control.engineering.ui_gap_consolidation import canonical_record

    items = {
        key: FanoutItem(key, "backend-gap", "u1", "gntik-ai/falcone", (), dict(member_spec))
        for key in ("gap:a", "gap:b")
    }
    return canonical_record(
        {
            "cluster_key": "gap:merged-x",
            "canonical_id": str(uuid4()),
            "item_kind": "backend-gap",
            "target_repository": "gntik-ai/falcone",
            "title": "Merged",
            "rationale": "Same need.",
            "members": [
                {"change_request_id": str(uuid4()), "item_key": key} for key in ("gap:a", "gap:b")
            ],
        },
        member_items=items,
        variants={key: dict(own) for key in ("gap:a", "gap:b")},
        blueprint_id=str(uuid4()),
        blueprint_sha256="d" * 64,
        project="falcone",
    )


_OWN = {
    "title": "t",
    "summary": "s",
    "why": "w",
    "what_changes": ["change"],
    "acceptance_criteria": ["criterion"],
    "linked": [],
}


def test_a_merge_chain_longer_than_the_resolver_follows_is_refused() -> None:
    """Review 2: eleven consolidations in a chain broke resolution of the oldest member."""
    # Inheriting through a member adds that member to the chain: 8 + 1 earlier canonicals,
    # plus this one, is exactly the 10 hops resolution follows; one more is refused.
    within = {"variants": [{**_OWN, "item_key": "gap:o", "via": [f"gap:c{n}" for n in range(8)]}]}
    assert _review2_record(within, _OWN)["spec"]["variants"]
    beyond = {"variants": [{**_OWN, "item_key": "gap:o", "via": [f"gap:c{n}" for n in range(9)]}]}
    with pytest.raises(ConsolidationRejected) as refused:
        _review2_record(beyond, _OWN)
    assert refused.value.code == "UI_GAP_MERGE_TOO_DEEP"


def test_variants_too_large_for_the_drafter_are_refused_never_truncated() -> None:
    """Review 2: a trailing what_changes requirement was cut from both agents' prompts."""
    from engineering_control.engineering.ui_blueprint import FanoutItem
    from engineering_control.engineering.ui_gap_consolidation import canonical_variants_text

    huge = {**_OWN, "what_changes": ["x" * 21_000]}
    with pytest.raises(ConsolidationRejected) as refused:
        _review2_record({}, huge)
    assert refused.value.code == "UI_GAP_CLUSTER_TOO_LARGE"
    item = FanoutItem("gap:merged-x", "backend-gap", "u1", "gntik-ai/falcone", (),
                      {"variants": [{"what_changes": ["y" * 41_000]}]})  # fmt: skip
    with pytest.raises(ConsolidationRejected):
        canonical_variants_text(item)
    record = _review2_record({}, _OWN)
    assert record["spec"]["verbatim_what_changes"] is True


def test_the_canonical_review_prompt_keeps_unicode_unescaped(tmp_path: Path) -> None:
    """Review 3: ASCII-escaped JSON grew 560-character CJK entries sixfold, so a cluster that
    fit the body limit pushed the reviewer's request past its 200k bound after merging."""
    from unittest.mock import Mock

    from engineering_control.activities import ui_plan
    from engineering_control.config import Settings

    handler = ui_plan.UiPlanActivityHandler(Settings(), repository=Mock(), change_requests=Mock())
    entry = "分页" * 280  # 560 CJK characters
    continued = [{"heading": f"[gap:a{n}] what_changes", "entries": [entry] * 15} for n in range(2)]
    review = handler._prepare_item_review(
        {
            "proposal": {"title": "Merged", "what_changes": [entry]},
            "source_worktree": str(tmp_path),
            "project": "falcone",
            "target_repository": "gntik-ai/falcone",
            "item_key": "gap:merged-x",
            "item_kind": "backend-gap",
            "description_redacted": "Consolidated gap.",
            "merged_from": ["gap:a0", "gap:a1"],
            "continued": continued,
            "change_request_id": str(uuid4()),
            "revision": 1,
        }
    )
    prompt = json.dumps(review, ensure_ascii=False)
    assert entry in prompt and "\\u5206" not in prompt
