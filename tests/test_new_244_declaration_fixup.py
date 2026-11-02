"""NEW-244: a targeted declaration fix-up instead of a full redraft.

When every assembly problem an approved epic batch owns is a declaration problem (its
tasks' depends_on and interfaces), the plan workflow asks the drafter once for a fix-up
that may change only those fields (decomposition-declaration-fixup-v1). Hermes refuses any
other change, re-runs the draft checks, and stores the result as a version derived from
the reviewed one: the review hash is the base's, and replaying the recorded changes on the
base must reproduce the content exactly. A refused fix-up falls back to the full redraft;
a history from before the change replays unchanged.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_234_decomposition_fixtures import api_epic, epic_output, frame, run_epic
from new_234_plan_fixtures import FRAME_CONTENT_SHA, PlanHarness, agent_result, draft_for, drive
from pre_declaration_fixup_decomposition_workflow import (
    PreDeclarationFixupDecompositionPlanWorkflow,
)
from sqlalchemy import update
from test_new_234_plan_workflow import PART_LOOP, WorkflowHarness

from engineering_control.activities import backlog_decomposition as activities
from engineering_control.db.backlog_decompositions import reviewed_version
from engineering_control.db.models import BacklogDecomposition
from engineering_control.engineering.decomposition import (
    DECLARATION_CODES,
    DecompositionRejected,
    PlanProblem,
    apply_declaration_changes,
    declaration_changes,
    declaration_only,
    normalize_epic_content,
    part_content_sha256,
)
from engineering_control.workflows.definitions import BacklogDecompositionPlanWorkflow
from engineering_control.workflows.worker_registry import ROLE_TASK_QUEUES

RUN_T1 = "falcone-serverless:RUN-01.t1"
API_T1 = "falcone-serverless:API-01.t1"
FIXUP_LOOP = [
    "decomposition.prepare_part_fixup",
    "agent.record_start",
    "decomposition.draft",
    "agent.persist_result",
    "decomposition.record_part_fixup",
]
_TEMPORAL = shutil.which("temporal") is not None
needs_temporal = pytest.mark.skipif(not _TEMPORAL, reason="requires the `temporal` CLI on PATH")


def _normalized(content: dict[str, Any]) -> dict[str, Any]:
    return normalize_epic_content(content, frame()["contracts"])[0]


def _run_task(content: dict[str, Any]) -> dict[str, Any]:
    task: dict[str, Any] = content["stories"][0]["tasks"][0]
    return task


# The diff and its replay ------------------------------------------------------------------


def test_the_workflow_and_the_checks_name_the_same_declaration_codes() -> None:
    assert BacklogDecompositionPlanWorkflow._DECLARATION_CODES == DECLARATION_CODES
    assert declaration_only([{"code": "DEPENDENCY_UNRESOLVED"}, {"code": "DEPENDENCY_CYCLE"}])
    assert not declaration_only([{"code": "DEPENDENCY_UNRESOLVED"}, {"code": "COVERS_UNKNOWN"}])
    assert not declaration_only([])


def test_only_declarations_and_dependency_amendments_may_change() -> None:
    base = api_epic()
    fixed = copy.deepcopy(base)
    fixed["stories"][0]["tasks"][1]["depends_on"] = ["contract:ctr-functions-api", API_T1]
    fixed["stories"][0]["tasks"][0]["interfaces"]["consumes"].append("ctr-functions-api")
    fixed["stories"][1]["story_amendment"] = {
        "reason": "API-02 also needs the worker.",
        "sources": ["API-02#ac1"],
        "changes": [{"section": "dependencies", "action": "ADD", "target": "RUN-01", "text": None}],
    }
    changes, refusals = declaration_changes(base, fixed)
    assert refusals == []
    assert [(c.get("task") or c.get("story"), c["field"]) for c in changes] == [
        (API_T1, "interfaces.consumes"),
        ("falcone-serverless:API-01.t2", "depends_on"),
        ("API-02", "story_amendment"),
    ]
    assert apply_declaration_changes(base, changes) == fixed
    assert base == api_epic()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["stories"][0]["tasks"][0]["acceptance_criteria"][0].update(criterion="x"),
        lambda c: c["stories"][0]["tasks"][0].update(title="Another title"),
        lambda c: c["stories"][0]["tasks"][0].update(paths=["services/x.mjs"]),
        lambda c: c["stories"][0]["tasks"][0].update(key="falcone-serverless:API-01.t9"),
        lambda c: c["stories"][0]["tasks"].pop(),
        lambda c: c["stories"][0]["tasks"].append(copy.deepcopy(c["stories"][0]["tasks"][0])),
        lambda c: c["stories"].reverse(),
        lambda c: c["stories"][1].update(
            story_amendment={
                "reason": "r",
                "sources": ["s"],
                "changes": [
                    {"section": "notes", "action": "ADD", "target": None, "text": "n"},
                ],
            }
        ),
        lambda c: None,  # no change at all
    ],
)
def test_any_other_change_refuses_the_fixup(mutate: Any) -> None:
    fixed = api_epic()
    mutate(fixed)
    changes, refusals = declaration_changes(api_epic(), fixed)
    assert changes == [] and refusals


def test_a_replay_naming_an_unknown_task_or_field_is_refused() -> None:
    for change in (
        {"task": "falcone-serverless:API-09.t1", "field": "depends_on", "value": []},
        {"task": API_T1, "field": "title", "value": "x"},
        {"story": "API-09", "field": "story_amendment", "value": None},
    ):
        with pytest.raises(ValueError):
            apply_declaration_changes(api_epic(), [change])


# The hash relaxation -----------------------------------------------------------------------


def _store(harness: PlanHarness, identity: str, parts: dict[str, Any]) -> None:
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
        return copy.deepcopy(dict(row.parts_json or {}))


def _derive(entry: dict[str, Any], fixed: dict[str, Any]) -> dict[str, Any]:
    """A fix-up version of the entry's approved version, as record_part_fixup_sync stores
    it (approved)."""
    base = next(v for v in entry["versions"] if v["attempt"] == entry["approved_attempt"])
    changes, refusals = declaration_changes(base["content"], fixed)
    assert refusals == []
    version = {
        "attempt": base["attempt"] + 1,
        "content": fixed,
        "content_sha256": part_content_sha256(fixed),
        "reviewed_sha256": base["reviewed_sha256"],
        "review": copy.deepcopy(base["review"]),
        "fixup": {
            "base_attempt": base["attempt"],
            "base_sha256": base["reviewed_sha256"],
            "changes": changes,
            "problems": [],
        },
    }
    entry["versions"].append(version)
    entry["approved_attempt"] = version["attempt"]
    return version


def _fixed_run(entry: dict[str, Any]) -> dict[str, Any]:
    fixed = copy.deepcopy(entry["versions"][0]["content"])
    _run_task(fixed)["interfaces"]["consumes"].append("ctr-functions-api")
    return fixed


def test_a_derived_version_is_accepted_only_when_its_changes_reproduce_it(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    first = harness.create()
    assert drive(harness, first["id"])["status"] == "CANDIDATE"
    parts = _parts(harness, first["id"])
    run_id = next(key for key, entry in parts["parts"].items() if entry.get("number") == 2)
    entry = parts["parts"][run_id]
    fixed = _fixed_run(entry)
    version = _derive(entry, fixed)
    assert reviewed_version(entry, version)
    _store(harness, first["id"], parts)
    found = harness.repository.approved_content_sync(UUID(first["id"]), run_id)
    assert found["content"] == fixed and found["content_sha256"] == part_content_sha256(fixed)

    def tampered(change: Any) -> dict[str, Any]:
        copied = copy.deepcopy(entry)
        change(copied, copied["versions"][-1])
        return copied

    def retitle(_entry: dict[str, Any], derived: dict[str, Any]) -> None:
        _run_task(derived["content"])["title"] = "Something else entirely"
        derived["content_sha256"] = part_content_sha256(derived["content"])

    def recompute_review(_entry: dict[str, Any], derived: dict[str, Any]) -> None:
        derived["reviewed_sha256"] = derived["content_sha256"]

    def edit_changes(_entry: dict[str, Any], derived: dict[str, Any]) -> None:
        derived["fixup"]["changes"][0]["value"] = []

    def edit_base(item: dict[str, Any], _derived: dict[str, Any]) -> None:
        _run_task(item["versions"][0]["content"])["title"] = "Changed after review"

    def chain(item: dict[str, Any], derived: dict[str, Any]) -> None:
        item["versions"][0]["fixup"] = copy.deepcopy(derived["fixup"])

    def another_review(_entry: dict[str, Any], derived: dict[str, Any]) -> None:
        derived["review"] = {**derived["review"], "summary": "Not the base's review."}

    for change in (retitle, recompute_review, edit_changes, edit_base, chain, another_review):
        copied = tampered(change)
        assert not reviewed_version(copied, copied["versions"][-1]), change.__name__
        _store(harness, first["id"], {**parts, "parts": {**parts["parts"], run_id: copied}})
        with pytest.raises(ValueError, match="no longer matches its review"):
            harness.repository.approved_content_sync(UUID(first["id"]), run_id)
        assert (
            harness.repository.reusable_part_sync(UUID(int=0), "falcone-serverless", run_id) is None
        ), change.__name__


def test_a_reused_fixup_is_adopted_over_its_reviewed_base(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    first = harness.create()
    assert drive(harness, first["id"])["status"] == "CANDIDATE"
    parts = _parts(harness, first["id"])
    run_id = next(key for key, entry in parts["parts"].items() if entry.get("number") == 2)
    fixed = _fixed_run(parts["parts"][run_id])
    _derive(parts["parts"][run_id], fixed)
    _store(harness, first["id"], parts)
    harness.requests.clear()
    second = harness.create()
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    assert harness.requests == []
    adopted = _parts(harness, second["id"])["parts"][run_id]
    assert adopted["approved_attempt"] == 2
    base, derived = adopted["versions"]
    assert base["content"] == parts["parts"][run_id]["versions"][0]["content"]
    assert derived["content"] == fixed and derived["fixup"]["base_attempt"] == 1
    assert reviewed_version(adopted, derived)
    with harness.repository.sessions() as session:
        row = session.get(BacklogDecomposition, UUID(second["id"]))
        assert row is not None
        tasks = {t["key"]: t for s in row.plan_json["stories"] for t in s["tasks"]}
    assert tasks[RUN_T1]["interfaces"]["consumes"] == ["ctr-worker-invoke", "ctr-functions-api"]


# The workflow ------------------------------------------------------------------------------


def _flaky_assembly(
    monkeypatch: pytest.MonkeyPatch, code: str = "DEPENDENCY_UNRESOLVED", times: int = 1
) -> list[int]:
    real = activities.assemble_plan
    calls: list[int] = []

    def flaky(scoped: Any, value: Any, epics: Any) -> dict[str, Any]:
        calls.append(1)
        if len(calls) <= times:
            raise DecompositionRejected(
                "ASSEMBLY_INVALID",
                [PlanProblem(code, "dependency", RUN_T1, "depends on a missing task")],
            )
        return real(scoped, value, epics)

    monkeypatch.setattr(activities, "assemble_plan", flaky)
    return calls


def _fixup_drafter(harness: WorkflowHarness, mutate: Any) -> list[dict[str, Any]]:
    """The drafter: a normal draft, or for a fix-up request the approved RUN batch with
    `mutate` applied."""
    fixups: list[dict[str, Any]] = []

    def draft(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload.get("fixup"), dict):
            return agent_result(payload, draft_for(payload))
        fixups.append(payload)
        assert "targeted fix-up, not a redraft" in payload["draft_request"]["prompt"]
        assert "DEPENDENCY_UNRESOLVED" in payload["draft_request"]["prompt"]
        content = _normalized(run_epic())
        mutate(content)
        output = epic_output(content, payload["part_id"], frame_sha256=FRAME_CONTENT_SHA)
        return agent_result(payload, output)

    harness.responses["decomposition.draft"] = draft
    return fixups


def _markers(harness: WorkflowHarness) -> list[str]:
    return [
        str(event.marker_recorded_event_attributes)
        for event in harness.history.events
        if event.HasField("marker_recorded_event_attributes")
    ]


@needs_temporal
async def test_a_declaration_problem_gets_one_fixup_and_no_redraft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    _flaky_assembly(monkeypatch)
    fixups = _fixup_drafter(
        harness, lambda c: _run_task(c)["interfaces"]["consumes"].append("ctr-functions-api")
    )
    result = await harness.run(created)
    assert result["status"] == "CANDIDATE", result
    assert len(fixups) == 1
    assembly = harness.calls.index("decomposition.assemble")
    assert harness.calls[assembly + 1 :] == [
        *FIXUP_LOOP,
        "decomposition.assemble",
        "decomposition.stage_document",
        "decomposition.persist",
        "decomposition.finalize_run",
    ]
    # No review of the fix-up: three parts, three reviews; the fix-up is the 7th call.
    assert harness.calls.count("decomposition.review") == 3
    assert harness.queues["decomposition.draft"] == {ROLE_TASK_QUEUES["claude"]}
    row = harness.row(created)
    assert row.provider_calls == 7
    assert any(key.endswith(":fixup:1") for key in row.parts_json["calls"])
    run = next(e for e in row.parts_json["parts"].values() if e.get("number") == 2)
    assert run["approved_attempt"] == 2 and run["fixup"]["status"] == "APPLIED"
    base, derived = run["versions"]
    assert derived["reviewed_sha256"] == base["reviewed_sha256"] == base["content_sha256"]
    assert derived["fixup"]["base_attempt"] == 1
    assert derived["fixup"]["changes"] == [
        {
            "task": RUN_T1,
            "field": "interfaces.consumes",
            "value": ["ctr-worker-invoke", "ctr-functions-api"],
        }
    ]
    assert derived["fixup"]["problems"][0]["code"] == "DEPENDENCY_UNRESOLVED"
    assert run["rejections"][0]["attempt"] == 1
    assert run["rejections"][0]["code"] == "ASSEMBLY_INVALID"
    tasks = {t["key"]: t for s in row.plan_json["stories"] for t in s["tasks"]}
    assert tasks[RUN_T1]["interfaces"]["consumes"] == ["ctr-worker-invoke", "ctr-functions-api"]
    assert any(item["phase"] == "fixup" for item in row.evidence_json["usage"])
    assert any("decomposition-declaration-fixup-v1" in item for item in _markers(harness))


@needs_temporal
async def test_a_part_gets_one_fixup_per_plan_then_its_redraft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    _flaky_assembly(monkeypatch, times=2)
    fixups = _fixup_drafter(
        harness, lambda c: _run_task(c)["interfaces"]["consumes"].append("ctr-functions-api")
    )
    result = await harness.run(created)
    assert result["status"] == "CANDIDATE", result
    assert len(fixups) == 1
    first = harness.calls.index("decomposition.assemble")
    second = harness.calls.index("decomposition.assemble", first + 1)
    assert harness.calls[first + 1 : second] == FIXUP_LOOP
    assert harness.calls[second + 1 : second + 1 + len(PART_LOOP)] == PART_LOOP
    run = next(e for e in harness.row(created).parts_json["parts"].values() if e.get("number") == 2)
    # The full redraft follows the fix-up's attempt number; it withdraws its approval.
    assert run["approved_attempt"] == 3
    assert [item["attempt"] for item in run["versions"]] == [1, 2, 3]


@needs_temporal
async def test_a_refused_fixup_falls_back_to_the_full_redraft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    _flaky_assembly(monkeypatch)

    def criterion(content: dict[str, Any]) -> None:
        _run_task(content)["acceptance_criteria"][0]["criterion"] = "Something else."

    fixups = _fixup_drafter(harness, criterion)
    result = await harness.run(created)
    assert result["status"] == "CANDIDATE", result
    assert len(fixups) == 1
    assembly = harness.calls.index("decomposition.assemble")
    assert harness.calls[assembly + 1 : assembly + 1 + len(FIXUP_LOOP) + len(PART_LOOP)] == [
        *FIXUP_LOOP,
        *PART_LOOP,
    ]
    row = harness.row(created)
    run = next(e for e in row.parts_json["parts"].values() if e.get("number") == 2)
    assert run["fixup"]["status"] == "REFUSED"
    assert any("acceptance_criteria" in reason for reason in run["fixup"]["reasons"])
    # The redraft uses the attempt the refused fix-up never stored.
    assert run["approved_attempt"] == 2
    assert all("fixup" not in version for version in run["versions"])
    assert harness.calls.count("decomposition.review") == 4
    assert row.provider_calls == 9


@needs_temporal
async def test_a_history_from_before_the_fixup_replays_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = WorkflowHarness(tmp_path)
    created = harness.create()
    _flaky_assembly(monkeypatch)
    fixups = _fixup_drafter(harness, lambda c: None)
    # Recorded by the pre-change class; WorkflowHarness.run replays it on the current one.
    result = await harness.run(created, workflow_cls=PreDeclarationFixupDecompositionPlanWorkflow)
    assert result["status"] == "CANDIDATE", result
    assert fixups == [] and "decomposition.prepare_part_fixup" not in harness.calls
    assembly = harness.calls.index("decomposition.assemble")
    assert harness.calls[assembly + 1 : assembly + 1 + len(PART_LOOP)] == PART_LOOP
    assert not any("decomposition-declaration-fixup-v1" in item for item in _markers(harness))
