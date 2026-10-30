"""NEW-242: falcone-workflow, the sixth Hermes project, and the backlog decomposition
generalised from falcone-serverless to a per-project profile.

Covers the registration (registry, enum, CLI, API schemas, write policy, project manifest,
R3 exemptions), the falcone-workflow decomposition profile (issue marker, title prefix and
task key over samples shaped like gntik-ai/falcone-workflow's docs/backlog.json, an
inventory built end to end, the cross-repository targets and the agent-pack gate) and pins
falcone-serverless's decomposition byte for byte: the values below were computed with the
same fixtures on origin/main c8f3bb6, before the profile existed, so its prompts, part ids,
plan and pack hashes are unchanged (approved plan 55292b52 keeps publishing).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from new_234_decomposition_fixtures import build_decomposition_pack
from new_234_decomposition_fixtures import inventory as serverless_inventory
from new_234_fanout_fixtures import FanoutHarness
from new_234_plan_fixtures import PlanHarness, drive
from pydantic import ValidationError

from engineering_control import projects
from engineering_control.api.schemas import (
    ChangeRequestCreate,
    ChangeRequestPublication,
    DecompositionCreate,
)
from engineering_control.cli import parser
from engineering_control.db.models import BacklogDecomposition
from engineering_control.engineering.backlog import (
    calculated_issue_risk,
    canonical_sha256,
    repository_r3_keyword_exemptions,
)
from engineering_control.engineering.change_requests import (
    CROSS_REPOSITORY_TARGETS,
    target_repository_allowed,
    validate_proposal,
)
from engineering_control.engineering.decomposition import (
    DECOMPOSITION_PROJECTS,
    FALCONE_SERVERLESS,
    FALCONE_WORKFLOW,
    PROJECT,
    TASK_KEY,
    TASK_TARGETS,
    TEST_COMMANDS,
    DecompositionInputs,
    DecompositionProfile,
    decomposition_pack_problems,
    decomposition_profile,
    pack_revision,
    plan_sha256,
)
from engineering_control.engineering.decomposition_amendments import _SOURCE_REPOSITORIES
from engineering_control.engineering.decomposition_fanout import (
    amendment_marker,
    fanout_tasks,
    short_key,
    task_context,
    task_description,
    task_lineage,
)
from engineering_control.engineering.decomposition_inventory import (
    AGENTS_PATH,
    BACKLOG_PATH,
    DESIGN_PATH,
    ISSUE_MARKER,
    DecompositionSourceError,
    IssueSnapshot,
    build_inventory,
    frame_input,
)
from engineering_control.engineering.toolchain import load_project_manifest
from engineering_control.models.enums import Project
from engineering_control.policy.github_write import (
    HISTORICAL_WRITE_REPOSITORIES,
    PREVIOUS_WRITE_REPOSITORIES,
    WRITE_REPOSITORIES,
    GitHubWriteDenialReason,
    GitHubWritePolicyEvaluator,
)
from engineering_control.workflows.cleanup_scope import CLEANUP_REPOSITORY_PROJECTS

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = "falcone-workflow"
REPOSITORY = "gntik-ai/falcone-workflow"
SOURCE_SHA = "2" * 40

# -- registration -----------------------------------------------------------------------


def test_falcone_workflow_is_a_registered_self_contained_project() -> None:
    assert projects.PROJECTS[-1] == WORKFLOW
    assert Project.FALCONE_WORKFLOW.value == WORKFLOW
    assert projects.PROJECT_REPOSITORIES[WORKFLOW] == (REPOSITORY, REPOSITORY)
    assert WORKFLOW in projects.SELF_CONTAINED
    assert WORKFLOW not in projects.GLOBAL_PROJECTS
    assert projects.default_branch(WORKFLOW) == "main"
    assert projects.REPOSITORY_PROJECTS[WORKFLOW] == WORKFLOW
    assert CLEANUP_REPOSITORY_PROJECTS[WORKFLOW] == WORKFLOW


def test_the_cli_offers_falcone_workflow_decompositions() -> None:
    cli = parser()
    assert cli.parse_args(["backlog-decompose", WORKFLOW, "--epic", "EP-DUR"]).project == WORKFLOW
    assert cli.parse_args(["backlog-decompositions", "--project", WORKFLOW]).project == WORKFLOW
    with pytest.raises(SystemExit):
        cli.parse_args(["backlog-decompose", "falcone"])


def test_the_api_schemas_accept_falcone_workflow() -> None:
    assert DecompositionCreate(project=WORKFLOW, epics=["EP-DUR", "3"]).project == WORKFLOW
    with pytest.raises(ValidationError):
        DecompositionCreate.model_validate({"project": "falcone"})
    request = ChangeRequestCreate(
        project=WORKFLOW, description="Add task leases.", target_repository=REPOSITORY
    )
    assert request.target_repository == REPOSITORY
    published = ChangeRequestPublication(
        issue_url=f"https://github.com/{REPOSITORY}/issues/12",
        approval_binding="a" * 64,
        decision_sequence=1,
    )
    assert published.issue_url.endswith("/issues/12")


def _evaluator(tmp_path: Path, repositories: list[str]) -> GitHubWritePolicyEvaluator:
    config = tmp_path / "etc"
    (config / "policy").mkdir(parents=True)
    (config / "projects.d").mkdir()
    policy = config / "policy" / "github-issue-write-validation.yaml"
    policy.write_text(
        yaml.safe_dump({"enabled": True, "safe_repositories": repositories}), encoding="utf-8"
    )
    policy.chmod(0o640)
    (config / "policy").chmod(0o750)
    return GitHubWritePolicyEvaluator(
        policy_path=policy,
        project_config_root=config / "projects.d",
        production=False,
        writes_enabled=True,
    )


def test_the_write_policy_allows_its_repository_and_keeps_the_earlier_allowlists(
    tmp_path: Path,
) -> None:
    assert REPOSITORY in WRITE_REPOSITORIES
    assert PREVIOUS_WRITE_REPOSITORIES == WRITE_REPOSITORIES - {REPOSITORY}
    # Every allowlist an earlier release installed stays accepted (never rotated out).
    assert HISTORICAL_WRITE_REPOSITORIES[0] == PREVIOUS_WRITE_REPOSITORIES
    assert len(HISTORICAL_WRITE_REPOSITORIES) == 3
    evaluator = _evaluator(tmp_path, sorted(WRITE_REPOSITORIES))
    assert evaluator.evaluate(lambda: (REPOSITORY,), project=WORKFLOW) is None
    denied = evaluator.evaluate(lambda: ("gntik-ai/falcone",), project=WORKFLOW)
    assert denied is not None
    assert denied.reason_code == GitHubWriteDenialReason.REPOSITORY_OUTSIDE_PROJECT


def test_the_manifest_is_merge_only_with_node_24_and_pinned_pnpm() -> None:
    manifest = load_project_manifest(ROOT / "config" / "projects", WORKFLOW)
    assert str(manifest.repositories.source.full_name) == REPOSITORY
    assert manifest.repositories.source.default_branch == "main"
    raw = yaml.safe_load((ROOT / "config" / "projects" / f"{WORKFLOW}.yaml").read_text())
    assert raw["deployment"] == {"strategy": "none", "adapter": "none"}
    merge = raw["delivery_control"]["merge_delivery"]
    assert merge["enabled"] is True and merge["kind"] == "protected-merge"
    assert merge["allow_r3_merge"] is False and merge["maximum_concurrency"] == 1
    toolchain = manifest.toolchain
    assert toolchain is not None
    # Disabled until the scaffold commits package.json and pnpm-lock.yaml (an enabled
    # toolchain needs a prepare step); the node/pnpm selection still reaches the agents.
    assert (toolchain.enabled, toolchain.node, toolchain.pnpm) == (False, "24", "11.28.3")
    assert (toolchain.rust, toolchain.go) == (None, None)
    assert not toolchain.default_node_toolchain


def test_production_is_exempt_but_security_signals_stay_r3() -> None:
    assert repository_r3_keyword_exemptions(REPOSITORY) == {"production"}
    title = "[falcone-workflow][DUR-01] Implement the transactional run state machine"
    plain = calculated_issue_risk(
        REPOSITORY,
        ("type:story",),
        title,
        "No production in-memory fallback exists when PostgreSQL is unavailable.",
    )
    assert plain != "R3"
    for body in (
        "Plan the data migration of every run.",
        "Define the secret lifecycle of worker credentials.",
        "Prevent a tenant isolation breach between workflow runs.",
    ):
        assert calculated_issue_risk(REPOSITORY, ("type:story",), title, body) == "R3", body


# -- the decomposition profile ----------------------------------------------------------

EPIC_BODY = (
    "<!-- falcone-workflow:EP-DUR -->\n\n## Goal\n\nMake PostgreSQL transactions the "
    "authoritative boundary for workflow progress.\n\n## Stories\n\n"
    "- DUR-01 — Implement the transactional run state machine\n"
    "- DUR-02 — Add task leases, fencing and recovery scans\n"
)


def _story_body(story: str, epic: str, criteria: list[str], depends: list[str]) -> str:
    requirements = [f"FR-{story}"]
    return "\n".join(
        [
            f"<!-- falcone-workflow:{story} -->",
            "",
            "## User story",
            "",
            f"As a platform engineer, I want {story} so that accepted work survives.",
            "",
            "## Acceptance criteria",
            "",
            *(f"- [ ] {item}" for item in criteria),
            "",
            "## Planning",
            "",
            f"- Parent: {epic}",
            "- Phase: M1",
            "- Effort: L",
            f"- Depends on: {', '.join(depends) or 'None'}",
            "",
            "## Requirements",
            "",
            *(f"- **{item}:** The system must keep {story}." for item in requirements),
        ]
    )


STORIES = {
    "DSL-01": (
        "EP-DSL",
        "Pin the workflow DSL contract",
        ["Every published definition validates against the pinned schema."],
        [],
    ),
    "DUR-01": (
        "EP-DUR",
        "Implement the transactional run state machine",
        [
            "Fault injection before/after commit proves that acknowledged starts remain "
            "discoverable.",
            "Two evaluators racing one revision produce exactly one committed transition.",
            "No production in-memory fallback exists when PostgreSQL is unavailable.",
        ],
        ["DSL-01"],
    ),
    "DUR-02": (
        "EP-DUR",
        "Add task leases, fencing and recovery scans",
        ["Kill a worker after claim and verify another claims after expiry."],
        ["DUR-01"],
    ),
}
EPICS = {"EP-DSL": "Workflow DSL compatibility", "EP-DUR": "Durable state and recovery"}
NUMBERS = {"EP-DSL": 1, "EP-DUR": 2, "DSL-01": 3, "DUR-01": 4, "DUR-02": 5}


def _backlog() -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    for epic, title in EPICS.items():
        area = epic.removeprefix("EP-")
        children = [story for story, (parent, *_rest) in STORIES.items() if parent == epic]
        issues.append(
            {
                "id": epic,
                "type": "epic",
                "title": f"[falcone-workflow][{epic}] {title}",
                "body": EPIC_BODY
                if epic == "EP-DUR"
                else f"<!-- falcone-workflow:{epic} -->\n\n## Goal\n\n{title}.\n\n## Stories\n\n"
                + "\n".join(f"- {story}" for story in children),
                "parent": None,
                "labels": ["falcone-workflow", "type:epic", f"area:{area.lower()}"],
            }
        )
        for story in children:
            _parent, story_title, criteria, depends = STORIES[story]
            issues.append(
                {
                    "id": story,
                    "type": "story",
                    "title": f"[falcone-workflow][{story}] {story_title}",
                    "body": _story_body(story, epic, criteria, depends),
                    "parent": epic,
                    "requirements": [f"FR-{story}"],
                    "dependencies": depends,
                    "phase": "M1",
                    "effort": "L",
                    "labels": ["falcone-workflow", "type:story", f"area:{area.lower()}"],
                }
            )
    return {
        "schema_version": 1,
        "project": WORKFLOW,
        "status": "proposed",
        "baseline_commit": "47674add95223e6ccd7f6f0dcd11106469a0a30e",
        "requirements": [
            {"id": f"FR-{story}", "category": story[:3], "statement": f"Keep {story}."}
            for story in STORIES
        ],
        "issues": issues,
    }


DESIGN = """# falcone-workflow: native workflow engine specification

Scope paragraph.

## 1. Survey

Temporal was surveyed.

## 2. Functional requirements checklist

### DSL. Workflow DSL compatibility

- FR-DSL-01.

### DUR. Durable state and recovery

- FR-DUR-01 and FR-DUR-02.

## 3. Baseline architecture

A TypeScript engine and trusted activity workers.

## 4. Detailed backlog

### DSL. Workflow DSL compatibility

DSL-01 details.

### DUR. Durable state and recovery

DUR-01 and DUR-02 details.

## 5. Ownership

Falcone owns the facade.

## 6. Delivery gates

M0 then M1.
"""


def _files() -> dict[str, bytes]:
    return {
        "docs/falcone-workflow-design.md": DESIGN.encode(),
        BACKLOG_PATH: (json.dumps(_backlog(), indent=2) + "\n").encode(),
        AGENTS_PATH: b"# AGENTS.md\n\nTypeScript on Node 24.\n",
    }


def _issues() -> list[IssueSnapshot]:
    return [
        IssueSnapshot(
            number=NUMBERS[item["id"]],
            title=item["title"],
            body=item["body"],
            labels=tuple(item["labels"]),
            parent=NUMBERS[item["parent"]] if item["parent"] else None,
        )
        for item in _backlog()["issues"]
    ]


def test_the_profiles_derive_marker_title_and_task_key_from_the_project() -> None:
    assert DECOMPOSITION_PROJECTS == {"falcone-serverless", WORKFLOW}
    profile = decomposition_profile(WORKFLOW)
    assert profile is FALCONE_WORKFLOW
    assert profile.design_path == "docs/falcone-workflow-design.md"
    assert profile.source_repository == REPOSITORY
    assert profile.task_key_format == "falcone-workflow:<STORY>.t<n>"
    assert profile.issue_marker.findall(EPIC_BODY) == ["EP-DUR"]
    body = _story_body("DUR-01", "EP-DUR", ["One."], ["DSL-01"])
    assert profile.issue_marker.findall(body) == ["DUR-01"]
    assert profile.issue_marker.findall("<!--  falcone-workflow:CON-01  -->") == ["CON-01"]
    # Another project's marker is never this project's.
    assert profile.issue_marker.findall("<!-- falcone-serverless:API-01 -->") == []
    assert FALCONE_SERVERLESS.issue_marker.findall(body) == []
    title = "[falcone-workflow][DUR-01] Implement the transactional run state machine"
    assert profile.title_prefix.sub("", title) == "Implement the transactional run state machine"
    assert profile.task_key.fullmatch("falcone-workflow:DUR-01.t2").groups() == ("DUR-01", "2")
    for key in (
        "falcone-workflow:DUR-01.t0",
        "falcone-workflow:DUR-01.t01",
        "falcone-serverless:DUR-01.t1",
        "falcone-workflowX:DUR-01.t1",
    ):
        assert profile.task_key.fullmatch(key) is None, key
    assert short_key("falcone-workflow:DUR-02.t3") == "DUR-02.t3"
    assert short_key("falcone-serverless:API-01.t2") == "API-01.t2"
    with pytest.raises(ValueError):
        short_key("falcone:API-01.t2")
    marker = amendment_marker(decomposition_id="d", story_id="DUR-01", project=WORKFLOW)
    assert marker == "<!-- hermes-decomposition-amendment plan=d story=falcone-workflow:DUR-01 -->"
    with pytest.raises(ValueError, match="not configured"):
        decomposition_profile("falcone")


def test_falcone_workflow_targets_are_its_engine_and_the_shared_cross_repositories() -> None:
    assert CROSS_REPOSITORY_TARGETS[WORKFLOW] == CROSS_REPOSITORY_TARGETS["falcone-serverless"]
    assert dict(FALCONE_WORKFLOW.task_targets) == {
        REPOSITORY: ("source", frozenset({"engine", "worker"})),
        "gntik-ai/falcone": (
            "source",
            frozenset({"workflow-facade", "routing", "console", "quotas", "admission"}),
        ),
        "gntik-ai/falcone-charts": ("deployment", frozenset({"chart"})),
    }
    assert FALCONE_WORKFLOW.test_commands[REPOSITORY] == (
        "pnpm run lint",
        "pnpm run typecheck",
        "pnpm run test",
    )
    for repository in ("gntik-ai/falcone", "gntik-ai/falcone-charts"):
        assert FALCONE_WORKFLOW.test_commands[repository] == TEST_COMMANDS[repository]
        assert target_repository_allowed(WORKFLOW, repository)
    assert not target_repository_allowed(WORKFLOW, "gntik-ai/musematic")
    assert not target_repository_allowed(WORKFLOW, "gntik-ai/falcone-serverless")
    proposal = {
        "title": "Route workflow starts to the native engine",
        "kind": "feature",
        "why": "The facade must reach the native engine.",
        "what_changes": ["Add the engine route behind the workflow facade."],
        "out_of_scope": ["Temporal removal."],
        "affected_repositories": [{"repository": "gntik-ai/falcone", "role": "source"}],
        "acceptance_criteria": ["A start reaches the engine."],
        "risks": ["A misrouted start."],
        "rollout": ["Behind a flag."],
        "kill_switch": ["Disable the flag."],
        "open_questions": [],
        "proposed_risk": "R2",
        "risk_rationale": "A routing change.",
    }
    assert validate_proposal(proposal, WORKFLOW, "gntik-ai/falcone").title
    # Story amendments edit an issue in a decomposition project's own repository only.
    assert {REPOSITORY, "gntik-ai/falcone-serverless"} == _SOURCE_REPOSITORIES


def test_a_profile_rejects_targets_outside_its_cross_repository_targets() -> None:
    with pytest.raises(RuntimeError, match="not a cross-repository target"):
        DecompositionProfile(
            project=WORKFLOW,
            design_path="docs/x.md",
            task_targets={
                REPOSITORY: ("source", frozenset({"engine"})),
                "gntik-ai/musematic": ("source", frozenset({"api"})),
            },
            test_commands={REPOSITORY: (), "gntik-ai/musematic": ()},
        )
    with pytest.raises(RuntimeError, match="must be its source"):
        DecompositionProfile(
            project=WORKFLOW,
            design_path="docs/x.md",
            task_targets={"gntik-ai/falcone": ("source", frozenset({"routing"}))},
            test_commands={"gntik-ai/falcone": ()},
        )


def test_a_falcone_workflow_inventory_binds_its_backlog_and_issues() -> None:
    inventory = build_inventory(
        source_sha=SOURCE_SHA, files=_files(), issues=_issues(), project=WORKFLOW
    )
    assert inventory["project"] == WORKFLOW
    assert inventory["design"]["path"] == "docs/falcone-workflow-design.md"
    assert [epic["id"] for epic in inventory["epics"]] == ["EP-DSL", "EP-DUR"]
    stories = {story["id"]: story for story in inventory["stories"]}
    assert stories["DUR-01"]["title"] == "Implement the transactional run state machine"
    assert stories["DUR-01"]["dependencies"] == ["DSL-01"]
    assert [item["id"] for item in stories["DUR-01"]["acceptance_criteria"]] == [
        "DUR-01#ac1",
        "DUR-01#ac2",
        "DUR-01#ac3",
    ]
    assert stories["DUR-02"]["epic_issue"] == NUMBERS["EP-DUR"]
    assert DecompositionInputs.of(inventory).project == WORKFLOW
    frame = frame_input(inventory)
    assert frame["project"] == WORKFLOW
    assert frame["task_key_format"] == "falcone-workflow:<STORY>.t<n>"
    assert [item["repository"] for item in frame["repositories"]] == [
        REPOSITORY,
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
    ]
    assert frame["repositories"][0]["components"] == ["engine", "worker"]
    assert frame["test_commands"][REPOSITORY] == list(FALCONE_WORKFLOW.test_commands[REPOSITORY])


def test_a_falcone_workflow_inventory_refuses_other_projects_sources() -> None:
    # The serverless design path is not this project's.
    files = _files()
    files[DESIGN_PATH] = files.pop("docs/falcone-workflow-design.md")
    with pytest.raises(DecompositionSourceError) as missing:
        build_inventory(source_sha=SOURCE_SHA, files=files, issues=_issues(), project=WORKFLOW)
    assert missing.value.details == ["missing docs/falcone-workflow-design.md"]
    # A falcone-serverless backlog is not a falcone-workflow backlog.
    files = _files()
    other = {**_backlog(), "project": "falcone-serverless"}
    files[BACKLOG_PATH] = json.dumps(other).encode()
    with pytest.raises(DecompositionSourceError, match="DECOMPOSITION_SOURCE_INVALID") as wrong:
        build_inventory(source_sha=SOURCE_SHA, files=files, issues=_issues(), project=WORKFLOW)
    assert wrong.value.details == [f"{BACKLOG_PATH} is not a schema 1 falcone-workflow backlog"]
    # Serverless markers on the issues: no issue carries a falcone-workflow marker.
    serverless_issues = [
        IssueSnapshot(
            number=issue.number,
            title=issue.title,
            body=issue.body.replace("falcone-workflow:", "falcone-serverless:"),
            labels=issue.labels,
            parent=issue.parent,
        )
        for issue in _issues()
    ]
    with pytest.raises(DecompositionSourceError, match="DECOMPOSITION_SOURCE_DRIFT"):
        build_inventory(
            source_sha=SOURCE_SHA, files=_files(), issues=serverless_issues, project=WORKFLOW
        )


def test_the_agent_pack_gate_blocks_falcone_workflow_until_the_pack_supports_it(
    tmp_path: Path,
) -> None:
    pack = build_decomposition_pack(tmp_path / "pack")
    assert decomposition_pack_problems(pack) == []
    assert decomposition_pack_problems(pack, project="falcone-serverless") == []
    problems = decomposition_pack_problems(pack, project=WORKFLOW)
    # The released NEW-234 schemas pin project falcone-serverless; the pack has no
    # falcone-workflow domain pack yet.
    assert "backlog-decomposition-frame.schema.json does not accept project " + WORKFLOW in (
        problems
    )
    assert f"missing domain-packs/{WORKFLOW}/domains.yaml" in problems
    # The revision binds the project's domain pack: another project never shares it.
    assert pack_revision(pack) == pack_revision(pack, "falcone-serverless")
    assert pack_revision(pack, WORKFLOW) != pack_revision(pack)


# -- falcone-serverless unchanged ---------------------------------------------------------

# Computed with these fixtures on origin/main c8f3bb6 (before NEW-242).
SERVERLESS_PINS = {
    "inventory_sha256": "811f33b9f2c00ae155e116304056e31ca8d186bc5ff55fa006d32bc3d0fee123",
    "frame_input": "311482cbaa416d7579c4fdf9c0aa2f6e2ed43a16d11dfc6b51dfa81453d3d85d",
    "part_ids": ["frame-113b3701556bd625", "ep02-65b4d5b97643eb11", "ep01-316c27edf208cb99"],
    "plan_sha256": "0b29b5ae410d28c5219f4f573109219532aa0a1384cb7d05453124278750d289",
    "pack_revision": "b4cf73e3a38f2035257069f0ba893e7f73848d41779f8261afcf107f91511a3a",
    # Every draft and review request (prompt, schema, role, files), the random ids masked.
    "requests": "05f476b69b6927de86c7f0e471005946767caeb665eb9907b6cb584549170698",
    "prompts": "94f9a878a62b0e375ab847186ded35610a75bbbf3d429675f8b6059d76e392d3",
    "fanout": "fd24be03398a6da597051869d40310392427c722cfd1b65566a600e5289f8726",
}
# The fan-out's Codex review requests and its summary, ids masked (same origin).
SERVERLESS_FANOUT_REVIEWS = "7af4ebeb94303738492f075ac5857067147f57665bf3e0d967427cca1ec0dba3"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_serverless_module_names_are_its_profile_and_unchanged() -> None:
    assert PROJECT == "falcone-serverless"
    assert DESIGN_PATH == "docs/falcone-serverless-design.md"
    assert TASK_KEY.pattern == r"^falcone-serverless:([A-Z]{2,4}-[0-9]{2})\.t([1-9][0-9]?)$"
    assert ISSUE_MARKER.pattern == (
        r"<!--\s*falcone-serverless:([A-Z]{2,4}-[0-9]{2}|EP-[A-Z]{2,4})\s*-->"
    )
    assert FALCONE_SERVERLESS.title_prefix.pattern == r"^\[falcone-serverless\]\[[A-Z0-9-]+\]\s*"
    assert FALCONE_SERVERLESS.plan_key.pattern == (
        r"^falcone-serverless:([A-Z]{2,4}-[0-9]{2})\.(t[1-9][0-9]?)$"
    )
    assert TASK_TARGETS is FALCONE_SERVERLESS.task_targets
    assert list(TASK_TARGETS) == [
        "gntik-ai/falcone-serverless",
        "gntik-ai/falcone",
        "gntik-ai/falcone-charts",
    ]
    assert TASK_TARGETS["gntik-ai/falcone-serverless"][1] == {"worker", "controller"}
    assert TASK_TARGETS["gntik-ai/falcone"][1] == {"functions-api", "quotas", "admission"}


def test_the_serverless_plan_prompts_parts_and_hashes_are_byte_identical(
    tmp_path: Path,
) -> None:
    harness = PlanHarness(tmp_path)
    created = harness.create()
    identity = str(created["id"])
    assert drive(harness, identity)["status"] == "CANDIDATE"

    def masked(value: Any) -> Any:
        text = json.dumps(value).replace(identity, "<ID>").replace(str(tmp_path), "<TMP>")
        return json.loads(text)

    with harness.repository.sessions() as session:
        row = session.get(BacklogDecomposition, UUID(identity))
        assert row is not None
        plan = dict(row.plan_json or {})
    view = harness.repository.get_sync(UUID(identity))
    assert view is not None
    inventory = serverless_inventory()
    requests = masked(harness.requests)
    found = {
        "inventory_sha256": inventory["inventory_sha256"],
        "frame_input": canonical_sha256(frame_input(inventory)),
        "part_ids": [item["part_id"] for item in view["parts"]],
        "plan_sha256": plan_sha256(plan),
        "pack_revision": pack_revision(harness.pack),
        "requests": _digest(
            [{key: value for key, value in item.items() if key != "run_id"} for item in requests]
        ),
        "prompts": _digest([item["prompt"] for item in requests]),
        "fanout": _digest(
            [
                [
                    task.key,
                    task.short_key,
                    task_description(task, decomposition_id="d"),
                    task_lineage(
                        task,
                        task_context(
                            task,
                            decomposition_id="11111111-1111-4111-8111-111111111111",
                            plan_sha256="a" * 64,
                        ),
                    ),
                ]
                for task in fanout_tasks(plan)
            ]
        ),
    }
    assert found == SERVERLESS_PINS


def test_the_serverless_fanout_reviews_are_byte_identical(tmp_path: Path) -> None:
    harness = FanoutHarness(tmp_path)
    approved = harness.approved()
    result = harness.run_fanout(approved)
    text = json.dumps(
        {
            "reviews": harness.reviews,
            "finalized": result["finalized"],
            "counts": [result["drafted"], result["failed"], result["skipped"]],
        },
        sort_keys=True,
    )
    text = text.replace(str(approved["id"]), "<ID>").replace(str(tmp_path), "<TMP>")
    text = _UUID.sub("<UUID>", text)
    assert hashlib.sha256(text.encode()).hexdigest() == SERVERLESS_FANOUT_REVIEWS
