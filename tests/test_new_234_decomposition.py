"""NEW-234 PR4: the decomposition contract (schemas, skill registry, part ids), every plan
check of design section 2, the assembly (determinism goldens) and the risk preview."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from new_234_decomposition_fixtures import (
    API_PART,
    DRAFT_SHA,
    FRAME_PART,
    FRAME_SHA,
    RUN_PART,
    api_epic,
    build_decomposition_pack,
    epic_output,
    frame,
    frame_output,
    inputs,
    inventory,
    mutated,
    review_output,
    run_epic,
    task,
)

from engineering_control.engineering.decomposition import (
    AGENT_PACK_MIN_VERSION,
    DECOMPOSE_SKILL,
    DECOMPOSITION_PART_SCHEMAS,
    DECOMPOSITION_SKILLS,
    MAX_REVIEW_CHARACTERS,
    REVIEW_SKILL,
    TASK_TARGETS,
    TEST_COMMANDS,
    DecompositionRejected,
    EpicPart,
    _effective,
    assemble_plan,
    check_epic_draft,
    check_frame_draft,
    check_review,
    decomposition_pack_problems,
    decomposition_provenance,
    epic_order,
    frame_problems,
    part_id,
    part_schema,
    plan_json,
    plan_problems,
    plan_schema,
    plan_sha256,
    refused_mention,
    refused_path,
    render_markdown,
    review_schema,
    risk_preview,
    task_risk,
    text_problems,
)
from engineering_control.engineering.decomposition_inventory import epic_input, frame_input
from engineering_control.engineering.plan_review import finding_text
from engineering_control.engineering.ui_blueprint import UI_PLAN_SKILLS, load_schema

ROOT = Path(__file__).parents[1]
# The agent-pack checkout whose schemas must equal Hermes's (the PR3 branch can be named
# with ENGINEERING_AGENT_PACK_CHECKOUT until the pack is released).
LOCAL_PACK = Path(
    os.environ.get("ENGINEERING_AGENT_PACK_CHECKOUT") or ROOT.parent / "engineering-agent-pack"
)
DECOMPOSITION_ID = "00000000-0000-4000-8000-000000000234"
SCHEMA_NAMES = sorted(
    {
        *(schema for _role, schema in DECOMPOSITION_SKILLS.values()),
        *DECOMPOSITION_PART_SCHEMAS[DECOMPOSE_SKILL].values(),
    }
)


def assembled() -> dict[str, Any]:
    return assemble_plan(
        inventory(),
        frame(),
        [EpicPart(1, API_PART, api_epic()), EpicPart(2, RUN_PART, run_epic())],
    )


def codes(problems: list[Any]) -> set[str]:
    return {item.code for item in problems}


def tasks_of(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["key"]: task for story in plan["stories"] for task in story["tasks"]}


# Contract: schemas and the skill registry ---------------------------------------------------


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_embedded_schemas_equal_the_agent_pack_files(name: str) -> None:
    pack_schema = LOCAL_PACK / "schemas" / name
    if not pack_schema.is_file():
        pytest.skip("the agent-pack checkout has no decomposition schemas")
    embedded = ROOT / "src/engineering_control/engineering/schemas" / name
    # Byte-for-byte copies of the pack contract.
    assert embedded.read_bytes() == pack_schema.read_bytes()
    for skill, (_role, schema) in DECOMPOSITION_SKILLS.items():
        if schema == name:
            released = LOCAL_PACK / "skills" / skill / "references" / "output.schema.json"
            assert json.loads(released.read_text()) == load_schema(name)
    for skill, parts in DECOMPOSITION_PART_SCHEMAS.items():
        if name in parts.values():
            released = LOCAL_PACK / "skills" / skill / "references" / name
            assert json.loads(released.read_text()) == load_schema(name)


def test_registry_names_valid_schemas_roles_and_parts() -> None:
    assert DECOMPOSITION_SKILLS == {
        "decompose-backlog": ("backlog-decomposer", "backlog-decomposition.schema.json"),
        "review-decomposition": ("decomposition-reviewer", "decomposition-review.schema.json"),
    }
    assert DECOMPOSITION_PART_SCHEMAS == {
        "decompose-backlog": {
            "frame": "backlog-decomposition-frame.schema.json",
            "epic": "backlog-decomposition-epic.schema.json",
        }
    }
    assert not set(DECOMPOSITION_SKILLS) & set(UI_PLAN_SKILLS)
    for name in SCHEMA_NAMES:
        Draft202012Validator.check_schema(load_schema(name))
    assert plan_schema()["properties"]["schema"] == {"const": "backlog-decomposition-v1"}
    assert set(part_schema("frame")["required"]) >= {"frame", "inputs"}
    assert set(part_schema("epic")["required"]) >= {"epic", "inputs"}
    assert "review" in review_schema()["required"]
    assert AGENT_PACK_MIN_VERSION == (1, 7, 1)
    # The three allowed targets and their allowlisted test commands.
    assert {
        repo: (role, sorted(components)) for repo, (role, components) in TASK_TARGETS.items()
    } == {
        "gntik-ai/falcone-serverless": ("source", ["controller", "worker"]),
        "gntik-ai/falcone": ("source", ["admission", "functions-api", "quotas"]),
        "gntik-ai/falcone-charts": ("deployment", ["chart"]),
    }
    assert set(TEST_COMMANDS) == set(TASK_TARGETS)
    assert all(
        refused_mention(command) is None for items in TEST_COMMANDS.values() for command in items
    )


def test_pack_check_needs_profile_1_7_1_and_both_skills(tmp_path: Path) -> None:
    assert decomposition_pack_problems(build_decomposition_pack(tmp_path / "ok")) == []
    old = decomposition_pack_problems(build_decomposition_pack(tmp_path / "old", version="1.6.2"))
    assert old == ["profile version 1.6.2 is older than 1.7.1"]
    missing = decomposition_pack_problems(
        build_decomposition_pack(
            tmp_path / "missing", skip=("skills/review-decomposition/SKILL.md",)
        )
    )
    assert missing == ["missing skills/review-decomposition/SKILL.md"]
    assert decomposition_pack_problems(tmp_path / "missing", (DECOMPOSE_SKILL,)) == []
    drifted = build_decomposition_pack(tmp_path / "drift")
    part = drifted / "skills/decompose-backlog/references/backlog-decomposition-epic.schema.json"
    part.write_text("{}")
    assert decomposition_pack_problems(drifted) == [
        "skills/decompose-backlog part schema differs from Hermes's "
        "backlog-decomposition-epic.schema.json"
    ]


def test_released_or_branch_pack_carries_the_decomposition_skills() -> None:
    if not (LOCAL_PACK / "skills" / DECOMPOSE_SKILL).is_dir():
        pytest.skip("the agent-pack checkout has no decomposition skills")
    assert decomposition_pack_problems(LOCAL_PACK) == []


def test_provenance_binds_skill_sources_and_part_ids_are_deterministic(tmp_path: Path) -> None:
    pack = build_decomposition_pack(tmp_path)
    first = decomposition_provenance(pack)
    assert decomposition_provenance(pack) == first
    (pack / "skills/decompose-backlog/SKILL.md").write_text("# changed\n")
    changed = decomposition_provenance(pack)
    assert changed != first
    assert decomposition_provenance(pack, REVIEW_SKILL) != changed
    document = frame_input(inventory())
    frame_id = part_id(document, first)
    assert frame_id.startswith("frame-") and len(frame_id) == 22
    # An unrelated commit keeps the part; changed content or provenance does not.
    assert part_id({**document, "source_sha": "2" * 40}, first) == frame_id
    assert part_id({**document, "part_id": "x"}, first) == frame_id
    assert part_id(document, changed) != frame_id
    assert part_id({**document, "agents_md": "changed"}, first) != frame_id
    epic = epic_input(inventory(), 2, frame=frame(), frame_sha256=FRAME_SHA)
    epic_id = part_id(epic, first)
    assert epic_id.startswith("ep02-") and len(epic_id) == 21
    assert part_id({**epic, "frame_sha256": "e" * 64}, first) != epic_id
    with pytest.raises(ValueError):
        part_id({"kind": "epic"}, first)


def test_epic_order_puts_dependency_roots_first() -> None:
    assert epic_order(inventory()) == [2, 1]


# Part drafts and reviews -------------------------------------------------------------------


def test_frame_draft_is_checked_and_bound() -> None:
    assert check_frame_draft(frame_output(), inputs(), FRAME_PART) == frame()
    rebound = frame_output()
    rebound["inputs"]["design_sha256"] = "0" * 64
    with pytest.raises(DecompositionRejected) as binding:
        check_frame_draft(rebound, inputs(), FRAME_PART)
    assert binding.value.code == "DRAFT_BINDING_MISMATCH"
    assert binding.value.details[0]["detail"] == "inputs.design_sha256"
    with pytest.raises(DecompositionRejected) as other_part:
        check_frame_draft(frame_output(part="frame-" + "a" * 16), inputs(), FRAME_PART)
    assert other_part.value.code == "DRAFT_BINDING_MISMATCH"
    blocked = {**frame_output(), "status": "BLOCKED", "frame": None, "blockers": ["no AGENTS.md"]}
    with pytest.raises(DecompositionRejected) as stopped:
        check_frame_draft(blocked, inputs(), FRAME_PART)
    assert stopped.value.code == "DRAFT_BLOCKED"
    with pytest.raises(DecompositionRejected) as invalid:
        check_frame_draft({"status": "COMPLETE"}, inputs(), FRAME_PART)
    assert invalid.value.code == "DRAFT_SCHEMA_INVALID"
    duplicate = frame()
    duplicate["contracts"].append(dict(duplicate["contracts"][0]))
    with pytest.raises(DecompositionRejected) as structure:
        check_frame_draft(frame_output(duplicate), inputs(), FRAME_PART)
    assert structure.value.code == "DRAFT_STRUCTURE_INVALID"
    assert codes(list(structure.value.problems)) == {"CONTRACT_DUPLICATE"}
    newline = frame()
    newline["global_decisions"] = ["Decided.\n"]
    with pytest.raises(DecompositionRejected) as trailing:
        check_frame_draft(frame_output(newline), inputs(), FRAME_PART)
    assert codes(list(trailing.value.problems)) == {"TEXT_TRAILING_NEWLINE"}
    with pytest.raises(ValueError):
        check_frame_draft(frame_output(), inputs(), API_PART)


def test_epic_draft_is_checked_with_deferred_and_earlier_dependencies() -> None:
    value = epic_output(api_epic(), API_PART)
    # Drafted first: RUN-01 is a later batch, so API-01.t1's dependency is deferred.
    content = check_epic_draft(
        value, inputs(), API_PART, inventory=inventory(), frame=frame(), frame_sha256=FRAME_SHA
    )
    assert content == api_epic()
    run = check_epic_draft(
        epic_output(run_epic(), RUN_PART),
        inputs(),
        RUN_PART,
        inventory=inventory(),
        frame=frame(),
        frame_sha256=FRAME_SHA,
        earlier=[api_epic()],
    )
    assert run == run_epic()
    with pytest.raises(DecompositionRejected) as frame_bound:
        check_epic_draft(
            epic_output(api_epic(), API_PART, frame_sha256="e" * 64),
            inputs(),
            API_PART,
            inventory=inventory(),
            frame=frame(),
            frame_sha256=FRAME_SHA,
        )
    assert frame_bound.value.details[0]["detail"] == "inputs.frame_sha256"


def _epic_problems(content: dict[str, Any], part: str = API_PART, **kwargs: Any) -> set[str]:
    with pytest.raises(DecompositionRejected) as rejected:
        check_epic_draft(
            epic_output(content, part),
            inputs(),
            part,
            inventory=inventory(),
            frame=frame(),
            frame_sha256=FRAME_SHA,
            **kwargs,
        )
    assert rejected.value.code in {"DRAFT_STRUCTURE_INVALID", "DRAFT_SCHEMA_INVALID"}
    return {item["code"] for item in rejected.value.details}


def test_epic_draft_lists_exactly_its_stories() -> None:
    missing = api_epic()
    missing["stories"] = missing["stories"][:1]
    assert _epic_problems(missing) == {"STORY_MISSING"}
    foreign = api_epic()
    foreign["stories"].append(run_epic()["stories"][0])
    assert "STORY_FOREIGN" in _epic_problems(foreign)
    doubled = api_epic()
    doubled["stories"].append(mutated(doubled["stories"][1]))
    assert {"STORY_DUPLICATE", "TASK_KEY_DUPLICATE"} <= _epic_problems(doubled)


def test_epic_draft_dependency_on_an_unknown_or_earlier_task() -> None:
    unknown = api_epic()
    unknown["stories"][1]["tasks"][0]["depends_on"] = ["falcone-serverless:API-01.t9"]
    assert _epic_problems(unknown) == {"DEPENDENCY_UNRESOLVED"}
    # Once RUN was reviewed earlier, its missing task is no longer deferred.
    later = api_epic()
    later["stories"][0]["tasks"][0]["depends_on"] = ["falcone-serverless:RUN-01.t7"]
    assert _epic_problems(later, earlier=[run_epic()]) == {"DEPENDENCY_UNRESOLVED"}
    schema = api_epic()
    del schema["stories"][0]["tasks"][0]["why"]
    assert _epic_problems(schema) == {"DRAFT_SCHEMA_INVALID"}


def test_review_is_bound_to_its_draft_and_frame() -> None:
    review = check_review(
        review_output(API_PART, frame_sha256=FRAME_SHA),
        inputs(),
        API_PART,
        DRAFT_SHA,
        frame_sha256=FRAME_SHA,
    )
    assert review["decision"] == "APPROVE"
    assert check_review(review_output(FRAME_PART), inputs(), FRAME_PART, DRAFT_SHA)
    with pytest.raises(DecompositionRejected) as draft:
        check_review(review_output(FRAME_PART, "e" * 64), inputs(), FRAME_PART, DRAFT_SHA)
    assert draft.value.code == "REVIEW_BINDING_MISMATCH"
    with pytest.raises(DecompositionRejected) as framed:
        check_review(review_output(API_PART), inputs(), API_PART, DRAFT_SHA, frame_sha256=FRAME_SHA)
    assert framed.value.details[0]["detail"] == "inputs.frame_sha256"
    blocked = {**review_output(FRAME_PART), "status": "BLOCKED", "review": None}
    with pytest.raises(DecompositionRejected) as stopped:
        check_review(blocked, inputs(), FRAME_PART, DRAFT_SHA)
    assert stopped.value.code == "REVIEW_BLOCKED"
    large = review_output(FRAME_PART)
    large["review"]["summary"] = "x" * (MAX_REVIEW_CHARACTERS + 1)
    with pytest.raises(DecompositionRejected) as too_large:
        check_review(large, inputs(), FRAME_PART, DRAFT_SHA)
    assert too_large.value.code == "REVIEW_TOO_LARGE"
    invalid = review_output(FRAME_PART)
    invalid["review"]["decision"] = "MAYBE"
    with pytest.raises(DecompositionRejected) as schema:
        check_review(invalid, inputs(), FRAME_PART, DRAFT_SHA)
    assert schema.value.code == "REVIEW_SCHEMA_INVALID"


def test_allowlisted_test_commands_are_hermetic() -> None:
    hazards = ("kind", "kubectl", "docker", "_LIVE", "curl", "tests/blackbox ", "e2e", "pnpm test")
    for repository, commands in TEST_COMMANDS.items():
        for command in commands:
            assert refused_mention(command) is None, command
            assert not any(hazard in f"{command} " for hazard in hazards), (repository, command)
    charts = TEST_COMMANDS["gntik-ai/falcone-charts"]
    # Only single named test files, never a directory-wide run of the chart suites.
    assert all(
        command.endswith(".test.mjs") for command in charts if command.startswith("node --test")
    )
    assert "node --test tests/blackbox" not in charts


def test_rejection_problems_read_as_findings() -> None:
    rejected = DecompositionRejected("X", ["plain"] * 60)
    assert len(rejected.details) == 50 and rejected.omitted == 10
    assert finding_text(rejected.details[0]) == "[other] -: plain"


# Plan rules (design section 2), positive and negative --------------------------------------


def test_a_valid_plan_has_no_problems() -> None:
    assert plan_problems(assembled(), inventory()) == []


def _plan_codes(change: Any) -> set[str]:
    plan = assembled()
    change(plan, tasks_of(plan))
    return codes(plan_problems(plan, inventory()))


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        # Task key format and uniqueness.
        (
            lambda p, t: t["falcone-serverless:API-02.t1"].update(
                key="falcone-serverless:API-02.t01"
            ),
            {"TASK_KEY_INVALID"},
        ),
        (
            lambda p, t: t["falcone-serverless:API-02.t1"].update(
                key="falcone-serverless:API-01.t3"
            ),
            {"TASK_KEY_STORY_MISMATCH"},
        ),
        (
            lambda p, t: t["falcone-serverless:API-01.t2"].update(
                key="falcone-serverless:API-01.t1", depends_on=[]
            ),
            {"TASK_KEY_DUPLICATE"},
        ),
        (
            lambda p, t: t["falcone-serverless:API-01.t2"].update(
                title="add cli DEPLOY and invoke"
            ),
            {"TASK_TITLE_DUPLICATE"},
        ),
        # Coverage: every story criterion and requirement.
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"]["acceptance_criteria"][0].update(
                covers=["RUN-01#ac1"]
            ),
            {"CRITERION_UNCOVERED"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(requirements=[]),
            {"REQUIREMENT_UNCOVERED"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"]["acceptance_criteria"][0][
                "covers"
            ].append("API-01#ac1"),
            {"COVERS_UNKNOWN"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"]["requirements"].append("FR-API-01"),
            {"REQUIREMENT_UNKNOWN"},
        ),
        (lambda p, t: p["stories"].pop(), {"STORY_MISSING", "DEPENDENCY_UNRESOLVED"}),
        # The dependency graph.
        (
            lambda p, t: t["falcone-serverless:API-02.t1"].update(
                depends_on=["falcone-serverless:API-02.t9"]
            ),
            {"DEPENDENCY_UNRESOLVED", "PREREQUISITE_UNORDERED"},
        ),
        (
            lambda p, t: t["falcone-serverless:API-01.t1"]["depends_on"].append(
                "falcone-serverless:API-01.t2"
            ),
            {"DEPENDENCY_CYCLE"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(
                depends_on=["falcone-serverless:API-01.t1"]
            ),
            {"DEPENDENCY_INCONSISTENT", "DEPENDENCY_CYCLE"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(
                depends_on=["falcone-serverless:RUN-01.t1"]
            ),
            {"DEPENDENCY_SELF"},
        ),
        # Targets: the three allowed (repository, role, component) combinations.
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"]["target"].update(component="chart"),
            {"TARGET_INVALID"},
        ),
        (
            lambda p, t: t["falcone-serverless:API-01.t2"]["target"].update(role="source"),
            {"TARGET_INVALID", "PROPOSAL_BOUNDS"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"]["target"].update(
                repository="gntik-ai/musematic"
            ),
            {"TARGET_INVALID", "PROPOSAL_BOUNDS", "CONTRACT_NOT_OWNED", "TEST_COMMAND_NOT_ALLOWED"},
        ),
        # Refused paths and test commands.
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"]["paths"].append("worker/.GitHub/ci.yml"),
            {"PATH_REFUSED"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"]["what_changes"].append(
                "Update docs/Agents.MD with the new rule."
            ),
            {"PATH_REFUSED"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(test_commands=["make test"]),
            {"TEST_COMMAND_NOT_ALLOWED"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(
                test_commands=["pnpm run test:unit"]
            ),
            {"TEST_COMMAND_NOT_ALLOWED"},
        ),
        # Contracts.
        (
            lambda p, t: t["falcone-serverless:API-02.t1"]["interfaces"]["provides"].append(
                "ctr-worker-invoke"
            ),
            {"CONTRACT_NOT_OWNED"},
        ),
        (
            lambda p, t: t["falcone-serverless:API-02.t1"]["interfaces"]["consumes"].append(
                "ctr-missing"
            ),
            {"CONTRACT_UNKNOWN"},
        ),
        (
            lambda p, t: p["frame"]["contracts"].append(dict(p["frame"]["contracts"][1])),
            {"CONTRACT_DUPLICATE"},
        ),
        # Size and ChangeProposal bounds.
        (lambda p, t: t["falcone-serverless:RUN-01.t1"].update(size="L"), {"SIZE_INVALID"}),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(estimated_files=16),
            {"SIZE_INVALID"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(why="x" * 3001),
            {"PROPOSAL_BOUNDS"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(what_changes=[]),
            {"PROPOSAL_BOUNDS"},
        ),
        # Text hygiene: trailing newline, control and format characters, issue references.
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(why="Needed.\n"),
            {"TEXT_TRAILING_NEWLINE"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(why="Needed\x07 now."),
            {"TEXT_CONTROL_CHARACTER"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(why="Needed \u202e now."),
            {"TEXT_CONTROL_CHARACTER"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"]["sources"].append("design\n§3"),
            {"TEXT_NEWLINE"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"].update(why="Follows up #12."),
            {"TEXT_ISSUE_REFERENCE"},
        ),
        (
            lambda p, t: t["falcone-serverless:RUN-01.t1"]["risks"].append(
                "See gntik-ai/falcone#7."
            ),
            {"TEXT_ISSUE_REFERENCE"},
        ),
        # Hermes's issue numbers and part ids.
        (lambda p, t: p["stories"][0].update(story_issue=99), {"STORY_ISSUE_MISMATCH"}),
        (lambda p, t: p["stories"][0].update(part_id=RUN_PART), {"STORY_PART_MISMATCH"}),
        (lambda p, t: p["inputs"].update(issues_sha256="0" * 64), {"PLAN_BINDING_MISMATCH"}),
    ],
)
def test_every_plan_rule_rejects_its_violation(change: Any, expected: set[str]) -> None:
    assert _plan_codes(change) == expected


def test_text_rules_allow_criterion_ids_entities_and_inner_newlines() -> None:
    assert text_problems({"why": "Covers API-01#ac2 and &#96;code&#96;.\nSecond line."}) == []
    assert codes(text_problems({"title": "One\nTwo"})) == {"TEXT_NEWLINE"}
    assert codes(text_problems({"why": "Tab\there"})) == {"TEXT_CONTROL_CHARACTER"}
    assert codes(text_problems({"why": "Line\u2028separator"})) == {"TEXT_CONTROL_CHARACTER"}
    assert codes(text_problems({"why": "See /issues/12 there"})) == {"TEXT_ISSUE_REFERENCE"}


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "worker/.GitHub/x.yml",
        ".claude/settings.json",
        "deep/.Codex/config.toml",
        ".mcp.json",
        "x/.MCP.JSON",
        "AGENTS.md",
        "docs/agents.MD",
        "a/b/CLAUDE.md",
        "../outside",
        "worker/../../x",
        "/etc/passwd",
        "C:/x",
    ],
)
def test_refused_paths_at_any_depth_and_case(path: str) -> None:
    assert refused_path(path) is not None


@pytest.mark.parametrize(
    "path",
    ["worker/src/main.rs", "docs/agents-guide.md", ".gitignore", "controller/github.go", "./x"],
)
def test_ordinary_paths_are_allowed(path: str) -> None:
    assert refused_path(path) is None


def test_refused_mentions_in_free_text() -> None:
    assert refused_mention("Edit .github/workflows/ci.yml") is not None
    assert refused_mention("Teach claude.md the rule.") is not None
    assert refused_mention("cat ../secrets") is not None
    for quoted in ("`../secrets`", '"../x"', "'..\\x'", "(../x)", "[../x](y)", "<..>", "cd ..",
                   "x/..", "a\\..\\b", "**../x**"):  # fmt: skip
        assert refused_mention(quoted) is not None, quoted
    for fine in ("wait...", "go -C controller test ./...", "v1..v2", "a..b"):
        assert refused_mention(fine) is None, fine
    assert refused_mention("Load raw.githubusercontent.com data and wait...") is None
    assert refused_mention("Add worker/src/agents_md.rs") is None
    assert (
        frame_problems({"contracts": [{**frame()["contracts"][0], "path": "deep/AGENTS.md"}]})[
            0
        ].code
        == "PATH_REFUSED"
    )


def _amend(story: dict[str, Any], *changes: dict[str, Any]) -> None:
    story["story_amendment"] = {
        "reason": "The story disagrees with the design.",
        "sources": ["design §4"],
        "changes": list(changes),
    }


def _change(section: str, action: str, target: str | None, text: str | None) -> dict[str, Any]:
    return {"section": section, "action": action, "target": target, "text": text}


def test_coverage_is_checked_against_the_amended_story() -> None:
    plan = assembled()
    run = plan["stories"][2]
    run["tasks"][0]["acceptance_criteria"][0]["covers"] = ["RUN-01#ac1"]
    run["tasks"][0]["requirements"] = []
    # REMOVE drops a criterion or requirement from what the tasks must cover.
    _amend(
        run,
        _change("acceptance_criteria", "REMOVE", "RUN-01#ac2", None),
        _change("requirements", "REMOVE", "FR-RUN-01", None),
    )
    assert plan_problems(plan, inventory()) == []
    # REPLACE changes the text of a criterion or requirement; it stays required.
    _amend(
        run,
        _change("acceptance_criteria", "REPLACE", "RUN-01#ac2", "A trap returns a typed error."),
        _change("requirements", "REPLACE", "FR-RUN-01", "Run Wasm components within limits."),
    )
    assert codes(plan_problems(plan, inventory())) == {
        "CRITERION_UNCOVERED",
        "REQUIREMENT_UNCOVERED",
    }
    run["tasks"][0]["acceptance_criteria"][0]["covers"] = ["RUN-01#ac1", "RUN-01#ac2"]
    run["tasks"][0]["requirements"] = ["FR-RUN-01"]
    assert plan_problems(plan, inventory()) == []
    # ADD makes a new criterion and requirement required (and coverable).
    _amend(
        run,
        _change("acceptance_criteria", "ADD", "RUN-01#ac3", "Fuel exhaustion is reported."),
        _change("requirements", "ADD", "FR-API-01", "The adapter is preserved."),
    )
    assert codes(plan_problems(plan, inventory())) == {
        "CRITERION_UNCOVERED",
        "REQUIREMENT_UNCOVERED",
    }
    run["tasks"][0]["acceptance_criteria"][0]["covers"].append("RUN-01#ac3")
    run["tasks"][0]["requirements"].append("FR-API-01")
    assert plan_problems(plan, inventory()) == []
    # A removed criterion can no longer be claimed.
    _amend(run, _change("acceptance_criteria", "REMOVE", "RUN-01#ac2", None))
    run["tasks"][0]["acceptance_criteria"][0]["covers"] = ["RUN-01#ac1", "RUN-01#ac2"]
    run["tasks"][0]["requirements"] = ["FR-RUN-01"]
    assert codes(plan_problems(plan, inventory())) == {"COVERS_UNKNOWN"}


def test_invalid_amendments_are_named() -> None:
    plan = assembled()
    run = plan["stories"][2]
    _amend(
        run,
        _change("acceptance_criteria", "REMOVE", None, "x"),
        _change("acceptance_criteria", "ADD", "RUN-01#ac1", "x"),
        _change("acceptance_criteria", "REPLACE", "API-01#ac1", "x"),
        _change("acceptance_criteria", "ADD", None, "an unnamed criterion"),
        _change("requirements", "REMOVE", "FR-API-01", None),
        _change("requirements", "ADD", "FR-RUN-01", "again"),
        _change("dependencies", "ADD", "RUN-01", "x"),
        _change("dependencies", "REMOVE", "API-01", None),
        _change("notes", "ADD", None, "A free note is fine."),
    )
    found = [item.code for item in plan_problems(plan, inventory())]
    assert found.count("AMENDMENT_INVALID") == 9


def test_story_prerequisites_are_preserved_by_task_dependencies() -> None:
    plan = assembled()
    tasks = tasks_of(plan)
    # Clearing every dependency would let a story start before its prerequisites.
    for item in tasks.values():
        item["depends_on"] = []
    found = plan_problems(plan, inventory())
    assert codes(found) == {"PREREQUISITE_UNORDERED"}
    assert sorted(item.subject for item in found) == ["API-01", "API-02"]
    # A transitive path is enough: API-02 -> API-01.t2 -> API-01.t1 -> RUN-01.t1.
    plan = assembled()
    tasks = tasks_of(plan)
    tasks["falcone-serverless:API-02.t1"]["depends_on"] = ["falcone-serverless:API-01.t2"]
    assert plan_problems(plan, inventory()) == []
    # Removing the prerequisite with an amendment lifts the obligation.
    tasks["falcone-serverless:API-02.t1"]["depends_on"] = []
    assert codes(plan_problems(plan, inventory())) == {"PREREQUISITE_UNORDERED"}
    _amend(plan["stories"][1], _change("dependencies", "REMOVE", "API-01", None))
    assert plan_problems(plan, inventory()) == []


def test_effective_ids_remove_before_they_add() -> None:
    assert _effective(["A", "B"], frozenset({"A"}), frozenset({"A"})) == ["B", "A"]
    assert _effective(["A", "B"], frozenset({"A"}), frozenset({"C", "B"})) == ["B", "C"]
    assert _effective(["A"], frozenset(), frozenset()) == ["A"]


def test_replacing_a_prerequisite_with_itself_keeps_it() -> None:
    plan = assembled()
    api02 = plan["stories"][1]
    tasks_of(plan)["falcone-serverless:API-02.t1"]["depends_on"] = []
    _amend(api02, _change("dependencies", "REPLACE", "API-01", "API-01"))
    # The no-op replacement is named, and API-01 stays a prerequisite of API-02.
    assert codes(plan_problems(plan, inventory())) == {
        "AMENDMENT_INVALID",
        "PREREQUISITE_UNORDERED",
    }
    # With RUN-01 -> API-02 added, the real story cycle API-02 -> API-01 -> RUN-01 is found.
    _amend(plan["stories"][2], _change("dependencies", "ADD", "API-02", "API-02"))
    found = codes(plan_problems(plan, inventory()))
    assert "STORY_DEPENDENCY_CYCLE" in found and "PREREQUISITE_UNORDERED" in found


def test_amended_story_dependencies_stay_acyclic_and_bind_tasks() -> None:
    plan = assembled()
    run = plan["stories"][2]
    # A task of RUN-01 may depend on API-02 only once an amendment adds that dependency.
    tasks_of(plan)["falcone-serverless:API-02.t1"]["depends_on"] = ["falcone-serverless:API-01.t1"]
    run["tasks"][0]["depends_on"] = ["falcone-serverless:API-02.t1"]
    assert "DEPENDENCY_INCONSISTENT" in codes(plan_problems(plan, inventory()))
    # ... but that amendment inverts API-02 -> API-01 -> RUN-01: a story cycle.
    _amend(run, _change("dependencies", "ADD", "API-02", "API-02"))
    found = codes(plan_problems(plan, inventory()))
    assert "STORY_DEPENDENCY_CYCLE" in found and "DEPENDENCY_CYCLE" in found
    # Dropping API-01's prerequisite RUN-01 too removes the story cycle.
    run["tasks"][0]["depends_on"] = []
    _amend(plan["stories"][0], _change("dependencies", "REPLACE", "RUN-01", "API-02"))
    assert "STORY_DEPENDENCY_CYCLE" in codes(plan_problems(plan, inventory()))
    _amend(plan["stories"][0], _change("dependencies", "REMOVE", "RUN-01", None))
    tasks_of(plan)["falcone-serverless:API-01.t1"]["depends_on"] = []
    # RUN-01 now depends on API-02, so its task must be ordered after API-02's.
    assert codes(plan_problems(plan, inventory())) == {"PREREQUISITE_UNORDERED"}
    run["tasks"][0]["depends_on"] = ["falcone-serverless:API-02.t1"]
    assert plan_problems(plan, inventory()) == []


def test_cross_epic_dependencies_resolve_at_assembly() -> None:
    missing = run_epic()
    missing["stories"][0]["tasks"][0]["key"] = "falcone-serverless:RUN-01.t2"
    with pytest.raises(DecompositionRejected) as rejected:
        assemble_plan(
            inventory(),
            frame(),
            [EpicPart(1, API_PART, api_epic()), EpicPart(2, RUN_PART, missing)],
        )
    assert rejected.value.code == "ASSEMBLY_INVALID"
    assert {item["subject"] for item in rejected.value.details} == {"falcone-serverless:API-01.t1"}


# Assembly ----------------------------------------------------------------------------------

PLAN_SHA256 = "cc96d2137e9f93358566416a9550339cc30215907759eb2e2842658e9b6487cd"
PLAN_JSON_SHA256 = "24fe60a91759944cbb7814be52baaa77c3a551d144692b328c88e7041b4b826a"
MARKDOWN_SHA256 = "11d4efb0c736b253e6b49187a7136882d64f5de781ce9fd96202b19d1b5caca4"


def test_assembly_fills_issue_numbers_and_is_deterministic() -> None:
    plan = assembled()
    reordered = assemble_plan(
        inventory(),
        frame(),
        [EpicPart(2, RUN_PART, run_epic()), EpicPart(1, API_PART, api_epic())],
    )
    assert plan == reordered
    assert [
        (s["story_id"], s["story_issue"], s["epic_issue"], s["part_id"]) for s in plan["stories"]
    ] == [
        ("API-01", 3, 1, API_PART),
        ("API-02", 4, 1, API_PART),
        ("RUN-01", 5, 2, RUN_PART),
    ]
    assert plan["inputs"] == inputs().plan_inputs()
    assert plan_sha256(plan) == PLAN_SHA256
    text = plan_json(plan)
    assert json.loads(text) == plan and text.endswith("}\n")
    assert hashlib.sha256(text.encode()).hexdigest() == PLAN_JSON_SHA256
    markdown = render_markdown(
        plan, decomposition_id=DECOMPOSITION_ID, plan_sha256=PLAN_SHA256, inventory=inventory()
    )
    assert hashlib.sha256(markdown.encode()).hexdigest() == MARKDOWN_SHA256
    assert f"- Plan sha256: `{PLAN_SHA256}`" in markdown
    assert "| `falcone-serverless:API-01.t1` Route the Functions API to the Wasm worker |" in (
        markdown
    )


def test_assembly_rejects_missing_or_mismatched_parts() -> None:
    with pytest.raises(DecompositionRejected) as missing:
        assemble_plan(inventory(), frame(), [EpicPart(1, API_PART, api_epic())])
    assert {item["code"] for item in missing.value.details} == {"ASSEMBLY_PART_MISSING"}
    with pytest.raises(DecompositionRejected) as wrong:
        assemble_plan(
            inventory(),
            frame(),
            [EpicPart(1, RUN_PART, api_epic()), EpicPart(2, RUN_PART, run_epic())],
        )
    assert "ASSEMBLY_PART_INVALID" in {item["code"] for item in wrong.value.details}
    large = api_epic()
    large["stories"][0]["tasks"][0]["what_changes"] = ["x"] * 16
    with pytest.raises(DecompositionRejected) as schema:
        assemble_plan(
            inventory(), frame(), [EpicPart(1, API_PART, large), EpicPart(2, RUN_PART, run_epic())]
        )
    assert schema.value.code == "ASSEMBLY_SCHEMA_INVALID"


def test_markdown_keeps_model_text_inert() -> None:
    plan = assembled()
    plan["frame"]["global_decisions"] = ["<!-- hidden --> | [link](x) `code`"]
    markdown = render_markdown(
        plan,
        decomposition_id=DECOMPOSITION_ID,
        plan_sha256=plan_sha256(plan),
        inventory=inventory(),
    )
    assert "<!--" not in markdown and "[link](x)" not in markdown and "`code`" not in markdown


# Risk preview (design section 7) ------------------------------------------------------------


def test_risk_preview_shows_the_calculated_import_risk() -> None:
    preview = risk_preview(assembled())
    assert [item["key"] for item in preview] == list(tasks_of(assembled()))
    assert {item["calculated_risk"] for item in preview} == {"R1"}
    assert not any(item["escalated"] for item in preview)
    secret = task(
        "falcone-serverless:RUN-01.t1",
        why="Own the secret lifecycle of the host capability bindings.",
    )
    escalated = task_risk(secret)
    assert escalated == {
        "key": "falcone-serverless:RUN-01.t1",
        "repository": "gntik-ai/falcone-serverless",
        "proposed_risk": "R1",
        "calculated_risk": "R3",
        "effective_risk": "R3",
        "escalated": True,
    }
    declared = task_risk(task("falcone-serverless:RUN-01.t1", proposed_risk="R3"))
    assert (declared["calculated_risk"], declared["effective_risk"], declared["escalated"]) == (
        "R1",
        "R3",
        False,
    )
    # falcone-serverless exempts "production" (NEW-222); the falcone repository does not.
    production = {"why": "Harden the production profile."}
    assert task_risk(task("falcone-serverless:RUN-01.t1", **production))["calculated_risk"] == "R1"
    falcone = {"repository": "gntik-ai/falcone", "role": "source", "component": "quotas"}
    assert (
        task_risk(task("falcone-serverless:RUN-01.t1", target=falcone, **production))[
            "calculated_risk"
        ]
        == "R3"
    )
