"""NEW-234 PR6: the backlog decomposition fan-out (design section 4).

One change request per task under its deterministic id, reviewed by Codex against its
target repository's snapshot; dedupe by lineage, GitHub title and publisher adoption;
dependency gating (409) and re-render when a predecessor is published; the NEW-146-style
retry; the ready set and its bulk approval; story amendments as operator-published edits.
The controller steps run against SQLite; Temporal paths are in the workflow tests.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from new_234_fanout_fixtures import KEYS, REVISE, FanoutHarness, worktree
from new_234_plan_fixtures import OPERATOR
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from engineering_control.activities import decomposition_fanout as fanout_activities
from engineering_control.activities.change_requests import ChangeRequestGitHubHandler
from engineering_control.api.main import create_app
from engineering_control.api.service import OperationAccepted
from engineering_control.db import change_requests as change_request_db
from engineering_control.db.backlog_decompositions import DecompositionFanoutActiveError
from engineering_control.db.change_requests import (
    ChangeRequestRepository,
    DependenciesAwaitingError,
)
from engineering_control.db.models import (
    BacklogDecomposition,
    ChangeRequest,
    DashboardEvent,
    NotificationOutbox,
)
from engineering_control.engineering.backlog import calculated_issue_risk
from engineering_control.engineering.change_request_publisher import (
    publish_change_request,
    verified_plan,
)
from engineering_control.engineering.change_requests import (
    DecompositionChangeContext,
    approval_phrase,
)
from engineering_control.engineering.decomposition import APPROVAL_PHRASE
from engineering_control.engineering.decomposition_amendments import (
    render_story_amendment_edits,
    story_amendment_edits,
)
from engineering_control.engineering.decomposition_fanout import (
    RETRY_FANOUT_PHRASE,
    amended_story_body,
    fanout_tasks,
    fanout_workflow_id,
    task_change_request_id,
)

TOKEN = "x" * 32
CLI = {"Authorization": f"Bearer {TOKEN}", "X-Engineering-Actor": "engineering-ctl:hermes"}


@pytest.fixture
def harness(tmp_path: Path) -> FanoutHarness:
    return FanoutHarness(tmp_path)


# Identity and order -----------------------------------------------------------------------


def test_tasks_fan_out_predecessors_first_under_deterministic_ids(
    harness: FanoutHarness,
) -> None:
    approved = harness.approved()
    plan = harness.repository.load_approved_sync(UUID(approved["id"]))["plan"]
    tasks = fanout_tasks(plan)
    assert [task.key for task in tasks] == list(KEYS)
    assert [task.repository for task in tasks] == [
        "gntik-ai/falcone-serverless",
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
        "gntik-ai/falcone",
    ]
    assert task_change_request_id("a" * 64, KEYS[0]) == task_change_request_id("a" * 64, KEYS[0])
    assert task_change_request_id("a" * 64, KEYS[0]) != task_change_request_id("b" * 64, KEYS[0])
    assert fanout_workflow_id(approved["id"], 3) == (
        f"backlog-decomposition-fanout-{approved['id']}-3"
    )


def test_a_context_without_dependencies_renders_as_before() -> None:
    base = {
        "decomposition_id": "11111111-1111-4111-8111-111111111111",
        "plan_sha256": "a" * 64,
        "task_key": "API-01.t2",
        "story_key": "API-01",
        "story_issue": 3,
        "epic_issue": 1,
    }
    assert DecompositionChangeContext.model_validate(base) == DecompositionChangeContext(
        **base, depends_on_tasks=(), depends_on=()
    )
    with pytest.raises(ValueError, match="cannot depend on itself"):
        DecompositionChangeContext(**base, depends_on_tasks=("API-01.t2",))
    with pytest.raises(ValueError, match="needs its predecessor"):
        DecompositionChangeContext(**base, depends_on=("gntik-ai/falcone#4",))
    with pytest.raises(ValueError):
        DecompositionChangeContext(
            **base, depends_on_tasks=("API-01.t1",), depends_on=("falcone#4",)
        )


# The fan-out ------------------------------------------------------------------------------


def test_one_candidate_per_task_reviewed_against_its_target_snapshot(
    harness: FanoutHarness,
) -> None:
    approved = harness.approved()
    result = harness.run_fanout(approved)
    assert (result["drafted"], result["failed"], result["skipped"]) == (4, 0, 0)
    plan = result["plans"][0]
    # falcone and falcone-charts share falcone's snapshot (source and deployment worktrees).
    assert [(item["project"], item["worktrees"]) for item in plan["snapshots"]] == [
        (
            "falcone",
            {
                "gntik-ai/falcone": "source_worktree",
                "gntik-ai/falcone-charts": "deployment_worktree",
            },
        ),
        ("falcone-serverless", {"gntik-ai/falcone-serverless": "source_worktree"}),
    ]
    assert [request["working_directory"] for request in harness.reviews] == [
        worktree("falcone-serverless", "source_worktree"),
        worktree("falcone", "source_worktree"),
        worktree("falcone", "deployment_worktree"),
        worktree("falcone", "source_worktree"),
    ]
    assert {(r["role"], r["provider"], r["model_class"]) for r in harness.reviews} == {
        ("code-reviewer", "codex", "judge")
    }
    assert "gntik-ai/falcone-charts" in harness.reviews[2]["prompt"]
    for key in KEYS:
        view = harness.request(approved, key)
        assert view["status"] == "CANDIDATE" and view["project"] == "falcone-serverless"
        context = view["decomposition_context"]
        assert context["decomposition_id"] == approved["id"]
        assert context["task_key"] == key.split(":", 1)[1]
        assert "<!-- hermes-decomposition plan=" in view["issue_body"]
        with harness.change_requests.sessions() as session:
            row = session.get(ChangeRequest, UUID(view["id"]))
            assert row is not None
            story = context["story_key"]
            assert row.decomposition_lineage == f"falcone-serverless:{story}:{context['task_key']}"
            # The import risk of the real rendered body is recorded (never reworded).
            risk = row.dedupe_json["import_risk"]
            assert risk["calculated_risk"] == calculated_issue_risk(
                row.target_repository,
                ("hermes:change-request", row.proposal_json["kind"]),
                str(row.issue_title),
                str(row.issue_body)[:6000],
            )
    first = harness.request(approved, KEYS[0])
    assert first["target_repository"] == "gntik-ai/falcone-serverless"
    assert harness.request(approved, KEYS[2])["target_repository"] == "gntik-ai/falcone-charts"
    # Predecessors are named in the body; no issue exists yet, so no dependency line.
    dependent = harness.request(approved, KEYS[1])
    assert "- Predecessor tasks: `RUN-01.t1`" in dependent["issue_body"]
    assert "Depends on:" not in dependent["issue_body"]
    assert dependent["awaiting_dependencies"] == ["RUN-01.t1"]
    assert dependent["approve_command"] is None
    assert first["awaiting_dependencies"] == [] and first["approve_command"]
    row = harness.repository.get_sync(UUID(approved["id"]))
    assert row is not None and row["fanout"]["last_run"]["drafted"] == 4
    assert row["fanout_workflow_id"] is None  # released when the run ended


def test_a_replayed_fanout_creates_no_duplicates(harness: FanoutHarness) -> None:
    approved = harness.approved()
    harness.run_fanout(approved)
    before = {row.id: row.issue_body for row in harness.rows()}
    again = harness.run_fanout(approved, run_id="run-2")
    assert (again["drafted"], again["processed"]) == (0, 0)
    assert again["plans"][0]["status"] == "DONE"
    assert again["workflow_id"].endswith("-2")
    assert {row.id: row.issue_body for row in harness.rows()} == before


def test_batches_and_max_items_cap_a_run(
    harness: FanoutHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fanout_activities, "FANOUT_BATCH", 2)
    approved = harness.approved()
    capped = harness.run_fanout(approved, max_items=3)
    assert [len(plan["items"]) for plan in capped["plans"]] == [2, 1]
    assert [plan["pending_total"] for plan in capped["plans"]] == [4, 2]
    assert capped["processed"] == 3
    assert capped["finalized"]["pending_total"] == 1
    rest = harness.run_fanout(approved, run_id="run-2")
    assert (rest["drafted"], rest["plans"][0]["items"]) == (
        1,
        [{"key": KEYS[3], "repository": "gntik-ai/falcone"}],
    )
    assert len(harness.rows()) == 4


def test_the_fanout_rechecks_the_plan_and_the_pack(harness: FanoutHarness) -> None:
    approved = harness.approved()
    role = harness.pack / "roles" / "backlog-decomposer.yaml"
    role.write_text(role.read_text() + "purpose: changed\n")
    blocked = harness.run_fanout(approved)
    assert blocked["plans"][-1]["blocker"] == "DECOMPOSITION_PACK_DRIFT"
    assert harness.rows() == []
    with harness.repository.sessions.begin() as session:
        row = session.get(BacklogDecomposition, UUID(approved["id"]))
        assert row is not None
        row.plan_json = {**row.plan_json, "stories": []}
    with pytest.raises(ValueError, match="no longer matches its hash"):
        harness.fanout._plan({"decomposition_id": approved["id"], "max_items": 5})


def test_a_superseded_fanout_writes_nothing(harness: FanoutHarness) -> None:
    approved = harness.approved()
    identity = UUID(approved["id"])
    stale = harness.repository.claim_fanout_sync(identity)["workflow_id"]
    harness.repository.release_fanout_sync(identity, stale)
    current = harness.repository.claim_fanout_sync(identity)["workflow_id"]
    assert current != stale
    old = {"workflow_id": stale, "workflow_run_id": "old"}
    planned = harness.fanout._plan(
        {"decomposition_id": approved["id"], "max_items": 5, "orchestration": old}
    )
    assert planned["blocker"] == "DECOMPOSITION_FANOUT_SUPERSEDED"
    item = harness.fanout._prepare_item(
        {"decomposition_id": approved["id"], "item_key": KEYS[0], "orchestration": old}
    )
    assert (item["status"], item["blocker"]) == ("SKIPPED", "DECOMPOSITION_FANOUT_SUPERSEDED")
    # A write raced by a supersession (the fence is atomic with the row write).
    new = {"workflow_id": current, "workflow_run_id": "new"}
    prepared = harness.fanout._prepare_item(
        {
            "decomposition_id": approved["id"],
            "item_key": KEYS[0],
            "source_worktree": "/w",
            "orchestration": new,
        }
    )
    assert prepared["status"] == "PROPOSAL_READY"
    failed = harness.cr._record_failure(
        {**prepared, "failure_phase": "review", "blocker": "X", "orchestration": old}
    )
    assert (failed["status"], failed["blocker"]) == (
        "SUPERSEDED",
        "DECOMPOSITION_FANOUT_SUPERSEDED",
    )
    assert harness.request(approved, KEYS[0])["status"] == "DRAFTING"


# Dedupe -----------------------------------------------------------------------------------


def _retitled(plan: dict[str, Any]) -> None:
    """Another plan of the same stories (a different plan hash, the same task keys)."""
    plan["stories"][0]["tasks"][0]["title"] = "Route the Functions API to the Wasm worker pool"


def test_an_existing_request_of_the_lineage_supersedes_the_task(harness: FanoutHarness) -> None:
    first = harness.approved()
    harness.run_fanout(first)
    harness.publish(first, KEYS[0], 101)
    second = harness.approved(_retitled)
    assert second["plan_sha256"] != first["plan_sha256"]
    result = harness.run_fanout(second)
    assert (result["drafted"], result["skipped"]) == (0, 4)
    assert len(harness.rows()) == 4
    superseded = harness.repository.get_sync(UUID(second["id"]))["fanout"]["superseded"]  # type: ignore[index]
    assert set(superseded) == set(KEYS)
    assert superseded[KEYS[0]]["published"] is True
    assert superseded[KEYS[0]]["existing"][0]["status"] == "PUBLISHED"
    # A replay does not reconsider them; --replace does, except the published one.
    assert harness.run_fanout(second, run_id="run-2")["processed"] == 0
    replaced = harness.run_fanout(second, replace=True, run_id="run-3")
    assert (replaced["drafted"], replaced["skipped"]) == (3, 0)
    assert len(harness.rows()) == 7
    assert harness.change_requests.item_states_sync([harness.request_id(second, KEYS[0])]) == {}
    with harness.change_requests.sessions() as session:
        row = session.get(ChangeRequest, harness.request_id(second, KEYS[1]))
        assert row is not None
        assert row.dedupe_json["lineage_matches"][0]["change_request_id"] == str(
            harness.request_id(first, KEYS[1])
        )
    # The new API-01.t1 waits for the published RUN-01.t1 of the first plan (its lineage).
    dependent = harness.request(second, KEYS[1])
    assert "- Depends on: gntik-ai/falcone-serverless#101" in dependent["issue_body"]
    assert dependent["awaiting_dependencies"] == []


def test_an_identical_plan_reuses_the_first_decompositions_requests(
    harness: FanoutHarness,
) -> None:
    first = harness.approved()
    harness.run_fanout(first)
    second = harness.approved()
    assert second["plan_sha256"] == first["plan_sha256"] and second["id"] != first["id"]
    result = harness.run_fanout(second)
    assert result["processed"] == 0 and len(harness.rows()) == 4
    ready = harness.repository.ready_sync(UUID(second["id"]))
    assert ready["ready"] == [] and ready["counts"] == {"SUPERSEDED_BY_EXISTING": 4}


def test_the_github_title_search_reaches_the_cross_repository_targets(
    tmp_path: Path, harness: FanoutHarness
) -> None:
    approved = harness.approved()
    matches = {
        "status": "MATCHES",
        "searched_repository": "gntik-ai/falcone-charts",
        "matches": [{"number": 7, "title": "Deploy workers", "url": "u", "score": 0.5}],
    }
    harness.run_fanout(approved, dedupe=matches)
    view = harness.request(approved, KEYS[2])
    assert (view["dedupe_status"], view["dedupe_warnings"][0]["number"]) == ("MATCHES", 7)
    broker = Mock(
        search_issues=Mock(
            return_value=[
                {
                    "number": 7,
                    "state": "OPEN",
                    "title": "Deploy the worker pool",
                    "url": "https://x",
                }
            ]
        )  # fmt: skip
    )
    github = ChangeRequestGitHubHandler(
        harness.settings.model_copy(update={"run_root": tmp_path / "runs"}), broker=broker
    )
    found = github._dedupe(
        {
            "project": "falcone-serverless",
            "target_repository": "gntik-ai/falcone-charts",
            "proposal": {"title": "Deploy the worker pool with the chart"},
        }
    )
    assert found["dedupe"]["status"] == "MATCHES"
    assert broker.search_issues.call_args.args[0] == "gntik-ai/falcone-charts"


def test_the_publisher_dry_runs_and_adopts_a_cross_repository_request(
    harness: FanoutHarness,
) -> None:
    approved = harness.approved()
    harness.run_fanout(approved)
    harness.publish(approved, KEYS[0], 101)
    harness.publish(approved, KEYS[1], 55)
    view = harness.approve_request(approved, KEYS[2])
    client = Mock(request=Mock(return_value=view))
    plan = verified_plan(view)
    assert plan.repository == "gntik-ai/falcone-charts"
    assert "- Depends on: gntik-ai/falcone#55" in plan.body
    dry = publish_change_request(client, view["id"], dry_run=True, gh=Mock())
    assert dry["status"] == "DRY_RUN" and "--repo gntik-ai/falcone-charts" in dry["gh_command"]
    # An issue already carrying exactly this publication is adopted, never filed twice.
    existing = {"number": 9, "title": plan.title, "body": plan.body, "url":
                "https://github.com/gntik-ai/falcone-charts/issues/9", "author": {}}  # fmt: skip
    calls: list[list[str]] = []

    def gh(arguments: list[str]) -> str:
        calls.append(arguments)
        return json.dumps([existing])

    published = {**view, "status": "PUBLISHED"}
    client.request = Mock(side_effect=[view, published])
    result = publish_change_request(client, view["id"], gh=gh)
    assert result["adopted"] is True
    assert not any("create" in call for call in calls)
    recorded = client.request.call_args_list[1]
    assert recorded.args[2]["issue_url"].endswith("/falcone-charts/issues/9")


# Gating and re-render -------------------------------------------------------------------


def test_a_dependent_is_refused_until_its_predecessors_are_published(
    harness: FanoutHarness,
) -> None:
    approved = harness.approved()
    harness.run_fanout(approved)
    dependent = harness.request(approved, KEYS[1])
    old_binding = dependent["approval_binding"]
    with pytest.raises(DependenciesAwaitingError, match="awaiting_dependencies RUN-01.t1"):
        harness.change_requests.approve_sync(
            UUID(dependent["id"]),
            old_binding,
            approval_phrase("falcone-serverless", old_binding),
            OPERATOR,
        )
    published = harness.publish(approved, KEYS[0], 101)
    # In the same transaction: the dependents of RUN-01.t1 are re-rendered.
    assert published["rerendered_dependents"] == [dependent["id"]]
    current = harness.request(approved, KEYS[1])
    assert "- Depends on: gntik-ai/falcone-serverless#101" in current["issue_body"]
    assert current["decomposition_context"]["depends_on"] == ["gntik-ai/falcone-serverless#101"]
    assert current["approval_binding"] != old_binding
    assert current["awaiting_dependencies"] == [] and current["approve_command"]
    # The content seen before the re-render can no longer be approved.
    with pytest.raises(ValueError, match="binding changed"):
        harness.change_requests.approve_sync(
            UUID(dependent["id"]),
            old_binding,
            approval_phrase("falcone-serverless", old_binding),
            OPERATOR,
        )
    # The backlog import reads the line as a cross-repository dependency, and only it.
    from test_new_216_dependency_parsing import issue

    from engineering_control.engineering.backlog import _dependencies

    body = current["issue_body"]
    assert _dependencies(issue(55, body), body.lower()) == ("gntik-ai/falcone-serverless#101",)
    approved_dependent = harness.approve_request(approved, KEYS[1])
    assert approved_dependent["status"] == "APPROVED"
    # The approved body is final: it is the published one, re-verified by the publisher.
    assert verified_plan(approved_dependent).body == current["issue_body"]
    for key in (KEYS[2], KEYS[3]):
        assert harness.request(approved, key)["awaiting_dependencies"] == ["API-01.t1"]


def test_a_candidate_persisted_after_its_predecessor_is_published_names_it(
    harness: FanoutHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fanout_activities, "FANOUT_BATCH", 1)
    approved = harness.approved()
    harness.run_fanout(approved, max_items=1)
    harness.publish(approved, KEYS[0], 101)
    harness.run_fanout(approved, max_items=1, run_id="run-2")
    view = harness.request(approved, KEYS[1])
    assert "- Depends on: gntik-ai/falcone-serverless#101" in view["issue_body"]
    assert view["awaiting_dependencies"] == []


def test_a_stale_body_is_re_rendered_and_never_approved(harness: FanoutHarness) -> None:
    approved = harness.approved()
    harness.run_fanout(approved)
    harness.publish(approved, KEYS[0], 101)
    identity = harness.request_id(approved, KEYS[1])
    # A publication that raced the persist: the stored body lacks the published issue.
    with harness.change_requests.sessions.begin() as session:
        row = session.get(ChangeRequest, identity)
        assert row is not None
        harness.change_requests._rerender_dependencies(row, [])
    stale = harness.request(approved, KEYS[1])
    assert "Depends on:" not in stale["issue_body"]
    binding = stale["approval_binding"]
    with pytest.raises(ValueError, match="DECOMPOSITION_DEPENDENCIES_STALE"):
        harness.change_requests.approve_sync(
            identity, binding, approval_phrase("falcone-serverless", binding), OPERATOR
        )
    repaired = harness.request(approved, KEYS[1])
    assert repaired["status"] == "CANDIDATE"
    assert "- Depends on: gntik-ai/falcone-serverless#101" in repaired["issue_body"]


# Retry ------------------------------------------------------------------------------------


def _broken(context: dict[str, Any]) -> Any:
    return {"decision": "maybe"} if context["item_key"] == KEYS[2] else None


def test_failed_tasks_are_retried_with_the_typed_phrase(harness: FanoutHarness) -> None:
    approved = harness.approved()
    identity = UUID(approved["id"])
    first = harness.run_fanout(
        approved,
        verdict=_broken,
    )
    assert (first["drafted"], first["failed"]) == (3, 1)
    last = harness.repository.get_sync(identity)["fanout"]["last_run"]  # type: ignore[index]
    assert last["failed_total"] == 1
    assert last["failed_items"][0]["item_key"] == "API-01.t2"
    view = harness.repository.get_sync(identity)
    assert view is not None and RETRY_FANOUT_PHRASE in view["fanout_retry_command"]
    binding = approved["approval_binding"]
    with pytest.raises(ValueError, match=RETRY_FANOUT_PHRASE):
        harness.repository.retry_fanout_sync(identity, binding, "RETRY_UI_FANOUT", OPERATOR)
    with pytest.raises(ValueError, match="binding does not match"):
        harness.repository.retry_fanout_sync(identity, "0" * 64, RETRY_FANOUT_PHRASE, OPERATOR)
    marker = harness.repository.claim_fanout_sync(identity)["workflow_id"]
    with pytest.raises(DecompositionFanoutActiveError):
        harness.repository.retry_fanout_sync(identity, binding, RETRY_FANOUT_PHRASE, OPERATOR)
    harness.repository.release_fanout_sync(identity, marker)
    reset = harness.repository.retry_fanout_sync(identity, binding, RETRY_FANOUT_PHRASE, OPERATOR)
    assert reset["reset_count"] == 1 and reset["reset"][0]["task_key"] == KEYS[2]
    again = harness.repository.retry_fanout_sync(identity, binding, RETRY_FANOUT_PHRASE, OPERATOR)
    assert again["reset_count"] == 0  # idempotent
    with harness.repository.sessions() as session:
        events = session.scalars(
            select(DashboardEvent).where(
                DashboardEvent.event_type == "decomposition_fanout.retry_requested"
            )
        ).all()
        assert len(events) == 2
    second = harness.run_fanout(approved, run_id="run-3")
    assert (second["drafted"], second["processed"]) == (1, 1)
    assert harness.request(approved, KEYS[2])["status"] == "CANDIDATE"


# Ready and bulk approval ------------------------------------------------------------------


def test_approve_ready_approves_exactly_the_listed_ready_set(harness: FanoutHarness) -> None:
    approved = harness.approved()
    identity = UUID(approved["id"])
    harness.run_fanout(
        approved, verdict=lambda context: REVISE if context["item_key"] == KEYS[3] else None
    )
    ready = harness.repository.ready_sync(identity)
    assert [entry["task_key"] for entry in ready["ready"]] == [KEYS[0]]
    assert [entry["task_key"] for entry in ready["awaiting_dependencies"]] == list(KEYS[1:])
    assert ready["counts"] == {"CANDIDATE": 4}
    binding = ready["ready_binding"]
    assert ready["approve_ready_command"].endswith(f"{approved['id']} {binding}")
    with pytest.raises(ValueError, match="DECOMPOSITION_READY_SET_CHANGED"):
        harness.repository.approve_ready_sync(identity, "0" * 64, OPERATOR)
    with pytest.raises(ValueError, match="allowlisted operator"):
        harness.repository.approve_ready_sync(identity, binding, "someone")
    done = harness.repository.approve_ready_sync(identity, binding, OPERATOR)
    assert done["approved_count"] == 1
    assert done["approved"][0]["publish_command"].startswith("engineering-ctl change-publish ")
    first = harness.request(approved, KEYS[0])
    assert first["status"] == "APPROVED" and first["decided_by"] == OPERATOR
    # The bulk approval is change-approve's: the publisher verifies its approval record.
    assert verified_plan(first).repository == "gntik-ai/falcone-serverless"
    # The same binding cannot approve anything else later.
    with pytest.raises(ValueError, match="no request"):
        harness.repository.approve_ready_sync(identity, binding, OPERATOR)
    harness.publish(approved, KEYS[0], 101)
    nxt = harness.repository.ready_sync(identity)
    assert [entry["task_key"] for entry in nxt["ready"]] == [KEYS[1]]
    harness.repository.approve_ready_sync(identity, nxt["ready_binding"], OPERATOR)
    harness.publish(approved, KEYS[1], 55)
    last = harness.repository.ready_sync(identity)
    # A reviewer's unresolved findings are approved one by one, never in bulk.
    assert [entry["task_key"] for entry in last["ready"]] == [KEYS[2]]
    assert [(e["task_key"], e["reason"]) for e in last["needs_individual_approval"]] == [
        (KEYS[3], "REVIEW_UNRESOLVED")
    ]
    # A re-render between listing and approving changes the binding: refused.
    stale_binding = last["ready_binding"]
    with harness.change_requests.sessions.begin() as session:
        row = session.get(ChangeRequest, harness.request_id(approved, KEYS[2]))
        assert row is not None
        row.dedupe_json = {**row.dedupe_json, "import_risk": {**row.dedupe_json["import_risk"],
                                                              "escalated": True}}  # fmt: skip
    escalated = harness.repository.ready_sync(identity)
    assert escalated["ready"] == []
    assert escalated["needs_individual_approval"][0]["reason"] == "IMPORT_RISK_ESCALATED"
    with pytest.raises(ValueError, match="no request"):
        harness.repository.approve_ready_sync(identity, stale_binding, OPERATOR)


def test_ready_requires_an_approved_decomposition(harness: FanoutHarness) -> None:
    created = harness.create()
    with pytest.raises(ValueError, match="not APPROVED"):
        harness.repository.ready_sync(UUID(created["id"]))


# Story amendments -------------------------------------------------------------------------


def _amended(plan: dict[str, Any]) -> None:
    run = next(story for story in plan["stories"] if story["story_id"] == "RUN-01")
    run["story_amendment"] = {
        "reason": "The trap criterion needs a typed error.",
        "sources": ["design §4"],
        "changes": [
            {
                "section": "acceptance_criteria",
                "action": "REPLACE",
                "target": "RUN-01#ac2",
                "text": "A trap returns a typed error.",
            },
            {"section": "notes", "action": "ADD", "target": None, "text": "Tasks follow."},
        ],
    }


def test_story_amendments_are_diffs_the_operator_publishes(
    harness: FanoutHarness, tmp_path: Path
) -> None:
    approved = harness.approved(_amended)
    shown = harness.repository.story_amendments_sync(UUID(approved["id"]))
    [amendment] = shown["amendments"]
    assert (amendment["story_id"], amendment["issue"]) == ("RUN-01", 5)
    assert amendment["repository"] == "gntik-ai/falcone-serverless"
    assert "+- [ ] A trap returns a typed error." in amendment["diff"]
    assert amendment["diff"].count("\n-- [ ] ") == 1
    assert "+- Notes (ADD): Tasks follow." in amendment["diff"]
    assert amendment["manual"] == []
    body = amendment["amended_body"]
    assert "<!-- falcone-serverless:RUN-01 -->" in body  # the story keeps its marker
    assert f"<!-- hermes-decomposition-amendment plan={approved['id']} " in body
    client = Mock(request=Mock(return_value=shown))
    output = tmp_path / "amendments"
    output.mkdir()
    edits = story_amendment_edits(client, approved["id"], output_dir=output)
    [entry] = edits["amendments"]
    path = Path(entry["body_file"])
    assert path.read_text() == body and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert entry["command"] == (
        f"gh api -X PATCH repos/gntik-ai/falcone-serverless/issues/5 -F body=@{path}"
    )
    assert entry["current_state"] == "UNVERIFIED"
    text = render_story_amendment_edits(edits)
    assert "Hermes never edits an issue" in text and entry["command"] in text
    assert amendment["diff"].rstrip("\n") in text
    # Repeating it keeps the identical file; a file with other content is refused.
    story_amendment_edits(client, approved["id"], output_dir=output)
    path.write_text("other")
    with pytest.raises(ValueError, match="exists with other content"):
        story_amendment_edits(client, approved["id"], output_dir=output)
    path.unlink()
    # --verify-current reads the issue (the operator's gh, read-only) and withholds the
    # command for a drifted or already amended body.
    inventory = harness.repository.get_sync(UUID(approved["id"]))
    assert inventory is not None
    calls: list[list[str]] = []
    for current, state in (("drifted body", "DRIFTED"), (body, "ALREADY_APPLIED")):

        def gh(arguments: list[str], current: str = current) -> str:
            calls.append(arguments)
            return current + "\n"

        [checked] = story_amendment_edits(
            client, approved["id"], output_dir=output, verify_current=True, gh=gh
        )["amendments"]
        assert (checked["current_state"], checked["command"]) == (state, None)
    assert calls[0] == [
        "api", "repos/gntik-ai/falcone-serverless/issues/5", "--jq", ".body"
    ]  # fmt: skip
    tampered = {**shown, "amendments": [{**amendment, "amended_body": body + "x"}]}
    with pytest.raises(ValueError, match="does not match its hash"):
        story_amendment_edits(Mock(request=Mock(return_value=tampered)), approved["id"],
                              output_dir=output)  # fmt: skip


def test_an_amendment_applies_in_place_and_lists_what_it_cannot_place() -> None:
    body = "\n".join(
        [
            "<!-- falcone-serverless:API-01 -->",
            "## Acceptance criteria",
            "",
            "- [ ] First.",
            "- [x] Second.",
            "",
            "## Planning",
            "",
            "- Depends on: RUN-01, RUN-02",
            "",
            "## Requirements",
            "",
            "- **FR-API-01:** statement.",
            "- **FR-API-02:** other.",
        ]
    )
    amendment = {
        "reason": "Align with the design.",
        "sources": ["design §2"],
        "changes": [
            {"section": "acceptance_criteria", "action": "REMOVE", "target": "API-01#ac1",
             "text": None},
            {"section": "acceptance_criteria", "action": "REPLACE", "target": "API-01#ac2",
             "text": "Second, typed."},
            {"section": "acceptance_criteria", "action": "ADD", "target": "API-01#ac3",
             "text": "Third."},
            {"section": "dependencies", "action": "REPLACE", "target": "RUN-02",
             "text": "SEC-01"},
            {"section": "requirements", "action": "REPLACE", "target": "FR-API-02",
             "text": "changed."},
            {"section": "requirements", "action": "REMOVE", "target": "FR-API-09",
             "text": None},
            {"section": "scope", "action": "ADD", "target": None, "text": "No CLI."},
        ],
    }  # fmt: skip
    amended, manual = amended_story_body(
        body, "API-01", amendment, decomposition_id="d", plan_sha256="s"
    )
    lines = amended.split("\n")
    assert "- [ ] First." not in lines
    assert lines[3:5] == ["- [x] Second, typed.", "- [ ] Third."]
    assert "- Depends on: RUN-01, SEC-01" in lines
    assert "- **FR-API-02:** changed." in lines and "- **FR-API-01:** statement." in lines
    assert "- Scope (ADD): No CLI." in lines
    assert "- Requirements (REMOVE FR-API-09): (removed)" in lines
    assert manual == [{"section": "requirements", "action": "REMOVE", "target": "FR-API-09"}]
    again, _ = amended_story_body(body, "API-01", amendment, decomposition_id="d", plan_sha256="s")
    assert again == amended  # deterministic


# API and CLI ------------------------------------------------------------------------------


@pytest.fixture
def api(harness: FanoutHarness) -> tuple[TestClient, Mock]:
    fake = Mock()
    fake.start_decomposition_fanout = AsyncMock(
        side_effect=lambda identity, max_items, replace, workflow_id: OperationAccepted(
            workflow_id=workflow_id, run_id="fanout-run"
        )
    )
    fake.ui_fanout_state = AsyncMock(return_value=("CLOSED", None))
    app = create_app(
        settings=harness.settings,
        service=fake,
        token=TOKEN,
        operations_repository=Mock(),
        command_bus=Mock(dispatch_pending=AsyncMock(return_value=False)),
        decomposition_repository=harness.repository,
        change_request_repository=harness.change_requests,
    )
    return TestClient(app), fake


def test_the_api_starts_retries_lists_and_bulk_approves(
    harness: FanoutHarness, api: tuple[TestClient, Mock]
) -> None:
    client, fake = api
    created = harness.create()
    from new_234_plan_fixtures import drive

    binding = drive(harness, created["id"])["approval_binding"]
    route = f"/v1/backlog-decompositions/{created['id']}"
    with client:
        response = client.post(
            f"{route}/approve",
            json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE,
                  "max_items": 500},
            headers=CLI,
        )  # fmt: skip
        assert response.status_code == 422  # above the hard limit (400)
        response = client.post(
            f"{route}/approve",
            json={"approval_binding": binding, "typed_confirmation": APPROVAL_PHRASE,
                  "max_items": 120, "replace": True},
            headers=CLI,
        )  # fmt: skip
        assert response.status_code == 200, response.text
        fanout = response.json()["fanout"]
        assert (fanout["max_items"], fanout["replace"], fanout["joined"]) == (120, True, False)
        fake.start_decomposition_fanout.assert_awaited_once_with(
            UUID(created["id"]), 120, True, f"backlog-decomposition-fanout-{created['id']}-1"
        )
        # The workflow ended without releasing its marker: a retry releases it and starts.
        retry = client.post(
            f"{route}/fanout-retry",
            json={"approval_binding": binding, "typed_confirmation": RETRY_FANOUT_PHRASE},
            headers=CLI,
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["fanout"]["workflow_id"].endswith("-2")
        assert retry.json()["fanout"]["max_items"] == 250
        fake.ui_fanout_state.return_value = ("RUNNING", None)
        busy = client.post(
            f"{route}/fanout-retry",
            json={"approval_binding": binding, "typed_confirmation": RETRY_FANOUT_PHRASE},
            headers=CLI,
        )
        assert busy.status_code == 409 and "running" in busy.text
        wrong = client.post(
            f"{route}/fanout-retry",
            json={"approval_binding": binding, "typed_confirmation": "RETRY_UI_FANOUT"},
            headers=CLI,
        )
        assert wrong.status_code == 422
        harness.repository.release_fanout_sync(
            UUID(created["id"]), f"backlog-decomposition-fanout-{created['id']}-2"
        )
        decomposition = harness.repository.get_sync(UUID(created["id"]))
        assert decomposition is not None
        harness.run_fanout(decomposition)
        ready = client.get(f"{route}/ready", headers=CLI)
        assert ready.status_code == 200
        listed = ready.json()
        assert [entry["task_key"] for entry in listed["ready"]] == [KEYS[0]]
        dependent = harness.request(decomposition, KEYS[1])
        refused = client.post(
            f"/v1/change-requests/{dependent['id']}/approve",
            json={
                "approval_binding": dependent["approval_binding"],
                "typed_confirmation": approval_phrase(
                    "falcone-serverless", dependent["approval_binding"]
                ),
            },
            headers=CLI,
        )
        assert refused.status_code == 409 and "awaiting_dependencies" in refused.text
        bulk = client.post(
            f"{route}/approve-ready", json={"ready_binding": listed["ready_binding"]}, headers=CLI
        )
        assert bulk.status_code == 200 and bulk.json()["approved_count"] == 1
        again = client.post(
            f"{route}/approve-ready", json={"ready_binding": listed["ready_binding"]}, headers=CLI
        )
        assert again.status_code == 409
        amendments = client.get(f"{route}/story-amendments", headers=CLI)
        assert amendments.status_code == 200 and amendments.json()["amendments"] == []
        assert (
            client.get(
                "/v1/backlog-decompositions/11111111-1111-4111-8111-111111111111/ready", headers=CLI
            ).status_code
            == 404
        )


def test_cli_fanout_commands_use_only_the_decomposition_routes() -> None:
    from engineering_control.cli import dispatch, parser

    identifier = "11111111-1111-4111-8111-111111111111"
    route = f"/v1/backlog-decompositions/{identifier}"
    cases = {
        ("backlog-decomposition-approve", identifier, "b" * 64, APPROVAL_PHRASE, "--max-items",
         "120", "--replace"): (
            "POST",
            f"{route}/approve",
            {"approval_binding": "b" * 64, "typed_confirmation": APPROVAL_PHRASE,
             "replace": True, "max_items": 120},
        ),
        ("backlog-decomposition-fanout-retry", identifier, "b" * 64, RETRY_FANOUT_PHRASE): (
            "POST",
            f"{route}/fanout-retry",
            {"approval_binding": "b" * 64, "typed_confirmation": RETRY_FANOUT_PHRASE,
             "replace": False},
        ),
        ("backlog-decomposition-ready", identifier): ("GET", f"{route}/ready", None),
        ("backlog-decomposition-approve-ready", identifier, "c" * 64): (
            "POST",
            f"{route}/approve-ready",
            {"ready_binding": "c" * 64},
        ),
    }  # fmt: skip
    for argv, expected in cases.items():
        client = Mock()
        dispatch(client, parser().parse_args(list(argv)))
        method, path, *payload = client.request.call_args.args
        assert (method, path, payload[0] if payload else None) == expected, argv
    with pytest.raises(SystemExit):
        parser().parse_args(
            ["backlog-decomposition-approve", identifier, "b" * 64, APPROVAL_PHRASE,
             "--max-items", "401"]
        )  # fmt: skip


# Review: global lock order, atomic lineage dedupe, fenced finalization --------------------


def test_every_path_locks_its_change_requests_once_in_id_order(
    harness: FanoutHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadlock of review 1 (persisting a dependent locks a predecessor, then waits for
    another, while publishing that other holds it and waits for the first) needs a
    transaction that locks change requests twice. Every path takes exactly one ordered
    lock, and a publication locks only the published request."""
    original = ChangeRequestRepository._lock_requests
    calls: list[tuple[int, list[UUID]]] = []

    def recording(session: Any, identities: Any) -> Any:
        ordered = sorted(set(identities))
        calls.append((id(session), ordered))
        return original(session, ordered)

    monkeypatch.setattr(ChangeRequestRepository, "_lock_requests", staticmethod(recording))
    approved = harness.approved()
    harness.run_fanout(approved)
    ids = {key: harness.request_id(approved, key) for key in KEYS}
    # Persisting API-01.t2 locked it and its predecessor API-01.t1 in one statement.
    assert sorted([ids[KEYS[1]], ids[KEYS[2]]]) in [locked for _session, locked in calls]
    calls.clear()
    published = harness.publish(approved, KEYS[0], 101)
    assert published["rerendered_dependents"] == [str(ids[KEYS[1]])]
    # approve: [RUN-01.t1]; publish: [RUN-01.t1]; deferred re-render: dependent + predecessor.
    assert [locked for _session, locked in calls] == [
        [ids[KEYS[0]]],
        [ids[KEYS[0]]],
        sorted([ids[KEYS[0]], ids[KEYS[1]]]),
    ]
    calls.clear()
    ready = harness.repository.ready_sync(UUID(approved["id"]))
    harness.repository.approve_ready_sync(UUID(approved["id"]), ready["ready_binding"], OPERATOR)
    assert [locked for _session, locked in calls] == [sorted(ids.values())]
    sessions = [session for session, _locked in calls]
    assert len(sessions) == len(set(sessions))
    # A second change-request lock in one transaction is refused outright.
    with harness.change_requests.sessions.begin() as session:
        original(session, [ids[KEYS[0]]])
        with pytest.raises(RuntimeError, match="once"):
            original(session, [ids[KEYS[1]]])


def test_a_publication_survives_a_failed_re_render_which_is_deferred(
    harness: FanoutHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = harness.approved()
    harness.run_fanout(approved)
    dependent = harness.request(approved, KEYS[1])

    def unavailable(_ids: Any) -> Any:
        raise OperationalError("SELECT", {}, Exception("deadlock detected"))

    monkeypatch.setattr(harness.change_requests, "refresh_dependencies_sync", unavailable)
    published = harness.publish(approved, KEYS[0], 101)
    # The operator's issue exists: the publication is recorded whatever the re-render does.
    assert published["status"] == "PUBLISHED"
    assert published["rerender_deferred"] == [dependent["id"]]
    assert harness.request(approved, KEYS[0])["issue_number"] == 101
    monkeypatch.undo()
    stale = harness.request(approved, KEYS[1])
    assert "Depends on:" not in stale["issue_body"]
    binding = stale["approval_binding"]
    with pytest.raises(ValueError, match="DECOMPOSITION_DEPENDENCIES_STALE"):
        harness.change_requests.approve_sync(
            UUID(stale["id"]), binding, approval_phrase("falcone-serverless", binding), OPERATOR
        )
    assert (
        "- Depends on: gntik-ai/falcone-serverless#101"
        in harness.request(approved, KEYS[1])["issue_body"]
    )
    assert harness.approve_request(approved, KEYS[1])["status"] == "APPROVED"


def test_one_active_request_per_lineage_is_a_database_constraint(
    harness: FanoutHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = harness.approved()
    harness.run_fanout(first)
    # Two decompositions both missing each other's request (review 2's race): the second
    # insert fails on migration 0023's index and is reported as superseded.
    monkeypatch.setattr(change_request_db, "LINEAGE_BLOCKING_STATUSES", frozenset())
    second = harness.approved(_retitled)
    result = harness.run_fanout(second)
    assert (result["drafted"], result["skipped"]) == (0, 4)
    assert len(harness.rows()) == 4
    superseded = harness.repository.get_sync(UUID(second["id"]))["fanout"]["superseded"]  # type: ignore[index]
    assert superseded[KEYS[0]]["existing"][0]["change_request_id"] == str(
        harness.request_id(first, KEYS[0])
    )
    with (
        harness.change_requests.sessions.begin() as session,
        pytest.raises(IntegrityError),
    ):
        row = session.get(ChangeRequest, harness.request_id(first, KEYS[0]))
        assert row is not None
        session.add(
            ChangeRequest(
                id=UUID("22222222-2222-4222-8222-222222222222"),
                request_key="dcp-duplicate",
                project=row.project,
                status="CANDIDATE",
                revision=1,
                description_redacted=row.description_redacted,
                description_sha256=row.description_sha256,
                redacted=False,
                proposal_json={},
                review_json={},
                dedupe_json={},
                target_repository=row.target_repository,
                requested_by=OPERATOR,
                decomposition_lineage=row.decomposition_lineage,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
        session.flush()


def test_replace_rejects_the_unpublished_match_it_replaces(harness: FanoutHarness) -> None:
    first = harness.approved()
    harness.run_fanout(first)
    harness.publish(first, KEYS[0], 101)
    second = harness.approved(_retitled)
    replaced = harness.run_fanout(second, replace=True)
    assert (replaced["drafted"], replaced["skipped"]) == (3, 1)
    for key in KEYS[1:]:
        old = harness.request(first, key)
        assert old["status"] == "REJECTED" and old["decided_by"] == OPERATOR
        assert second["id"] in str(old["decision_reason"])
    assert harness.request(first, KEYS[0])["status"] == "PUBLISHED"
    # A revision cannot revive a request whose lineage another active request holds.
    with harness.change_requests.sessions.begin() as session:
        row = session.get(ChangeRequest, harness.request_id(first, KEYS[1]))
        assert row is not None
        row.status = "FAILED"
    with pytest.raises(ValueError, match="SUPERSEDED_BY_EXISTING"):
        harness.change_requests.revise_sync(
            harness.request_id(first, KEYS[1]), "try again", OPERATOR
        )


def test_a_superseded_fanout_cannot_finalize_or_notify(harness: FanoutHarness) -> None:
    approved = harness.approved()
    identity = UUID(approved["id"])
    stale = harness.repository.claim_fanout_sync(identity)["workflow_id"]
    harness.repository.release_fanout_sync(identity, stale)
    current = harness.repository.claim_fanout_sync(identity)["workflow_id"]
    finalized = harness.fanout._finalize(
        {
            "decomposition_id": approved["id"],
            "project": "falcone-serverless",
            "drafted": 9,
            "orchestration": {"workflow_id": stale, "workflow_run_id": "old"},
        }
    )
    assert finalized["status"] == "SUPERSEDED"
    view = harness.repository.get_sync(identity)
    assert view is not None and "last_run" not in view["fanout"]
    assert view["fanout_workflow_id"] == current  # the current owner keeps its marker

    def done_events() -> list[Any]:
        with harness.repository.sessions() as session:
            return [
                row
                for row in session.scalars(select(NotificationOutbox)).all()
                if row.payload_json.get("event") == "DECOMPOSITION_FANOUT_DONE"
            ]

    assert done_events() == []
    done = harness.fanout._finalize(
        {
            "decomposition_id": approved["id"],
            "project": "falcone-serverless",
            "orchestration": {"workflow_id": current, "workflow_run_id": "new"},
        }
    )
    assert done["status"] == "FINALIZED"
    view = harness.repository.get_sync(identity)
    assert view is not None and view["fanout_workflow_id"] is None
    assert len(done_events()) == 1


def test_migration_0023_enforces_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration-0023.sqlite3'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    name = "uq_change_requests_active_decomposition_lineage"

    def index() -> dict[str, Any] | None:
        engine = create_engine(database_url)
        found = {item["name"]: item for item in inspect(engine).get_indexes("change_requests")}
        engine.dispose()
        return found.get(name)

    command.upgrade(config, "head")  # bootstrap: 0001 creates the current models
    bootstrapped = index()
    assert bootstrapped is not None and bootstrapped["unique"]
    command.downgrade(config, "0022")
    assert index() is None
    command.upgrade(config, "0023")  # the explicit path
    created = index()
    assert created is not None and created["unique"]
    predicate = str(created["dialect_options"]["sqlite_where"])
    assert "decomposition_lineage IS NOT NULL" in predicate
    assert all(status in predicate for status in ("DRAFTING", "CANDIDATE", "APPROVED", "PUBLISHED"))
    engine = create_engine(database_url)
    row = (
        "INSERT INTO change_requests (id, request_key, project, status, revision, "
        "description_redacted, description_sha256, redacted, proposal_json, review_json, "
        "dedupe_json, target_repository, requested_by, decomposition_lineage, created_at, "
        "updated_at, decision_sequence) VALUES (:id, :key, 'falcone-serverless', :status, "
        "1, 'd', :sha, 0, '{}', "
        "'{}', '{}', 'gntik-ai/falcone', 'cli:engineering-ctl', 'falcone-serverless:A-01:A-01.t1', "
        "'2026-10-22', '2026-10-22', 0)"
    )
    with engine.begin() as connection:
        connection.execute(
            text(row), {"id": "a" * 32, "key": "k1", "status": "CANDIDATE", "sha": "0" * 64}
        )
        connection.execute(
            text(row), {"id": "b" * 32, "key": "k2", "status": "REJECTED", "sha": "0" * 64}
        )
    # The downgrade refuses while an active decomposition request exists.
    with pytest.raises(RuntimeError, match="revision 0023 downgrade refuses"):
        command.downgrade(config, "0022")
    # Duplicates refuse the upgrade; an incompatible index of the same name is refused.
    with engine.begin() as connection:
        connection.execute(text("UPDATE change_requests SET status = 'REJECTED'"))
    command.downgrade(config, "0022")
    with engine.begin() as connection:
        connection.execute(text("UPDATE change_requests SET status = 'CANDIDATE'"))
    with pytest.raises(RuntimeError, match="refuses duplicate active decomposition requests"):
        command.upgrade(config, "0023")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE change_requests SET status = 'REJECTED' WHERE request_key = 'k2'")
        )
        connection.execute(text(f"CREATE INDEX {name} ON change_requests (decomposition_lineage)"))
    with pytest.raises(RuntimeError, match="incompatible decomposition lineage index"):
        command.upgrade(config, "0023")
    with engine.begin() as connection:
        connection.execute(text(f"DROP INDEX {name}"))
        connection.execute(
            text(f"CREATE UNIQUE INDEX {name} ON change_requests (decomposition_lineage) "
                 "WHERE status IN ('DRAFTING', 'CANDIDATE')")
        )  # fmt: skip
    with pytest.raises(RuntimeError, match="incompatible decomposition lineage predicate"):
        command.upgrade(config, "0023")
    engine.dispose()


_FULL = (
    "decomposition_lineage IS NOT NULL AND "
    "status IN ('DRAFTING', 'CANDIDATE', 'APPROVED', 'PUBLISHED')"
)


@pytest.mark.parametrize(
    "predicate",
    [
        f"{_FULL} AND revision > 1",
        f"{_FULL} AND 0",
        f"({_FULL}) OR status = 'REJECTED'",
        "status IN ('DRAFTING', 'CANDIDATE', 'APPROVED', 'PUBLISHED')",
        _FULL.replace("'PUBLISHED'", "'PUBLISHED', 'FAILED'"),
        _FULL.lower(),
        _FULL.replace("'CANDIDATE'", "'Candidate'"),
    ],
)
def test_migration_0023_refuses_an_index_that_enforces_less(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, predicate: str
) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration-0023-predicate.sqlite3'}"
    monkeypatch.setenv("ENGINEERING_DATABASE_URL", database_url)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    name = "uq_change_requests_active_decomposition_lineage"
    command.upgrade(config, "head")
    command.stamp(config, "0022")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(f"DROP INDEX {name}"))
        connection.execute(
            text(
                f"CREATE UNIQUE INDEX {name} ON change_requests (decomposition_lineage) "
                f"WHERE {predicate}"
            )
        )
    with pytest.raises(RuntimeError, match="incompatible decomposition lineage predicate"):
        command.upgrade(config, "0023")
    engine.dispose()


def test_migration_0023_accepts_exactly_the_postgresql_rendering() -> None:
    import importlib.util

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/0023_decomposition_lineage_unique.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0023", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rendered = (
        "((decomposition_lineage IS NOT NULL) AND ((status)::text = ANY ((ARRAY["
        "'DRAFTING'::character varying, 'CANDIDATE'::character varying, "
        "'APPROVED'::character varying, 'PUBLISHED'::character varying])::text[])))"
    )
    assert module._predicate_is_compatible(rendered)
    assert module._predicate_is_compatible(_FULL)
    for extra in (" AND (revision > 1)", " AND false", " AND (NOT (status IS NULL))"):
        assert not module._predicate_is_compatible(f"({rendered[1:-1]}{extra})")
    assert not module._predicate_is_compatible(None)
    # Literals are compared byte-exact: lowercase or mixed-case statuses cover nothing.
    assert not module._predicate_is_compatible(_FULL.lower())
    assert not module._predicate_is_compatible(_FULL.replace("'DRAFTING'", "'Drafting'"))
    assert not module._predicate_is_compatible(rendered.replace("'APPROVED'", "'approved'"))
    assert not module._predicate_is_compatible(_FULL.replace("'DRAFTING'", "'DRAFTING '"))
    assert not module._predicate_is_compatible(_FULL.replace("'DRAFTING'", "'DRAFT''ING'"))
    # Keywords and identifiers fold; the exact predicate is accepted as SQLite stores it.
    assert module._predicate_is_compatible(
        " decomposition_lineage is not null and STATUS in "
        "('PUBLISHED', 'APPROVED', 'CANDIDATE', 'DRAFTING')"
    )
