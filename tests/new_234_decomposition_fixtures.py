"""NEW-234 PR4: a miniature falcone-serverless snapshot (two epics, three stories), its
GitHub issue snapshot, and reviewed frame and epic parts that satisfy every plan rule."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml

from engineering_control.engineering.decomposition import (
    DECOMPOSITION_PART_SCHEMAS,
    DECOMPOSITION_SKILLS,
    DecompositionInputs,
)
from engineering_control.engineering.decomposition_inventory import (
    AGENTS_PATH,
    BACKLOG_PATH,
    DESIGN_PATH,
    IssueSnapshot,
    build_inventory,
)
from engineering_control.engineering.ui_blueprint import load_schema

SOURCE_SHA = "1" * 40
FRAME_PART = "frame-0123456789abcdef"
API_PART = "ep01-0123456789abcdef"
RUN_PART = "ep02-fedcba9876543210"
FRAME_SHA = "f" * 64
DRAFT_SHA = "d" * 64

DESIGN = """# falcone-serverless: Option A implementation specification

Intro paragraph.

## Accepted direction and scope

Option A is accepted.

### Confirmed requirements

Restricted pods only.

## 1. Survey and comparative analysis

Knative was surveyed.

## 2. Functional requirements checklist

### API. API and developer experience

- FR-API-01 and FR-API-02.

### RUN. Runtime and execution

- FR-RUN-01.

## 3. Baseline architecture

### Components

A Rust worker and a Go controller.

```text
## not a heading inside a fence
```

## 4. Detailed backlog

### API. API and developer experience

API-01 and API-02 details.

### RUN. Runtime and execution

RUN-01 details.

## 5. Remaining questions and ownership

Falcone owns quotas.

## 6. Implementation trade-offs and sequence

Milestones M1 and M2.

## Sources and evidence

Links.
"""

AGENTS = "# AGENTS.md\n\nWork in fs-test-* namespaces only.\n"
ADR = (
    "# ADR 0001: execution model option A\n\nStatus: accepted.\n\n"
    "Workers run Wasmtime in restricted pods.\n"
)


def _story_body(
    story: str, criteria: list[str], depends: list[str], requirements: list[str]
) -> str:
    return "\n".join(
        [
            f"<!-- falcone-serverless:{story} -->",
            "",
            "## User story",
            "",
            f"As a developer I want {story}.",
            "",
            "## Acceptance criteria",
            "",
            *(f"- [ ] {item}" for item in criteria),
            "",
            "## Planning",
            "",
            f"- Parent: EP-{story.split('-')[0]}",
            f"- Depends on: {', '.join(depends) or 'None'}",
            "",
            "## Requirements",
            "",
            *(f"- **{item}:** statement." for item in requirements),
        ]
    )


def backlog() -> dict[str, Any]:
    def epic(area: str, title: str, stories: list[str]) -> dict[str, Any]:
        body = (
            f"<!-- falcone-serverless:EP-{area} -->\n\n## Goal\n\n{title}.\n\n## Stories\n\n"
            + "\n".join(f"- {story}" for story in stories)
        )
        return {
            "id": f"EP-{area}",
            "type": "epic",
            "title": f"[falcone-serverless][EP-{area}] {title}",
            "body": body,
            "parent": None,
            "labels": ["falcone-serverless", "type:epic"],
        }

    def story(
        identity: str, title: str, criteria: list[str], depends: list[str], reqs: list[str]
    ) -> dict[str, Any]:
        return {
            "id": identity,
            "type": "story",
            "title": f"[falcone-serverless][{identity}] {title}",
            "body": _story_body(identity, criteria, depends, reqs),
            "parent": f"EP-{identity.split('-')[0]}",
            "labels": ["falcone-serverless", "type:story"],
            "dependencies": depends,
            "effort": "M",
            "phase": "M1",
            "requirements": reqs,
        }

    return {
        "schema_version": 1,
        "project": "falcone-serverless",
        "requirements": [
            {"id": "FR-API-01", "category": "API", "statement": "Preserve the Functions API."},
            {"id": "FR-API-02", "category": "API", "statement": "Provide CLI operations."},
            {"id": "FR-RUN-01", "category": "RUN", "statement": "Run Wasm components."},
        ],
        "issues": [
            epic("API", "API and developer experience", ["API-01", "API-02"]),
            story(
                "API-01",
                "Preserve the Functions API",
                ["Legacy fixtures pass on both backends.", "Unauthorized requests stay denied."],
                ["RUN-01"],
                ["FR-API-01"],
            ),
            story(
                "API-02",
                "Add CLI operations",
                ["CLI deploy and invoke work with Keycloak credentials."],
                ["API-01"],
                ["FR-API-02"],
            ),
            epic("RUN", "Runtime and execution", ["RUN-01"]),
            story(
                "RUN-01",
                "Implement Wasmtime execution",
                ["A component runs with fuel and memory limits.", "A trap returns an error."],
                [],
                ["FR-RUN-01"],
            ),
        ],
    }


def files(**overrides: bytes) -> dict[str, bytes]:
    value = {
        DESIGN_PATH: DESIGN.encode(),
        BACKLOG_PATH: (json.dumps(backlog(), indent=2) + "\n").encode(),
        AGENTS_PATH: AGENTS.encode(),
        "docs/adr/0001-execution-model.md": ADR.encode(),
    }
    value.update(overrides)
    return value


ISSUE_NUMBERS = {"EP-API": 1, "EP-RUN": 2, "API-01": 3, "API-02": 4, "RUN-01": 5}


def issues(document: dict[str, Any] | None = None) -> list[IssueSnapshot]:
    items = (document or backlog())["issues"]
    return [
        IssueSnapshot(
            number=ISSUE_NUMBERS[item["id"]],
            title=item["title"],
            body=item["body"],
            labels=tuple(item["labels"]),
            parent=ISSUE_NUMBERS[item["parent"]] if item["parent"] else None,
        )
        for item in items
    ]


def inventory() -> dict[str, Any]:
    return build_inventory(source_sha=SOURCE_SHA, files=files(), issues=issues())


def inputs() -> DecompositionInputs:
    return DecompositionInputs.of(inventory())


def frame() -> dict[str, Any]:
    return {
        "contracts": [
            {
                "id": "ctr-worker-invoke",
                "owner_repository": "gntik-ai/falcone-serverless",
                "kind": "wit",
                "path": "wit/invoke.wit",
                "summary": "The worker invocation interface.",
                "consumers": ["gntik-ai/falcone"],
                "sources": ["design §3"],
            },
            {
                "id": "ctr-functions-api",
                "owner_repository": "gntik-ai/falcone",
                "kind": "http-api",
                "path": "services/functions/openapi.yaml",
                "summary": "The public Functions API.",
                "consumers": ["gntik-ai/falcone-serverless", "gntik-ai/falcone-charts"],
                "sources": ["design §4"],
            },
        ],
        "global_decisions": ["Tests run in fs-test-* namespaces with restricted pods."],
    }


def task(key: str, **overrides: Any) -> dict[str, Any]:
    story = key.split(":", 1)[1].split(".", 1)[0]
    value: dict[str, Any] = {
        "key": key,
        "title": f"Implement {key.split(':', 1)[1]}",
        "kind": "feature",
        "target": {
            "repository": "gntik-ai/falcone-serverless",
            "role": "source",
            "component": "worker",
        },
        "why": f"Story {story} needs it.",
        "what_changes": ["Add the worker module."],
        "out_of_scope": [],
        "acceptance_criteria": [{"criterion": "It works.", "covers": [f"{story}#ac1"]}],
        "requirements": [],
        "interfaces": {"provides": [], "consumes": []},
        "test_commands": ["cargo test --workspace --locked"],
        "paths": ["worker/src/lib.rs"],
        "depends_on": [],
        "risks": [],
        "proposed_risk": "R1",
        "risk_rationale": "A bounded change.",
        "size": "S",
        "estimated_files": 2,
        "rollout": [],
        "kill_switch": [],
        "open_questions": [],
        "sources": [f"{story}#ac1"],
    }
    value.update(overrides)
    return value


def run_epic() -> dict[str, Any]:
    return {
        "stories": [
            {
                "story_id": "RUN-01",
                "tasks": [
                    task(
                        "falcone-serverless:RUN-01.t1",
                        title="Run Wasm components with fuel and memory limits",
                        acceptance_criteria=[
                            {
                                "criterion": "A component runs within its limits.",
                                "covers": ["RUN-01#ac1", "RUN-01#ac2"],
                            }
                        ],
                        requirements=["FR-RUN-01"],
                        interfaces={"provides": ["ctr-worker-invoke"], "consumes": []},
                    )
                ],
                "story_amendment": None,
            }
        ]
    }


def api_epic() -> dict[str, Any]:
    falcone = {"repository": "gntik-ai/falcone", "role": "source", "component": "functions-api"}
    charts = {"repository": "gntik-ai/falcone-charts", "role": "deployment", "component": "chart"}
    return {
        "stories": [
            {
                "story_id": "API-01",
                "tasks": [
                    task(
                        "falcone-serverless:API-01.t1",
                        title="Route the Functions API to the Wasm worker",
                        target=falcone,
                        acceptance_criteria=[
                            {
                                "criterion": "Legacy fixtures pass and denials stay.",
                                "covers": ["API-01#ac1", "API-01#ac2"],
                            }
                        ],
                        requirements=["FR-API-01"],
                        interfaces={
                            "provides": ["ctr-functions-api"],
                            "consumes": ["ctr-worker-invoke"],
                        },
                        test_commands=["pnpm run test:contracts"],
                        paths=["services/functions/src/router.mjs"],
                        depends_on=["falcone-serverless:RUN-01.t1"],
                    ),
                    task(
                        "falcone-serverless:API-01.t2",
                        title="Deploy the worker pool with the chart",
                        target=charts,
                        acceptance_criteria=[
                            {"criterion": "The chart renders.", "covers": ["API-01#ac1"]}
                        ],
                        interfaces={"provides": [], "consumes": ["ctr-functions-api"]},
                        test_commands=["helm lint charts/in-falcone"],
                        paths=["charts/in-falcone/values.yaml"],
                        depends_on=["falcone-serverless:API-01.t1"],
                        kind="chore",
                    ),
                ],
                "story_amendment": None,
            },
            {
                "story_id": "API-02",
                "tasks": [
                    task(
                        "falcone-serverless:API-02.t1",
                        title="Add CLI deploy and invoke",
                        target=falcone,
                        requirements=["FR-API-02"],
                        test_commands=["pnpm run test:unit"],
                        paths=["services/cli/src/deploy.mjs"],
                        depends_on=["falcone-serverless:API-01.t1"],
                    )
                ],
                "story_amendment": None,
            },
        ]
    }


def _bound(kind: str, part: str, **extra: Any) -> dict[str, Any]:
    return {**inputs().plan_inputs(), "part_id": part, **extra}


def frame_output(content: dict[str, Any] | None = None, part: str = FRAME_PART) -> dict[str, Any]:
    return {
        "status": "COMPLETE",
        "project": "falcone-serverless",
        "inputs": _bound("frame", part),
        "frame": frame() if content is None else content,
        "blockers": [],
    }


def epic_output(
    content: dict[str, Any], part: str, frame_sha256: str = FRAME_SHA
) -> dict[str, Any]:
    return {
        "status": "COMPLETE",
        "project": "falcone-serverless",
        "inputs": _bound("epic", part, frame_sha256=frame_sha256),
        "epic": content,
        "blockers": [],
    }


def review_output(
    part: str, draft_sha256: str = DRAFT_SHA, frame_sha256: str | None = None
) -> dict[str, Any]:
    return {
        "status": "COMPLETE",
        "project": "falcone-serverless",
        "inputs": _bound("review", part, frame_sha256=frame_sha256, draft_sha256=draft_sha256),
        "review": {"decision": "APPROVE", "summary": "Every criterion is covered.", "findings": []},
        "blockers": [],
    }


def mutated(value: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(value)


def build_decomposition_pack(
    root: Path, *, version: str = "1.7.0", skip: tuple[str, ...] = ()
) -> Path:
    """A minimal released agent pack with the two decomposition skills (synced schemas)."""
    written = {
        "profiles/engineering.yaml": f"schema_version: 1\nid: engineering\nversion: {version}\n",
        "skills/catalog.yaml": yaml.safe_dump(
            {
                "skills": [
                    {
                        "id": skill,
                        "role": role,
                        **(
                            {"part_output_schemas": dict(DECOMPOSITION_PART_SCHEMAS[skill])}
                            if skill in DECOMPOSITION_PART_SCHEMAS
                            else {}
                        ),
                    }
                    for skill, (role, _schema) in DECOMPOSITION_SKILLS.items()
                ]
            }
        ),
        "domain-packs/common/POLICIES.md": "# Policy\n",
        "domain-packs/falcone-serverless/domains.yaml": "project: falcone-serverless\n",
    }
    for skill, (role, schema) in DECOMPOSITION_SKILLS.items():
        written[f"skills/{skill}/SKILL.md"] = f"# {skill}\n"
        written[f"skills/{skill}/references/contract.md"] = "# Contract\n"
        written[f"skills/{skill}/references/output.schema.json"] = json.dumps(load_schema(schema))
        for part in DECOMPOSITION_PART_SCHEMAS.get(skill, {}).values():
            written[f"skills/{skill}/references/{part}"] = json.dumps(load_schema(part))
        written[f"roles/{role}.yaml"] = f"id: {role}\n"
        written[f"generated/claude/agents/{role}.md"] = f"# {role}\n"
        written[f"generated/codex/agents/{role}.toml"] = f'id = "{role}"\n'
    for relative, text in written.items():
        if relative in skip:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
