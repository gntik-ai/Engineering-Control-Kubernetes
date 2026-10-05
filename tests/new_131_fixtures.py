"""NEW-131 test fixtures: a falcone-sized synthetic inventory and judgement part outputs."""

from __future__ import annotations

from typing import Any

from new_123_fixtures import (
    CHECKS,
    EPIC_KINDS,
    INVENTORY_SHA,
    KIT_SHA,
    KIT_VERSION,
    SOURCE_SHA,
)

from engineering_control.engineering.ui_blueprint_parts import BATCH, FRAME, batch_gap_prefix

LEGACY = "apps/web-console"
# NEW-136: the Git tree id of the legacy app (inventory schema 2).
LEGACY_TREE = "7" * 40
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "UNKNOWN")
CLASSES = ("OK", "ROUTED_NOT_PUBLISHED", "NO_ROUTE", "GATEWAY_ONLY")
ROUTED_PAGES = 50
ORPHAN_PAGES = 3
ENDPOINTS = 234


def _file(name: str) -> str:
    return f"{LEGACY}/src/pages/{name}.tsx"


def inventory_document() -> dict[str, Any]:
    """50 routed pages (one nested), 3 orphans, 234 endpoints, each used by at least one
    routed page (some shared, some with the UNKNOWN method), 5 dead modules."""
    names = [f"ConsolePage{index:02d}Page" for index in range(ROUTED_PAGES - 1)]
    names.append("admin/BackupStatusPage")
    orphans = [f"Orphan{index}Page" for index in range(ORPHAN_PAGES)]
    endpoints = [
        {
            "method": METHODS[index % len(METHODS)],
            "path": f"/v1/resources/{index}/items",
            "class": CLASSES[index % len(CLASSES)],
            "sources": {"catalog": index % 2 == 0, "method_unknown": index % 6 == 5},
            "files": [f"{LEGACY}/src/lib/api{index % 7}.ts"],
        }
        for index in range(ENDPOINTS)
    ]
    endpoints.sort(key=lambda item: (item["method"], item["path"]))

    def uses(page_index: int) -> list[dict[str, Any]]:
        chosen = {k for k in range(ENDPOINTS) if k % ROUTED_PAGES == page_index}
        if page_index < 5:  # shared endpoints: the closure of several pages
            chosen |= {0, 1, 2}
        return [
            {"method": endpoints[k]["method"], "path": endpoints[k]["path"],
             "class": endpoints[k]["class"]}
            for k in sorted(chosen)
        ]  # fmt: skip

    pages: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        component = name.rsplit("/", 1)[-1]
        loc = [80, 300, 700, 1500][index % 4]
        pages.append(
            {
                "file": _file(name),
                "component": component,
                "routed": True,
                "routes": [f"/console/p{index}"],
                "guards": ["RequireSuperadminRoute"] if index % 9 == 0 else [],
                "loc": loc,
                "closure_loc": loc * 3,
                "tier": ["small", "medium", "large", "xl"][index % 4],
                "components": [f"{LEGACY}/src/components/console/{component}Table.tsx"],
                "libraries": [f"{LEGACY}/src/lib/api{index % 7}.ts"],
                "endpoints": uses(index),
                "tests": {
                    "vitest": [f"{LEGACY}/src/__tests__/{component}.test.tsx"],
                    "playwright": ["e2e/console.spec.ts"] if index % 10 == 0 else [],
                },
            }
        )
    for index, name in enumerate(orphans):
        pages.append(
            {
                "file": _file(name),
                "component": name,
                "routed": False,
                "routes": [],
                "guards": [],
                "loc": 40 + index,
                "closure_loc": 90,
                "tier": "small",
                "components": [],
                "libraries": [],
                "endpoints": uses(index)[:1],
                "tests": {"vitest": [], "playwright": []},
            }
        )
    pages.sort(key=lambda page: page["file"])
    return {
        "schema_version": 2,
        "project": "falcone",
        "source_sha": SOURCE_SHA,
        "legacy_app": LEGACY,
        "legacy_app_tree": LEGACY_TREE,
        "target_app": "apps/web-console-next",
        "summary": {"page_files": len(pages), "routed_pages": ROUTED_PAGES, "endpoints": ENDPOINTS},
        "routes": [
            {
                "path": page["routes"][0],
                "page": page["file"],
                "component": page["component"],
                "guards": page["guards"],
            }
            for page in pages
            if page["routes"]
        ],  # fmt: skip
        "pages": pages,
        "endpoints": endpoints,
        "dead_modules": [
            {"file": f"{LEGACY}/src/actions/old{index}.mjs", "loc": 10 + index}
            for index in range(5)
        ],
        "orphan_pages": [page["file"] for page in pages if not page["routed"]],
        "inventory_sha256": INVENTORY_SHA,
    }


def kit_document() -> dict[str, Any]:
    def call(tool: str, arguments: dict[str, Any], text: str) -> dict[str, Any]:
        return {"tool": tool, "arguments": arguments, "is_error": False, "text": text,
                "truncated": False, "bytes": len(text)}  # fmt: skip

    return {
        "schema_version": 1,
        "gntik_ui_version": KIT_VERSION,
        "calls": [
            call("list_kit", {"kind": "template"}, "list-page users-list"),
            call("list_kit", {"kind": "block"}, "data-table"),
            call("list_kit", {"kind": "component"}, "button dialog"),
            call("get_rules", {}, "reglas"),
            call("get_app_shell", {"include_html_block": True}, "<shell/>"),
        ],
        "snapshot_sha256": KIT_SHA,
    }


def part_inputs(part_id: str, draft_sha256: str | None = None) -> dict[str, Any]:
    return {
        "inventory_sha256": INVENTORY_SHA,
        "legacy_source_sha": SOURCE_SHA,
        "gntik_ui_version": KIT_VERSION,
        "kit_source": "snapshot",
        "kit_snapshot_sha256": KIT_SHA,
        "part_id": part_id,
        "draft_sha256": draft_sha256,
    }


def gap(gap_id: str, kind: str, blocks: list[str], resolution: str = "RESOLVED") -> dict[str, Any]:
    backend = kind == "BACKEND_GAP"
    return {
        "id": gap_id,
        "type": kind,
        "target_repository": "gntik-ai/falcone" if backend else "gntik-ai/gntik-ui",
        "summary": f"{gap_id} is missing.",
        "evidence": [f"inventory evidence for {gap_id}"],
        "blocks": blocks,
        "interim": "the closest existing composite",
        "seed_ref": None,
        "cutover_resolution": resolution,
        "cutover_reason": f"{gap_id} decided by the frame.",
    }


def frame_content(first_page: str) -> dict[str, Any]:
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
        "gaps": [
            gap("gap-plans-api", "BACKEND_GAP", [first_page]),
            gap("gap-datatable-pagination", "KIT_GAP", [], "DEFERRED"),
        ],
        "cutover": {
            "steps": [
                {"action": "switch-default-route", "detail": "Serve the new console at /."},
                {"action": "keep-legacy-one-release", "detail": "Keep /legacy for a release."},
                {"action": "delete-legacy-and-dead-modules", "detail": "Delete the old app."},
            ],
            "acceptance_criteria": ["Every route serves the new console."],
        },
        "global_decisions": ["Every list page uses DataTable with PaginationFooter."],
    }


def page_judgement(page_id: str, *, gap_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "page_id": page_id,
        "disposition": "MIGRATE",
        "disposition_reason": "A routed page.",
        "target": {
            "template": "list-page",
            "layout": None,
            "blocks": ["data-table"],
            "components": ["button"],
            "wrapped": [],
            "justification": "A list of records with a create action.",
        },
        "behaviour": {
            "actions": [
                {"name": "Open", "trigger": "on-load", "endpoint_refs": [], "permission": None}
            ],
            "validations": [],
            "permissions": [{"guard": "auth", "rule": "signed-in operators only"}],
            "states": [{"state": "loading", "expected": "a skeleton table"}],
        },  # fmt: skip
        "i18n": {"string_scope": "every label and message", "estimated_strings": 20},
        "wave": "u1",
        "parity_acceptance": [
            {"check": check, "criterion": f"{check} matches the legacy page", "fixtures": ["t"]}
            for check in CHECKS
        ],
        "gap_ids": list(gap_ids),
        "depends_on": ["u0-scaffold"],
    }


def batch_content(index: int, page_ids: list[str]) -> dict[str, Any]:
    own = f"{batch_gap_prefix(index)}export"
    return {
        "pages": [
            page_judgement(
                page_id,
                gap_ids=(("gap-plans-api", own) if position == 0 else ()),
            )
            for position, page_id in enumerate(page_ids)
        ],
        "gaps": [gap(own, "BACKEND_GAP", [page_ids[0]], "DEFERRED")],
    }


def part_output(kind: str, part_id: str, content: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "COMPLETE",
        "mode": "draft",
        "project": "falcone",
        "inputs": part_inputs(part_id),
        kind: content,
        "review": None,
        "blockers": [],
    }


def part_review(
    kind: str, part_id: str, draft_sha256: str, decision: str = "APPROVE"
) -> dict[str, Any]:
    findings = (
        []
        if decision == "APPROVE"
        else [
            {
                "category": "behaviour_gap",
                "subject": part_id,
                "detail": f"The 403 state is missing in {part_id}.",
                "reference": "pages[0].behaviour.states",
            }
        ]
    )
    return {
        "status": "COMPLETE",
        "mode": "review",
        "project": "falcone",
        "inputs": part_inputs(part_id, draft_sha256),
        kind: None,
        "review": {"decision": decision, "summary": f"{decision} {part_id}", "findings": findings},
        "blockers": [],
    }


def draft_for(part: dict[str, Any], part_id: str, routed: list[str]) -> dict[str, Any]:
    if part["kind"] == FRAME:
        return part_output(FRAME, part_id, frame_content(routed[0]))
    return part_output(BATCH, part_id, batch_content(int(part["index"]), list(part["page_ids"])))
