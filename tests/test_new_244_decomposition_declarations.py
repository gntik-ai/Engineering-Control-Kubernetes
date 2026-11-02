"""NEW-244: deterministic declaration normalization of the backlog decomposition.

normalize_declarations adds a registered frame contract to a task's interfaces.consumes
from its `contract:<ctr-id>` dependencies (N1), its provided contracts (N2) and its paths
under a contract's registered path (N3). The draft Hermes stores, hashes, stages and has
reviewed is the normalized one; approved and reused parts are normalized in memory when
they are read (their stored content and hashes stay as reviewed), so the assembled plan,
its Markdown and its JSON carry the normalized declarations.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from uuid import UUID

from new_234_decomposition_fixtures import (
    FRAME_SHA,
    api_epic,
    epic_output,
    frame,
    inputs,
    inventory,
    run_epic,
    task,
)
from new_234_plan_fixtures import EPICS, PlanHarness, draft_for, drive
from sqlalchemy import update

from engineering_control.db.models import BacklogDecomposition
from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.decomposition import (
    MAX_CONSUMES,
    DecompositionRejected,
    EpicPart,
    assemble_plan,
    check_epic_draft,
    normalize_declarations,
    normalize_epic_content,
    normalize_epic_output,
    part_content_sha256,
)

WORKER = "ctr-worker-invoke"  # owned by falcone-serverless, path wit/invoke.wit
FUNCTIONS = "ctr-functions-api"  # owned by falcone, path services/functions/openapi.yaml
SERVERLESS = "falcone-serverless:"
FALCONE = {"repository": "gntik-ai/falcone", "role": "source", "component": "functions-api"}


def _contracts() -> list[dict[str, Any]]:
    return list(frame()["contracts"])


def _story(*tasks: dict[str, Any], story: str = "RUN-01") -> dict[str, Any]:
    return {"story_id": story, "tasks": list(tasks), "story_amendment": None}


def _consumes(stories: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        task["key"]: list(task["interfaces"]["consumes"])
        for story in stories
        for task in story["tasks"]
    }


# The rules -------------------------------------------------------------------------------


def test_n1_a_contract_dependency_adds_the_contract_to_consumes() -> None:
    key = SERVERLESS + "API-02.t1"
    stories, records = normalize_declarations(
        [_story(task(key, depends_on=[f"contract:{FUNCTIONS}"]), story="API-02")], _contracts()
    )
    assert _consumes(stories) == {key: [FUNCTIONS]}
    assert records == [{"task": key, "contract": FUNCTIONS, "rule": "N1"}]


def test_n2_a_provided_contract_is_also_consumed() -> None:
    key = SERVERLESS + "RUN-01.t1"
    stories, records = normalize_declarations(
        [_story(task(key, interfaces={"provides": [WORKER], "consumes": []}))], _contracts()
    )
    assert _consumes(stories) == {key: [WORKER]}
    assert records == [{"task": key, "contract": WORKER, "rule": "N2"}]


def test_n3_a_path_at_or_under_the_contract_path_in_its_owner_repository() -> None:
    exact = SERVERLESS + "RUN-01.t1"
    directory = SERVERLESS + "RUN-01.t2"
    sibling = SERVERLESS + "RUN-01.t3"
    elsewhere = SERVERLESS + "RUN-01.t4"
    contracts = [*_contracts(), {**_contracts()[0], "id": "ctr-wit-dir", "path": "wit/"}]
    stories, records = normalize_declarations(
        [
            _story(
                task(exact, paths=["worker/src/lib.rs", "wit/invoke.wit"]),
                task(directory, paths=["wit/sub/other.wit"]),
                # A name prefix is not the path (only the wit/ directory contract matches).
                task(sibling, paths=["wit/invoke.wit2", "witness/x.rs"]),
                # The same path in another repository is another file.
                task(elsewhere, target=FALCONE, paths=["wit/invoke.wit"]),
            )
        ],
        contracts,
    )
    assert _consumes(stories) == {
        exact: [WORKER, "ctr-wit-dir"],
        directory: ["ctr-wit-dir"],
        sibling: ["ctr-wit-dir"],
        elsewhere: [],
    }
    assert {(item["task"], item["contract"], item["rule"]) for item in records} == {
        (exact, WORKER, "N3"),
        (exact, "ctr-wit-dir", "N3"),
        (directory, "ctr-wit-dir", "N3"),
        (sibling, "ctr-wit-dir", "N3"),
    }


def test_only_registered_contracts_are_added_once_in_rule_order_and_within_the_bound() -> None:
    key = SERVERLESS + "API-01.t1"
    value = task(
        key,
        target=FALCONE,
        interfaces={"provides": [FUNCTIONS, "ctr-unknown"], "consumes": []},
        depends_on=["contract:ctr-unregistered", f"contract:{WORKER}", f"contract:{WORKER}"],
        paths=["services/functions/openapi.yaml"],
    )
    stories, records = normalize_declarations([_story(value, story="API-01")], _contracts())
    # An unknown contract stays CONTRACT_UNKNOWN on its own field; it is never copied.
    assert _consumes(stories) == {key: [WORKER, FUNCTIONS]}
    assert [item["rule"] for item in records] == ["N1", "N2"]
    full = [f"ctr-c{index:02d}" for index in range(MAX_CONSUMES)]
    crowded = task(key, interfaces={"provides": [WORKER], "consumes": full})
    contracts = [*_contracts(), *({**_contracts()[0], "id": item} for item in full)]
    stories, records = normalize_declarations([_story(crowded)], contracts)
    assert _consumes(stories)[key] == full and records == []


def test_normalization_is_idempotent_and_deterministic() -> None:
    stories = [*api_epic()["stories"], *run_epic()["stories"]]
    once, records = normalize_declarations(stories, _contracts())
    again, more = normalize_declarations(once, _contracts())
    assert records and more == [] and again == once
    assert normalize_declarations(stories, _contracts()) == (once, records)


def test_only_consumes_changes_and_the_input_is_not_mutated() -> None:
    stories = [*api_epic()["stories"], *run_epic()["stories"]]
    original = copy.deepcopy(stories)
    normalized, records = normalize_declarations(stories, _contracts())
    assert stories == original
    assert {(item["task"], item["contract"]) for item in records} == {
        (SERVERLESS + "RUN-01.t1", WORKER),
        (SERVERLESS + "API-01.t1", FUNCTIONS),
    }

    def without_consumes(items: list[dict[str, Any]]) -> list[Any]:
        found = copy.deepcopy(items)
        for story in found:
            for value in story["tasks"]:
                value["interfaces"].pop("consumes")
        return found

    assert without_consumes(normalized) == without_consumes(original)
    assert [t["key"] for s in normalized for t in s["tasks"]] == [
        t["key"] for s in original for t in s["tasks"]
    ]
    for before, after in zip(
        _consumes(original).values(), _consumes(normalized).values(), strict=True
    ):
        assert after[: len(before)] == before


def test_n2_creates_no_plan_problem_and_n1_resolves_an_unconsumed_dependency() -> None:
    contracts = _contracts()
    run = normalize_epic_content(run_epic(), contracts)[0]
    api = normalize_epic_content(api_epic(), contracts)[0]
    plan = assemble_plan(
        inventory(),
        frame(),
        [EpicPart(2, "ep02-" + "b" * 16, run), EpicPart(1, "ep01-" + "a" * 16, api)],
    )
    # N2's self-consumption binds nothing: the contract dependencies resolve as before.
    keys = {t["key"]: t["depends_on"] for s in plan["stories"] for t in s["tasks"]}
    raw = {t["key"]: t["depends_on"] for s in api_epic()["stories"] for t in s["tasks"]}
    assert {key: keys[key] for key in raw} == raw
    unconsumed = api_epic()
    unconsumed["stories"][1]["tasks"][0]["depends_on"] = [f"contract:{FUNCTIONS}"]
    part = "ep01-" + "a" * 16
    try:
        check_epic_draft(
            epic_output(unconsumed, part),
            inputs(),
            part,
            inventory=inventory(),
            frame=frame(),
            frame_sha256=FRAME_SHA,
            earlier=[run_epic()],
        )
    except DecompositionRejected as rejected:
        assert {item.code for item in rejected.problems} == {"CONTRACT_DEPENDENCY_UNCONSUMED"}
    else:
        raise AssertionError("the unconsumed contract dependency was accepted")
    value, records = normalize_epic_output(epic_output(unconsumed, part), contracts)
    check_epic_draft(
        value,
        inputs(),
        part,
        inventory=inventory(),
        frame=frame(),
        frame_sha256=FRAME_SHA,
        earlier=[run_epic()],
    )
    assert {"task": SERVERLESS + "API-02.t1", "contract": FUNCTIONS, "rule": "N1"} in records


def test_a_schema_invalid_or_blocked_output_is_left_for_the_checks() -> None:
    part = "ep01-" + "a" * 16
    broken = epic_output(api_epic(), part)
    broken["epic"]["stories"][0]["tasks"][0]["size"] = "XL"
    assert normalize_epic_output(broken, _contracts()) == (broken, [])
    blocked = {**epic_output(api_epic(), part), "status": "BLOCKED"}
    assert normalize_epic_output(blocked, _contracts()) == (blocked, [])


# The plan --------------------------------------------------------------------------------


def _parts(harness: PlanHarness, identity: str) -> dict[str, Any]:
    with harness.repository.sessions() as session:
        row = session.get(BacklogDecomposition, UUID(identity))
        assert row is not None
        return copy.deepcopy(dict(row.parts_json or {}))


def _plan(harness: PlanHarness, identity: str) -> dict[str, Any]:
    with harness.repository.sessions() as session:
        row = session.get(BacklogDecomposition, UUID(identity))
        assert row is not None
        return copy.deepcopy(dict(row.plan_json or {}))


def _verified(harness: PlanHarness, identity: str) -> None:
    """The candidate's stored plan, Markdown and binding pass the approval's tamper check
    (plan hash, binding and every plan rule re-run on the stored, normalized plan)."""
    with harness.repository.sessions() as session:
        row = session.get(BacklogDecomposition, UUID(identity))
        assert row is not None and row.status == "CANDIDATE"
        harness.repository._verify(row)
    markdown = harness.repository.markdown_sync(UUID(identity))
    assert markdown is not None and markdown.startswith("# Backlog decomposition")


def test_record_part_draft_stores_the_normalized_draft_with_consistent_hashes(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    drafted: dict[str, Any] = {}

    def draft(payload: dict[str, Any], attempt: int) -> Any:
        value = draft_for(payload)
        drafted[payload["part_id"]] = value
        return value

    # drive() stages the recorded document and binds the review to the staged part's hash,
    # so a CANDIDATE proves stored == staged == reviewed.
    assert drive(harness, created["id"], draft=draft)["status"] == "CANDIDATE"
    parts = _parts(harness, created["id"])["parts"]
    contracts = _contracts()
    for part_id, entry in parts.items():
        version = entry["versions"][0]
        assert part_content_sha256(version["content"]) == version["content_sha256"]
        assert version["content_sha256"] == version["reviewed_sha256"]
        if entry["kind"] == "frame":
            assert "normalized" not in version
            continue
        raw = drafted[part_id]["epic"]
        expected, records = normalize_epic_content(raw, contracts)
        assert version["content"] == expected != raw
        assert version["normalized"] == records
    plan = _plan(harness, created["id"])
    consumed = {t["key"]: t["interfaces"]["consumes"] for s in plan["stories"] for t in s["tasks"]}
    assert consumed[SERVERLESS + "RUN-01.t1"] == [WORKER]
    assert consumed[SERVERLESS + "API-01.t1"] == [WORKER, FUNCTIONS]
    _verified(harness, created["id"])


def test_the_draft_hash_the_reviewer_binds_is_the_normalized_documents(tmp_path: Path) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    seen: list[dict[str, Any]] = []
    original = harness.step

    def step(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = original(name, payload)
        if name == "decomposition.record_part_draft":
            seen.append(result)
        return result

    harness.step = step  # type: ignore[method-assign]
    assert drive(harness, created["id"])["status"] == "CANDIDATE"
    for result in seen:
        assert result["draft_sha256"] == canonical_sha256(result["document"])
    epic = [item for item in seen if "epic" in item["document"]]
    assert epic and all(
        item["document"]["epic"] != EPICS[item["part"]["number"]]() for item in epic
    )


def test_the_review_prompt_lists_the_normalizations_and_keeps_contract_findings(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    assert drive(harness, created["id"])["status"] == "CANDIDATE"
    reviews = [item["prompt"] for item in harness.requests if item["provider"] == "codex"]
    frame_review, run_review, api_review = reviews
    assert "Declared interfaces" not in frame_review
    for prompt in (run_review, api_review):
        assert "Hermes normalizes every epic batch" in prompt
        assert "N1) depends_on names contract:<ctr-id>" in prompt
        assert "N2) interfaces.provides names the contract" in prompt
        assert "N3) a task path is the contract's registered path" in prompt
        assert "never make it the sole reason for REVISE" in prompt
        # Real contract findings stay REVISE findings.
        assert "a provided contract the task's repository does not own" in prompt
        assert "does not list in interfaces.provides" in prompt
    assert f"{SERVERLESS}RUN-01.t1 consumes {WORKER} (N2)" in run_review
    assert f"{SERVERLESS}API-01.t1 consumes {FUNCTIONS} (N2)" in api_review


def test_a_part_approved_before_normalization_is_reused_and_assembled_normalized(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    first = harness.create()
    assert drive(harness, first["id"])["status"] == "CANDIDATE"
    # Rewrite the first plan's approved API batch as one approved before NEW-244: never
    # normalized, with a contract dependency it does not consume (today's checks refuse it
    # unless the read normalizes it).
    raw = api_epic()
    raw["stories"][1]["tasks"][0]["depends_on"] = [f"contract:{FUNCTIONS}"]
    parts = _parts(harness, first["id"])
    api_id = next(key for key, entry in parts["parts"].items() if entry.get("number") == 1)
    version = parts["parts"][api_id]["versions"][0]
    version.pop("normalized", None)
    version["content"] = raw
    version["content_sha256"] = version["reviewed_sha256"] = part_content_sha256(raw)
    with harness.repository.sessions.begin() as session:
        session.execute(
            update(BacklogDecomposition)
            .where(BacklogDecomposition.id == UUID(first["id"]))
            .values(parts_json=parts)
        )
    harness.requests.clear()
    second = harness.create()
    assert drive(harness, second["id"])["status"] == "CANDIDATE"
    view = harness.repository.get_sync(UUID(second["id"]))
    assert view is not None and harness.requests == [] and view["provider_calls"] == 0
    assert {item["reused_from"]["decomposition_id"] for item in view["parts"]} == {first["id"]}
    # The adopted version is stored exactly as reviewed (its id and hashes unchanged) ...
    adopted = _parts(harness, second["id"])["parts"][api_id]["versions"][0]
    assert adopted["content"] == raw
    assert adopted["content_sha256"] == adopted["reviewed_sha256"] == part_content_sha256(raw)
    # ... and the assembled plan, its JSON and its Markdown carry the normalized batch.
    plan = _plan(harness, second["id"])
    consumed = {t["key"]: t["interfaces"]["consumes"] for s in plan["stories"] for t in s["tasks"]}
    assert consumed[SERVERLESS + "API-02.t1"] == [FUNCTIONS]
    assert consumed[SERVERLESS + "API-01.t1"] == [WORKER, FUNCTIONS]
    _verified(harness, second["id"])
