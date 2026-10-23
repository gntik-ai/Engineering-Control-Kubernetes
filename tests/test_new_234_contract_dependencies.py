"""NEW-234 1.7.1 (operator-approved 2026-10-23): contract-aware epic order and
`contract:<ctr-id>` dependencies.

The falcone-serverless decomposition kept blocking on its first epic batch, EP-MIG, which
consumes contracts whose provider tasks live in epics not drafted yet. Once the frame is
approved the epics are re-ordered (an epic providing a contract before the epics consuming
it), and a task may depend on `contract:<ctr-id>`, which Hermes binds at assembly to the
tasks providing the contract.
"""

from __future__ import annotations

import copy
import random
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from new_234_decomposition_fixtures import (
    API_PART,
    FRAME_PART,
    FRAME_SHA,
    RUN_PART,
    api_epic,
    epic_output,
    frame,
    frame_output,
    inputs,
    inventory,
    run_epic,
    task,
)
from new_234_fanout_fixtures import KEYS, FanoutHarness

from engineering_control.engineering.decomposition import (
    DecompositionRejected,
    EpicPart,
    assemble_plan,
    assembly_redrafts,
    check_epic_draft,
    check_frame_draft,
    epic_order,
    plan_problems,
    render_markdown,
    resolved_dependencies,
)
from engineering_control.engineering.decomposition_fanout import (
    fanout_tasks,
    task_context,
    task_description,
)

API_T1 = "falcone-serverless:API-01.t1"
API_T2 = "falcone-serverless:API-01.t2"
API2_T1 = "falcone-serverless:API-02.t1"
RUN_T1 = "falcone-serverless:RUN-01.t1"
RUN_T2 = "falcone-serverless:RUN-01.t2"
DECOMPOSITION_ID = "00000000-0000-4000-8000-000000000234"


# Contract-aware epic order --------------------------------------------------------------


def _inventory(
    epics: list[tuple[str, int, list[str]]], dependencies: dict[str, list[str]]
) -> dict[str, Any]:
    """The inventory fields epic_order reads: epics (id, number, issue, stories) and stories
    (id, issue, epic, criteria, requirements, dependencies)."""
    stories = [
        {
            "id": story,
            "issue": 100 + index,
            "epic": epic_id,
            "acceptance_criteria": [{"id": f"{story}#ac1"}],
            "requirements": [],
            "dependencies": dependencies.get(story, []),
        }
        for epic_id, _number, members in epics
        for index, story in enumerate(members)
    ]
    return {
        "epics": [
            {"id": epic_id, "number": number, "issue": number, "stories": members}
            for epic_id, number, members in epics
        ],
        "stories": stories,
    }


def _contract(identity: str, providers: list[str], consumers: list[str]) -> dict[str, Any]:
    return {"id": identity, "provider_stories": providers, "consumer_stories": consumers}


# Shaped like falcone-serverless: 14 epics in backlog order; EP-MIG (12) has a root story
# (MIG-01) and consumes almost every contract (MIG-02). Contract cycles are covered below.
SERVERLESS_EPICS = [
    ("EP-API", 1, ["API-01", "API-02"]),
    ("EP-RUN", 2, ["RUN-01", "RUN-02"]),
    ("EP-DEP", 3, ["DEP-01", "DEP-02"]),
    ("EP-INV", 4, ["INV-01", "INV-02"]),
    ("EP-EVT", 5, ["EVT-01", "EVT-02"]),
    ("EP-WF", 6, ["WF-01", "WF-02"]),
    ("EP-SEC", 7, ["SEC-01"]),
    ("EP-TEN", 8, ["TEN-01"]),
    ("EP-STO", 9, ["STO-01"]),
    ("EP-OBS", 10, ["OBS-01"]),
    ("EP-OPS", 11, ["OPS-01"]),
    ("EP-MIG", 12, ["MIG-01", "MIG-02"]),
    ("EP-WAS", 13, ["WAS-01"]),
    ("EP-JOB", 14, ["JOB-01"]),
]
SERVERLESS_DEPENDENCIES = {
    "RUN-02": ["RUN-01"],
    "API-01": ["RUN-01", "MIG-01"],
    "API-02": ["API-01"],
    "DEP-01": ["RUN-01", "MIG-01"],
    "DEP-02": ["DEP-01"],
    "INV-01": ["RUN-02"],
    "INV-02": ["INV-01"],
    "EVT-01": ["RUN-02"],
    "EVT-02": ["EVT-01"],
    "WF-01": ["RUN-02"],
    "WF-02": ["WF-01"],
    "SEC-01": ["RUN-01"],
    "TEN-01": ["API-01"],
    "STO-01": ["RUN-01"],
    "OBS-01": ["RUN-02"],
    "OPS-01": ["DEP-02"],
    "MIG-02": ["DEP-02", "WF-02", "EVT-02", "API-02"],
    "JOB-01": ["WF-02"],
}
SERVERLESS_FRAME = {
    "contracts": [
        _contract("ctr-worker-execution-api", ["RUN-02"], ["MIG-02", "INV-01", "EVT-01"]),
        _contract("ctr-pool-observed-status", ["INV-02"], ["MIG-02", "OBS-01"]),
        _contract("ctr-functions-api", ["API-01"], ["MIG-02", "DEP-01", "TEN-01"]),
        _contract("ctr-deploy-revision", ["DEP-02"], ["MIG-02", "OPS-01"]),
        _contract("ctr-event-trigger", ["EVT-01"], ["MIG-02", "WF-01"]),
        _contract("ctr-workflow-bridge", ["WF-01"], ["MIG-02", "JOB-01"]),
        _contract("ctr-quota-policy", ["TEN-01"], ["MIG-02", "INV-01"]),
        _contract("ctr-secret-binding", ["STO-01"], ["SEC-01", "MIG-02"]),
        _contract("ctr-capability-host", ["WAS-01"], ["RUN-02"]),
        # An existing contract (no provider story) orders nothing.
        _contract("ctr-legacy-routes", [], ["MIG-02", "API-02"]),
    ],
    "global_decisions": [],
}


def test_the_frame_puts_contract_providers_before_ep_mig() -> None:
    real = _inventory(SERVERLESS_EPICS, SERVERLESS_DEPENDENCIES)
    before = epic_order(real)
    # Before: EP-MIG (a dependency root) is among the first batches.
    assert before.index(12) < 3
    order = epic_order(real, SERVERLESS_FRAME)
    assert sorted(order) == list(range(1, 15))
    # EP-MIG consumes almost every contract: drafted after all eight providing epics (only
    # EP-OPS, which provides nothing to it and sits deeper, comes later).
    assert order.index(12) == 12
    position = {number: index for index, number in enumerate(order)}
    epic_of = {story: number for _id, number, stories in SERVERLESS_EPICS for story in stories}
    for contract in SERVERLESS_FRAME["contracts"]:
        for provider in contract["provider_stories"]:
            for consumer in contract["consumer_stories"]:
                if epic_of[provider] != epic_of[consumer]:
                    assert position[epic_of[provider]] < position[epic_of[consumer]], contract
    # EP-API's story API-01 depends on MIG-01 (a story cycle between the epics), but EP-API
    # provides a contract EP-MIG consumes, so EP-API still goes first.
    assert position[1] < position[12]
    assert order == [13, 2, 9, 7, 5, 6, 14, 1, 8, 4, 10, 3, 12, 11]


def test_the_order_is_deterministic_whatever_the_list_order() -> None:
    real = _inventory(SERVERLESS_EPICS, SERVERLESS_DEPENDENCIES)
    expected = epic_order(real, SERVERLESS_FRAME)
    shuffler = random.Random(234)  # noqa: S311 - a fixed seed for a test permutation
    for _ in range(5):
        epics = copy.deepcopy(SERVERLESS_EPICS)
        shuffler.shuffle(epics)
        contracts = copy.deepcopy(SERVERLESS_FRAME["contracts"])
        shuffler.shuffle(contracts)
        shuffled = _inventory(epics, SERVERLESS_DEPENDENCIES)
        assert epic_order(shuffled, {"contracts": contracts}) == expected


def test_a_contract_cycle_breaks_by_prerequisites_then_depth_then_number() -> None:
    epics = [("EP-AAA", 1, ["AAA-01"]), ("EP-BBB", 2, ["BBB-01"]), ("EP-CCC", 3, ["CCC-01"])]
    cycle = {
        "contracts": [
            _contract("ctr-a", ["AAA-01"], ["BBB-01"]),
            _contract("ctr-b", ["BBB-01"], ["AAA-01"]),
        ]
    }
    # A symmetric cycle: the lower epic number first; the free epic CCC goes before both.
    assert epic_order(_inventory(epics, {}), cycle) == [3, 1, 2]
    # AAA-01 depends on BBB-01 (one undrafted prerequisite epic): BBB goes first.
    assert epic_order(_inventory(epics, {"AAA-01": ["BBB-01"]}), cycle) == [3, 2, 1]
    # Fewer undrafted providers wins over prerequisites: BBB waits on no story but on two
    # providers, AAA and CCC on one each (AAA also on CCC's story), so CCC goes first.
    wider = {
        "contracts": [
            *cycle["contracts"],
            _contract("ctr-c", ["CCC-01"], ["BBB-01"]),
            _contract("ctr-e", ["BBB-01"], ["CCC-01"]),
        ]
    }
    assert epic_order(_inventory(epics, {"AAA-01": ["CCC-01"]}), wider) == [3, 2, 1]
    # Equal providers and prerequisites: the shallower epic first (AAA-01 depends on CCC-01,
    # so by number AAA would go before BBB).
    assert epic_order(_inventory(epics, {"AAA-01": ["CCC-01"]}), cycle) == [3, 2, 1]
    assert epic_order(_inventory(epics, {"BBB-01": ["CCC-01"]}), cycle) == [3, 1, 2]


def test_a_frame_without_ordering_hints_or_with_foreign_stories_orders_by_dependencies() -> None:
    real = _inventory(SERVERLESS_EPICS, SERVERLESS_DEPENDENCIES)
    foreign = {"contracts": [_contract("ctr-x", ["ZZZ-01"], ["MIG-02"]), {"id": "ctr-old"}]}
    order = epic_order(real, foreign)
    assert sorted(order) == list(range(1, 15))
    # Hints naming no inventory story (or absent) order nothing: story dependencies decide.
    assert order == epic_order(real, {"contracts": []})


def test_the_fixture_frame_keeps_run_before_api() -> None:
    assert epic_order(inventory()) == [2, 1]
    assert epic_order(inventory(), frame()) == [2, 1]
    reversed_hint = copy.deepcopy(frame())
    reversed_hint["contracts"][0]["provider_stories"] = []
    reversed_hint["contracts"][1]["consumer_stories"] = ["RUN-01"]
    assert epic_order(inventory(), reversed_hint) == [1, 2]


def test_the_frame_check_refuses_unknown_provider_and_consumer_stories() -> None:
    stories = [str(story["id"]) for story in inventory()["stories"]]
    assert check_frame_draft(frame_output(), inputs(), FRAME_PART, stories=stories) == frame()
    wrong = copy.deepcopy(frame())
    wrong["contracts"][0]["provider_stories"] = ["ZZZ-01"]
    wrong["contracts"][1]["consumer_stories"] = ["API-01", "MIG-02"]
    with pytest.raises(DecompositionRejected) as rejected:
        check_frame_draft(frame_output(wrong), inputs(), FRAME_PART, stories=stories)
    assert [(item["code"], item["subject"]) for item in rejected.value.details] == [
        ("CONTRACT_STORY_UNKNOWN", "ctr-worker-invoke"),
        ("CONTRACT_STORY_UNKNOWN", "ctr-functions-api"),
    ]
    # Without the inventory's stories (a stored plan re-checked) only the shape is checked.
    assert check_frame_draft(frame_output(wrong), inputs(), FRAME_PART) == wrong


# Contract dependencies: plan checks ------------------------------------------------------


def _epics(change: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    api, run = api_epic(), run_epic()
    if change is not None:
        tasks = {t["key"]: t for epic in (api, run) for s in epic["stories"] for t in s["tasks"]}
        change(tasks, api, run)
    return api, run


def _assemble(change: Any = None, contracts: Any = None) -> dict[str, Any]:
    api, run = _epics(change)
    framed = frame() if contracts is None else contracts
    return assemble_plan(
        inventory(), framed, [EpicPart(1, API_PART, api), EpicPart(2, RUN_PART, run)]
    )


def _codes(change: Any) -> list[tuple[str, str]]:
    with pytest.raises(DecompositionRejected) as rejected:
        _assemble(change)
    assert rejected.value.code == "ASSEMBLY_INVALID"
    return sorted((item["code"], item["subject"]) for item in rejected.value.details)


def _contract_dependency(tasks: dict[str, Any], *_epics: Any) -> None:
    tasks[API_T1]["depends_on"] = ["contract:ctr-worker-invoke"]


def test_a_contract_dependency_on_a_consumed_frame_contract_is_accepted() -> None:
    plan = _assemble(_contract_dependency)
    assert plan_problems(plan, inventory()) == []
    task_of = {t["key"]: t for s in plan["stories"] for t in s["tasks"]}
    # The plan keeps the authored dependency; the binding is derived from the plan.
    assert task_of[API_T1]["depends_on"] == ["contract:ctr-worker-invoke"]
    bound = resolved_dependencies(plan["stories"])
    assert bound[API_T1] == (RUN_T1,)
    assert bound[API_T2] == (API_T1,) and bound[RUN_T1] == ()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        # The frame does not declare the contract.
        (
            lambda t, *_: t[API_T1].update(depends_on=["contract:ctr-unknown"]),
            # API-01 no longer reaches its prerequisite RUN-01 either.
            [("CONTRACT_UNKNOWN", API_T1), ("PREREQUISITE_UNORDERED", "API-01")],
        ),
        # The task does not consume it (API-02.t1 consumes nothing).
        (
            lambda t, *_: t[API2_T1].update(depends_on=[API_T1, "contract:ctr-worker-invoke"]),
            [("CONTRACT_DEPENDENCY_UNCONSUMED", API2_T1)],
        ),
        # Nothing provides it once every batch is drafted (keyed by the consumer).
        (
            lambda t, *_: (
                _contract_dependency(t),
                t[RUN_T1]["interfaces"].update(provides=[]),
            ),
            [("CONTRACT_PROVIDER_MISSING", API_T1), ("PREREQUISITE_UNORDERED", "API-01")],
        ),
        # The bound graph must stay acyclic: RUN-01.t1 -> (API-01.t1 provides the Functions
        # API) -> RUN-01.t1.
        (
            lambda t, *_: t[RUN_T1].update(
                depends_on=["contract:ctr-functions-api"],
                interfaces={"provides": ["ctr-worker-invoke"], "consumes": ["ctr-functions-api"]},
            ),
            [("DEPENDENCY_CYCLE", API_T1)],
        ),
    ],
)
def test_contract_dependencies_are_refused_when_they_cannot_bind(
    change: Any, expected: list[tuple[str, str]]
) -> None:
    assert _codes(change) == expected


def test_a_missing_provider_goes_back_to_the_consuming_part() -> None:
    def missing(tasks: dict[str, Any], *_epics: Any) -> None:
        _contract_dependency(tasks)
        tasks[RUN_T1]["interfaces"]["provides"] = []

    api, run = _epics(missing)
    parts = [EpicPart(2, RUN_PART, run), EpicPart(1, API_PART, api)]
    with pytest.raises(DecompositionRejected) as rejected:
        assemble_plan(inventory(), frame(), parts)
    owned, unowned = assembly_redrafts(rejected.value.details, inventory(), parts)
    assert list(owned) == [API_PART] and unowned == []
    assert owned[API_PART][0]["detail"].startswith(
        "depends on contract:ctr-worker-invoke, which no plan task provides"
    )


def test_a_contract_dependency_binds_to_every_provider() -> None:
    second = task(
        RUN_T2,
        title="Expose the invocation interface to the API",
        acceptance_criteria=[{"criterion": "The interface answers.", "covers": ["RUN-01#ac1"]}],
        interfaces={"provides": ["ctr-worker-invoke"], "consumes": []},
        depends_on=[RUN_T1],
    )

    def two_providers(tasks: dict[str, Any], _api: Any, run: dict[str, Any]) -> None:
        _contract_dependency(tasks)
        run["stories"][0]["tasks"].append(second)
        # An explicit key and the contract naming the same task bind it once.
        tasks[API2_T1]["depends_on"] = [API_T1, RUN_T2]

    plan = _assemble(two_providers)
    bound = resolved_dependencies(plan["stories"])
    assert bound[API_T1] == (RUN_T1, RUN_T2)
    markdown = render_markdown(
        plan, decomposition_id=DECOMPOSITION_ID, plan_sha256="0" * 64, inventory=inventory()
    )
    assert f"| `contract:ctr-worker-invoke` (`{RUN_T1}`, `{RUN_T2}`) |" in markdown, markdown
    # The provider itself is never its own predecessor.
    tasks = {t["key"]: t for s in plan["stories"] for t in s["tasks"]}
    tasks[RUN_T2]["interfaces"]["consumes"] = ["ctr-worker-invoke"]
    tasks[RUN_T2]["depends_on"] = ["contract:ctr-worker-invoke"]
    assert resolved_dependencies(plan["stories"])[RUN_T2] == (RUN_T1,)


def test_an_epic_batch_may_depend_on_a_contract_not_provided_yet() -> None:
    api, _run = _epics(_contract_dependency)
    # API is drafted first: RUN (the provider) is not drafted yet, so binding waits.
    content = check_epic_draft(
        epic_output(api, API_PART),
        inputs(),
        API_PART,
        inventory=inventory(),
        frame=frame(),
        frame_sha256=FRAME_SHA,
    )
    assert content == api
    # An unknown contract is refused at once.
    unknown = copy.deepcopy(api)
    unknown["stories"][0]["tasks"][0]["depends_on"] = ["contract:ctr-unknown"]
    with pytest.raises(DecompositionRejected) as rejected:
        check_epic_draft(
            epic_output(unknown, API_PART),
            inputs(),
            API_PART,
            inventory=inventory(),
            frame=frame(),
            frame_sha256=FRAME_SHA,
        )
    assert {item["code"] for item in rejected.value.details} == {"CONTRACT_UNKNOWN"}
    # The last batch: nothing left to draft, a contract nobody provides is refused there.
    orphan = run_epic()
    orphan["stories"][0]["tasks"][0]["interfaces"] = {
        "provides": [],
        "consumes": ["ctr-functions-api"],
    }
    orphan["stories"][0]["tasks"][0]["depends_on"] = ["contract:ctr-functions-api"]
    bare = copy.deepcopy(api_epic())
    bare["stories"][0]["tasks"][0]["interfaces"]["provides"] = []
    with pytest.raises(DecompositionRejected) as late:
        check_epic_draft(
            epic_output(orphan, RUN_PART),
            inputs(),
            RUN_PART,
            inventory=inventory(),
            frame=frame(),
            frame_sha256=FRAME_SHA,
            earlier=[bare],
        )
    assert [(item["code"], item["subject"]) for item in late.value.details] == [
        ("CONTRACT_PROVIDER_MISSING", RUN_T1)
    ]


# The fan-out links the bound tasks -------------------------------------------------------


def test_the_fanout_orders_and_links_the_bound_provider_tasks() -> None:
    plan = _assemble(_contract_dependency)
    tasks = fanout_tasks(plan)
    by_key = {item.key: item for item in tasks}
    assert [item.key for item in tasks].index(RUN_T1) < [item.key for item in tasks].index(API_T1)
    assert by_key[API_T1].depends_on == ("RUN-01.t1",)
    context = task_context(by_key[API_T1], decomposition_id=DECOMPOSITION_ID, plan_sha256="0" * 64)
    assert context.depends_on_tasks == ("RUN-01.t1",)
    description = task_description(by_key[API_T1], decomposition_id=DECOMPOSITION_ID)
    assert f"Predecessor tasks: {RUN_T1}." in description
    assert "contract:" not in description.split("Predecessor tasks:", 1)[1].split("\n", 1)[0]


def test_published_providers_become_depends_on_links(tmp_path: Path) -> None:
    harness = FanoutHarness(tmp_path)

    def contract_dependency(plan: dict[str, Any]) -> None:
        for story in plan["stories"]:
            for item in story["tasks"]:
                if item["key"] == API_T1:
                    item["depends_on"] = ["contract:ctr-worker-invoke"]

    approved = harness.approved(contract_dependency)
    plan = harness.repository.load_approved_sync(UUID(approved["id"]))["plan"]
    assert any(
        item["depends_on"] == ["contract:ctr-worker-invoke"]
        for story in plan["stories"]
        for item in story["tasks"]
    )
    result = harness.run_fanout(approved)
    assert (result["drafted"], result["failed"]) == (4, 0)
    assert KEYS[0] == RUN_T1 and KEYS[1] == API_T1
    dependent = harness.request(approved, API_T1)
    assert "- Predecessor tasks: `RUN-01.t1`" in dependent["issue_body"]
    assert "contract:" not in dependent["issue_body"].split("## Backlog decomposition", 1)[1]
    assert dependent["awaiting_dependencies"] == ["RUN-01.t1"]
    harness.publish(approved, RUN_T1, 101)
    current = harness.request(approved, API_T1)
    assert "- Depends on: gntik-ai/falcone-serverless#101" in current["issue_body"]
    assert current["decomposition_context"]["depends_on_tasks"] == ["RUN-01.t1"]
    assert current["awaiting_dependencies"] == []
