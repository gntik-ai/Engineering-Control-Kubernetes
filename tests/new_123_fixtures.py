"""NEW-123 test fixtures: a schema-valid blueprint, its provider outputs and a fake pack."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from engineering_control.engineering.ui_blueprint import UI_PLAN_SKILLS, load_schema

SOURCE_SHA = "a" * 40
INVENTORY_SHA = "b" * 64
KIT_SHA = "c" * 64
KIT_VERSION = "0.1.0-canary.7"
EPIC_KINDS = (
    "scaffold",
    "console-api",
    "shell-navigation-auth",
    "i18n",
    "playwright-harness",
    "csp-assets",
    "image-chart",
)
CHECKS = (
    "data",
    "action",
    "error-empty",
    "accessibility",
    "kit-conformance",
    "runtime-health",
    "responsive",
    "visual-review",
)


def page(
    page_id: str,
    wave: str | None,
    gap_ids: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
    disposition: str = "MIGRATE",
) -> dict[str, Any]:
    deleted = disposition == "DELETE"
    return {
        "page_id": page_id,
        "route": f"/{page_id}",
        "legacy_files": [f"apps/web-console/src/pages/{page_id}.tsx"],
        "reachability": "ORPHAN" if deleted else "ROUTED",
        "disposition": disposition,
        "disposition_reason": "Orphan page, never routed." if deleted else "A routed page.",
        "target": {
            "template": None if deleted else "list-page",
            "layout": None if deleted else "app-shell",
            "blocks": [] if deleted else ["data-table"],
            "components": [] if deleted else ["button"],
            "wrapped": [],
            "justification": "A list of records with a create action.",
        },
        "behaviour": {
            "actions": [
                {
                    "name": "Create",
                    "trigger": "button",
                    "endpoint_refs": ["POST /v1/tenants"],
                    "permission": "SA",
                }
            ],
            "validations": [{"field": "name", "rule": "required"}],
            "permissions": [{"guard": "auth", "rule": "signed-in operators only"}],
            "states": [{"state": "loading", "expected": "a skeleton table"}],
        },
        "endpoints": [
            {
                "method": "GET",
                "path": "/v1/tenants",
                "xcheck_class": "OK",
                "inventory_ref": "pages[0].endpoints[0]",
            }
        ],
        "i18n": {
            "namespace": page_id.replace("-", ""),
            "string_scope": "every label and message",
            "estimated_strings": 20,
            "locales": ["es", "en"],
            "default_locale": "es",
        },
        "complexity": {"tier": "S", "page_loc": 120, "closure_loc": 400},
        "wave": None if deleted else wave,
        "parity_acceptance": []
        if deleted
        else [
            {
                "check": check,
                "criterion": f"{check} matches the legacy page",
                "fixtures": ["t.json"],
            }
            for check in CHECKS
        ],
        "gap_ids": list(gap_ids),
        "legacy_tests": {"vitest": ["TenantsPage.test.tsx"], "playwright_journeys": []},
        "depends_on": list(depends_on),
    }


def blueprint() -> dict[str, Any]:
    return {
        "foundation_epics": [
            {
                "id": f"u0-{kind}",
                "kind": kind,
                "title": f"Foundation {kind}",
                "scope": [f"Deliver the {kind} foundation."],
                "acceptance_criteria": [f"The {kind} foundation passes its CI check."],
                "depends_on": [] if kind == "scaffold" else ["u0-scaffold"],
                "gap_ids": [],
            }
            for kind in EPIC_KINDS
        ],
        "pages": [
            page("tenants", "u1", ("gap-plans-api", "gap-datatable-pagination"), ("u0-scaffold",)),
            page("workspaces", "u2", (), ("tenants",)),
            page("old-reference", None, disposition="DELETE"),
        ],
        "gaps": [
            {
                "id": "gap-plans-api",
                "type": "BACKEND_GAP",
                "target_repository": "gntik-ai/falcone",
                "summary": "The plans surface is routed but not in the public catalogue.",
                "evidence": ["xcheck ROUTED_NOT_PUBLISHED GET /v1/plans"],
                "blocks": ["tenants"],
                "interim": "none: the page waits for the endpoint",
                "seed_ref": "falcone-1",
            },
            {
                "id": "gap-datatable-pagination",
                "type": "KIT_GAP",
                "target_repository": "gntik-ai/gntik-ui",
                "summary": "DataTable has no server-side pagination.",
                "evidence": ["list_kit data-table props"],
                "blocks": ["tenants"],
                "interim": "DataTable with paginated false plus PaginationFooter",
                "seed_ref": "gntik-ui-4",
            },
            {
                "id": "gap-usage-api",
                "type": "BACKEND_GAP",
                "target_repository": "gntik-ai/falcone",
                "summary": "The workspace usage endpoint has no route.",
                "evidence": ["xcheck NO_ROUTE GET /v1/workspaces/{}/usage"],
                "blocks": ["workspaces"],
                "interim": "none",
                "seed_ref": None,
            },
        ],
        "dead_modules": [
            {"path": "apps/web-console/src/old.ts", "decision": "DELETE", "reason": "unused"}
        ],
        "cutover": {
            "id": "u5-cutover",
            "wave": "u5",
            "steps": [
                {"action": "switch-default-route", "detail": "Serve the new console at /."},
                {"action": "keep-legacy-one-release", "detail": "Keep /legacy for a release."},
                {"action": "delete-legacy-and-dead-modules", "detail": "Delete the old app."},
            ],
            "depends_on_pages": ["tenants", "workspaces"],
            "requires_parity_pass": True,
            "requires_i18n_complete": True,
            "blocking_gaps": [
                {"gap_id": "gap-plans-api", "resolution": "RESOLVED", "reason": "needed"},
                {
                    "gap_id": "gap-datatable-pagination",
                    "resolution": "DEFERRED",
                    "reason": "the interim composite is acceptable",
                },
            ],
            "acceptance_criteria": ["Every route serves the new console."],
        },
    }


def inputs(draft_sha256: str | None = None) -> dict[str, Any]:
    return {
        "inventory_sha256": INVENTORY_SHA,
        "legacy_source_sha": SOURCE_SHA,
        "gntik_ui_version": KIT_VERSION,
        "kit_source": "snapshot",
        "kit_snapshot_sha256": KIT_SHA,
        "draft_sha256": draft_sha256,
    }


def draft_output(value: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "COMPLETE",
        "mode": "draft",
        "project": "falcone",
        "inputs": inputs(),
        "blueprint": value or blueprint(),
        "review": None,
        "blockers": [],
    }


def review_output(draft_sha256: str, decision: str = "APPROVE") -> dict[str, Any]:
    findings = (
        []
        if decision == "APPROVE"
        else [
            {
                "category": "behaviour_gap",
                "subject": "tenants",
                "detail": "The 403 state is missing.",
                "reference": "pages[0].behaviour.states",
            }
        ]
    )
    return {
        "status": "COMPLETE",
        "mode": "review",
        "project": "falcone",
        "inputs": inputs(draft_sha256),
        "blueprint": None,
        "review": {"decision": decision, "summary": f"{decision} summary", "findings": findings},
        "blockers": [],
    }


def gap_output(item_key: str, finding_type: str, repository: str) -> dict[str, Any]:
    return {
        "decision": "DRAFTED",
        "finding_ref": item_key,
        "finding_type": finding_type,
        "source_flow": "A",
        "target": {"repository": repository, "reason": "routing rule"},
        "change_request": {
            "title": f"Close {item_key.removeprefix('gap:')}",
            "kind": "feature",
            "why": "The migrated page needs it.",
            "what_changes": ["Add the missing capability."],
            "out_of_scope": [],
            "affected_repositories": [{"repository": repository, "role": "source"}],
            "acceptance_criteria": ["A positive test passes."],
            "risks": [],
            "rollout": [],
            "kill_switch": [],
            "open_questions": [],
            "proposed_risk": "R1",
            "risk_rationale": "A bounded addition.",
        },
        "openspec": {
            "change_id": "close-gap",
            "capability": "ui-gap",
            "requirements": [
                {
                    "name": "Gap closed",
                    "statement": "The system SHALL provide it.",
                    "scenarios": [
                        {"name": "ok", "given": ["a user"], "when": "they ask", "then": ["ok"]}
                    ],
                }
            ],
        },
        "backend_contract": None,
        "kit_proposal": None,
        "ui_defect": None,
        "backend_change": finding_type == "BACKEND_GAP",
        "blocking": False,
        "labels": ["ui-migration"],
        "dependencies": {"blocks": [], "blocked_by": []},
        "duplicate_search": None,
        "publication": "DRAFT_ONLY",
        "notes": [],
    }


def build_fake_pack(root: Path, *, version: str = "1.4.0", skip: tuple[str, ...] = ()) -> Path:
    """A minimal released agent pack with the two UI skills (and their synced schemas)."""
    files = {
        "profiles/engineering.yaml": f"schema_version: 1\nid: engineering\nversion: {version}\n",
        "skills/catalog.yaml": yaml.safe_dump(
            {
                "skills": [
                    {"id": skill, "role": role} for skill, (role, _schema) in UI_PLAN_SKILLS.items()
                ]
            }
        ),
    }
    for skill, (role, schema) in UI_PLAN_SKILLS.items():
        files[f"skills/{skill}/SKILL.md"] = f"# {skill}\n"
        files[f"skills/{skill}/references/contract.md"] = "# Contract\n"
        files[f"skills/{skill}/references/output.schema.json"] = json.dumps(load_schema(schema))
        files[f"roles/{role}.yaml"] = f"id: {role}\n"
        files[f"generated/claude/agents/{role}.md"] = f"# {role}\n"
        files[f"generated/codex/agents/{role}.toml"] = f'id = "{role}"\n'
    for relative, text in files.items():
        if relative in skip:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root
