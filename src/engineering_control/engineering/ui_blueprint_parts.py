"""NEW-131: the batched UI migration blueprint (flow A).

One model call drafting the whole blueprint was too large (the first NEW-123 run: 52 min and
about 447k output tokens; the redraft hit the agent timeout), and a model must not copy
mechanical data (its review found 199 inventoried endpoints omitted on 9 pages). So:

- Hermes fills every *mechanical* field from the inventory (page identity, route, files,
  reachability, endpoints verbatim, LOC and tier, test assets, the i18n namespace and
  locales, orphan pages, dead modules, the cutover's fixed and derived fields, the inputs);
- Claude drafts only the *judgement* fields, in parts: one frame (foundation epics,
  cross-page gaps, the cutover steps and criteria, global decisions), then page batches;
- Codex reviews every part; Hermes assembles the reviewed parts and the mechanical fields
  into the full blueprint and validates it against the full schema.

Everything here is pure and deterministic.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter
from typing import Any

from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.ui_blueprint import (
    REPOSITORY_FALCONE,
    REPOSITORY_GNTIK_UI,
    UI_PLAN_PART_SCHEMAS,
    BlueprintRejected,
    PlanInputs,
    _check_inputs,
    blueprint_schema,
    load_schema,
    schema_errors,
    structure_problems,
)

FRAME = "frame"
BATCH = "batch"
PART_SCHEMAS = dict(UI_PLAN_PART_SCHEMAS["plan-ui-migration"])
PART_ID_PATTERN = r"^(frame|b[0-9]{3})-[a-f0-9]{16}$"
PART_ID_VERSION = "ui-plan-part-v1"
DEFAULT_BATCH_PAGES = 8
MAX_BATCH_PAGES = 25
MAX_BATCH_GAPS = 100
# Per provider call (Claude draft or Codex review of one part); the effort stays as before.
PART_TIMEOUT_SECONDS = 1800
LOCALES = ("es", "en")
KIT_SLICE_TOOLS = frozenset({"list_kit"})
MECHANICAL_PART = "mechanical"
_TIERS = {"small": "S", "medium": "M", "large": "L", "xl": "XL"}
_BATCH_GAP = re.compile(r"^gap-b[0-9]{3}-")
_CUTOVER = "u5-cutover"

# Every blueprint field, classified (pages[] and cutover per field, i18n per subfield).
# Orphan pages are mechanical in full (Hermes deletes them); cutover.blocking_gaps is
# assembled by Hermes from the decision each drafted gap carries.
MECHANICAL_FIELDS: tuple[str, ...] = (
    "status",
    "mode",
    "project",
    "inputs",
    "review",
    "blockers",
    "dead_modules",
    "pages[].page_id",
    "pages[].route",
    "pages[].legacy_files",
    "pages[].reachability",
    "pages[].endpoints",
    "pages[].i18n.namespace",
    "pages[].i18n.locales",
    "pages[].i18n.default_locale",
    "pages[].complexity",
    "pages[].legacy_tests",
    "cutover.id",
    "cutover.wave",
    "cutover.depends_on_pages",
    "cutover.requires_parity_pass",
    "cutover.requires_i18n_complete",
    "cutover.blocking_gaps",
)
JUDGEMENT_FIELDS: tuple[str, ...] = (
    "foundation_epics",
    "gaps",
    "pages[].disposition",
    "pages[].disposition_reason",
    "pages[].target",
    "pages[].behaviour",
    "pages[].i18n.string_scope",
    "pages[].i18n.estimated_strings",
    "pages[].wave",
    "pages[].parity_acceptance",
    "pages[].gap_ids",
    "pages[].depends_on",
    "cutover.steps",
    "cutover.acceptance_criteria",
)
PAGE_JUDGEMENT_KEYS = (
    "page_id",
    "disposition",
    "disposition_reason",
    "target",
    "behaviour",
    "i18n",
    "wave",
    "parity_acceptance",
    "gap_ids",
    "depends_on",
)
I18N_JUDGEMENT_KEYS = ("string_scope", "estimated_strings")


# --------------------------------------------------------------------------------------
# Part schemas, derived from the full blueprint schema
# --------------------------------------------------------------------------------------


def derive_part_schemas(full: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The frame and batch output schemas, built from the full schema's own definitions
    (the committed copies and the agent pack's must equal this; see the tests)."""
    defs = full["$defs"]
    page = defs["page"]["properties"]
    blocking = defs["cutover"]["properties"]["blocking_gaps"]["items"]["properties"]
    part_gap = copy.deepcopy(defs["gap"])
    part_gap["required"] = [*part_gap["required"], "cutover_resolution", "cutover_reason"]
    part_gap["properties"]["cutover_resolution"] = copy.deepcopy(blocking["resolution"])
    part_gap["properties"]["cutover_reason"] = copy.deepcopy(blocking["reason"])
    i18n = page["i18n"]["properties"]
    page_judgement = {
        "type": "object",
        "additionalProperties": False,
        "required": list(PAGE_JUDGEMENT_KEYS),
        "properties": {
            **{key: copy.deepcopy(page[key]) for key in PAGE_JUDGEMENT_KEYS if key != "i18n"},
            "i18n": {
                "type": "object",
                "additionalProperties": False,
                "required": list(I18N_JUDGEMENT_KEYS),
                "properties": {key: copy.deepcopy(i18n[key]) for key in I18N_JUDGEMENT_KEYS},
            },
        },
    }
    inputs = copy.deepcopy(full["properties"]["inputs"])
    inputs["required"] = [*inputs["required"], "part_id"]
    inputs["properties"]["part_id"] = {"type": "string", "pattern": PART_ID_PATTERN}
    gaps = {"type": "array", "items": {"$ref": "#/$defs/part_gap"}}
    cutover = defs["cutover"]["properties"]
    frame = {
        "type": "object",
        "additionalProperties": False,
        "required": ["foundation_epics", "gaps", "cutover", "global_decisions"],
        "properties": {
            "foundation_epics": copy.deepcopy(defs["blueprint"]["properties"]["foundation_epics"]),
            "gaps": {**gaps, "maxItems": 300},
            "cutover": {
                "type": "object",
                "additionalProperties": False,
                "required": ["steps", "acceptance_criteria"],
                "properties": {
                    "steps": copy.deepcopy(cutover["steps"]),
                    "acceptance_criteria": copy.deepcopy(cutover["acceptance_criteria"]),
                },
            },
            "global_decisions": {
                "type": "array",
                "maxItems": 30,
                "items": {"type": "string", "minLength": 1, "maxLength": 600},
            },
        },
    }
    batch = {
        "type": "object",
        "additionalProperties": False,
        "required": ["pages", "gaps"],
        "properties": {
            "pages": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_BATCH_PAGES,
                "items": {"$ref": "#/$defs/page_judgement"},
            },
            "gaps": {**gaps, "maxItems": MAX_BATCH_GAPS},
        },
    }

    def envelope(key: str, own: dict[str, Any]) -> dict[str, Any]:
        properties = full["properties"]
        return {
            "$schema": full["$schema"],
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "mode", "project", "inputs", key, "review", "blockers"],
            "properties": {
                "status": copy.deepcopy(properties["status"]),
                "mode": copy.deepcopy(properties["mode"]),
                "project": copy.deepcopy(properties["project"]),
                "inputs": copy.deepcopy(inputs),
                key: {"anyOf": [{"$ref": f"#/$defs/{key}"}, {"type": "null"}]},
                "review": copy.deepcopy(properties["review"]),
                "blockers": copy.deepcopy(properties["blockers"]),
            },
            "$defs": {**own, "part_gap": copy.deepcopy(part_gap), "review": defs["review"]},
        }

    return {
        FRAME: envelope(FRAME, {FRAME: frame, "epic": copy.deepcopy(defs["epic"])}),
        BATCH: envelope(BATCH, {BATCH: batch, "page_judgement": page_judgement}),
    }


def part_schema(kind: str) -> dict[str, Any]:
    return load_schema(PART_SCHEMAS[kind])


# --------------------------------------------------------------------------------------
# Mechanical fields
# --------------------------------------------------------------------------------------


def _kebab(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", value)
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def page_ids(inventory: dict[str, Any]) -> dict[str, str]:
    """inventory page file -> a stable, unique blueprint page id."""
    prefix = f"{inventory.get('legacy_app', '')}/src/pages/"
    assigned: dict[str, str] = {}
    taken: set[str] = set()
    for file in sorted(str(page["file"]) for page in inventory["pages"]):
        relative = file.removeprefix(prefix)
        base = (_kebab(re.sub(r"\.[A-Za-z]+$", "", relative))[:100].strip("-")) or "page"
        candidate, number = base, 2
        while candidate in taken:
            candidate = f"{base[:94].strip('-')}-{number}"
            number += 1
        taken.add(candidate)
        assigned[file] = candidate
    return assigned


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def mechanical_fields(inventory: dict[str, Any]) -> dict[str, Any]:
    """Every field Hermes fills from the inventory: pages (orphans in full) and dead modules.

    Endpoints are copied verbatim (method, path and cross-check class) for every page and
    cite their entry in the inventory's endpoint list."""
    identities = page_ids(inventory)
    references = {
        (str(item["method"]), str(item["path"])): index
        for index, item in enumerate(inventory.get("endpoints") or [])
    }
    pages: list[dict[str, Any]] = []
    for position, page in enumerate(inventory["pages"]):
        page_id = identities[str(page["file"])]
        routed = bool(page.get("routes"))
        endpoints = [
            {
                "method": str(use["method"]),
                "path": str(use["path"]),
                "xcheck_class": str(use["class"]),
                "inventory_ref": (
                    f"endpoints[{references[(str(use['method']), str(use['path']))]}]"
                    if (str(use["method"]), str(use["path"])) in references
                    else f"pages[{position}].endpoints[{index}]"
                ),
            }
            for index, use in enumerate(page.get("endpoints") or [])
        ]
        record: dict[str, Any] = {
            "page_id": page_id,
            "route": sorted(page["routes"])[0] if routed else None,
            "legacy_files": _unique(
                [
                    str(page["file"]),
                    *map(str, page.get("components") or []),
                    *map(str, page.get("libraries") or []),
                ]  # fmt: skip
            )[:100],
            "reachability": "ROUTED" if routed else "ORPHAN",
            "endpoints": endpoints,
            "i18n": {
                "namespace": f"pages.{page_id}"[:100],
                "locales": list(LOCALES),
                "default_locale": LOCALES[0],
            },
            "complexity": {
                "tier": _TIERS.get(str(page.get("tier")), "XL"),
                "page_loc": int(page.get("loc") or 0),
                "closure_loc": int(page.get("closure_loc") or 0),
            },
            "legacy_tests": {
                "vitest": list((page.get("tests") or {}).get("vitest") or []),
                "playwright_journeys": list((page.get("tests") or {}).get("playwright") or []),
            },
        }
        if not routed:
            record = _orphan_page(record)
        pages.append(record)
    pages.sort(key=lambda item: item["page_id"])
    dead = [
        {
            "path": str(item["file"]),
            "decision": "DELETE",
            "reason": (
                "Unreachable from the legacy entry point in the deterministic inventory "
                f"({int(item.get('loc') or 0)} LOC): deleted with the legacy app at u5, "
                "never migrated."
            ),
        }
        for item in inventory.get("dead_modules") or []
    ]
    return {"pages": pages, "dead_modules": dead}


def _orphan_page(record: dict[str, Any]) -> dict[str, Any]:
    """An orphan page in full: DELETE (operator decision: dead code is not migrated)."""
    return _ordered_page(
        record,
        {
            "disposition": "DELETE",
            "disposition_reason": (
                "Orphan page: no route of the legacy router reaches it (inventory "
                "orphan_pages); deleted, not migrated."
            ),
            "target": {
                "template": None,
                "layout": None,
                "blocks": [],
                "components": [],
                "wrapped": [],
                "justification": "Deleted orphan page: no gntik-ui target.",
            },
            "behaviour": {"actions": [], "validations": [], "permissions": [], "states": []},
            "i18n": {"string_scope": "None: the page is deleted.", "estimated_strings": 0},
            "wave": None,
            "parity_acceptance": [],
            "gap_ids": [],
            "depends_on": [],
        },
    )


def _ordered_page(mechanical: dict[str, Any], judgement: dict[str, Any]) -> dict[str, Any]:
    i18n = mechanical["i18n"]
    return {
        "page_id": mechanical["page_id"],
        "route": mechanical["route"],
        "legacy_files": mechanical["legacy_files"],
        "reachability": mechanical["reachability"],
        "disposition": judgement["disposition"],
        "disposition_reason": judgement["disposition_reason"],
        "target": judgement["target"],
        "behaviour": judgement["behaviour"],
        "endpoints": mechanical["endpoints"],
        "i18n": {
            "namespace": i18n["namespace"],
            "string_scope": judgement["i18n"]["string_scope"],
            "estimated_strings": judgement["i18n"]["estimated_strings"],
            "locales": i18n["locales"],
            "default_locale": i18n["default_locale"],
        },
        "complexity": mechanical["complexity"],
        "wave": judgement["wave"],
        "parity_acceptance": judgement["parity_acceptance"],
        "gap_ids": judgement["gap_ids"],
        "legacy_tests": mechanical["legacy_tests"],
        "depends_on": judgement["depends_on"],
    }


def routed_page_ids(mechanical: dict[str, Any]) -> list[str]:
    return [page["page_id"] for page in mechanical["pages"] if page["reachability"] == "ROUTED"]


def split_batches(identities: list[str], size: int) -> list[list[str]]:
    if not 1 <= size <= MAX_BATCH_PAGES:
        raise ValueError("ui plan batch size is out of range")
    return [identities[start : start + size] for start in range(0, len(identities), size)]


# --------------------------------------------------------------------------------------
# Part identities and the staged model inputs
# --------------------------------------------------------------------------------------


def _short(*parts: str) -> str:
    return hashlib.sha256("|".join((PART_ID_VERSION, *parts)).encode()).hexdigest()[:16]


def frame_part_id(
    inventory_sha256: str, kit_snapshot_sha256: str, routed: list[str], provenance: str
) -> str:
    """Deterministic: the inventory, the kit, the routed pages and the provenance (skill,
    role, profile and schemas, see ui_blueprint.plan_provenance)."""
    digest = _short(FRAME, inventory_sha256, kit_snapshot_sha256, ",".join(routed), provenance)
    return f"frame-{digest}"


def batch_part_id(
    index: int,
    inventory_sha256: str,
    kit_snapshot_sha256: str,
    identities: list[str],
    frame_sha256: str,
    provenance: str,
) -> str:
    """Deterministic: the inventory, the kit, the batch's pages, the reviewed frame the
    batch was drafted against and the provenance. A reviewed batch with this id is never
    drafted again."""
    if not 1 <= index <= 999:
        raise ValueError("ui plan batch index is out of range")
    digest = _short(
        BATCH,
        inventory_sha256,
        kit_snapshot_sha256,
        ",".join(identities),
        frame_sha256,
        provenance,
    )
    return f"b{index:03d}-{digest}"


def batch_gap_prefix(index: int) -> str:
    return f"gap-b{index:03d}-"


def kit_slice(kit: dict[str, Any]) -> dict[str, Any]:
    """The kit catalogue the page batches need (the list_kit outputs), not the whole kit."""
    return {
        "kind": "kit-catalogue",
        "gntik_ui_version": kit.get("gntik_ui_version"),
        "snapshot_sha256": kit.get("snapshot_sha256"),
        "calls": [call for call in kit.get("calls") or [] if call.get("tool") in KIT_SLICE_TOOLS],
    }


def stage_plan(inventory: dict[str, Any], kit: dict[str, Any], batch_pages: int) -> dict[str, Any]:
    """The mechanical fields, the part plan and the small input file each part reads."""
    mechanical = mechanical_fields(inventory)
    identities = page_ids(inventory)
    by_id = {identities[str(page["file"])]: page for page in inventory["pages"]}
    mechanical_by_id = {page["page_id"]: page for page in mechanical["pages"]}
    routed = routed_page_ids(mechanical)
    batches = split_batches(routed, batch_pages)
    batch_of = {page: index for index, batch in enumerate(batches, 1) for page in batch}
    references = {
        (str(item["method"]), str(item["path"])): index
        for index, item in enumerate(inventory.get("endpoints") or [])
    }
    users: dict[int, list[str]] = {}
    for page_id, page in sorted(by_id.items()):
        for use in page.get("endpoints") or []:
            key = (str(use["method"]), str(use["path"]))
            if key in references:
                users.setdefault(references[key], []).append(page_id)
    index_rows = [
        {
            "page_id": page_id,
            "route": mechanical_by_id[page_id]["route"],
            "reachability": mechanical_by_id[page_id]["reachability"],
            "tier": mechanical_by_id[page_id]["complexity"]["tier"],
            "batch": batch_of.get(page_id),
        }
        for page_id in sorted(by_id)
    ]
    common = {
        "project": inventory.get("project"),
        "source_sha": inventory.get("source_sha"),
        "inventory_sha256": inventory.get("inventory_sha256"),
        "kit_snapshot_sha256": kit.get("snapshot_sha256"),
        "legacy_app": inventory.get("legacy_app"),
        "target_app": inventory.get("target_app"),
        "hermes_fills": list(MECHANICAL_FIELDS),
    }
    frame_input = {
        "kind": FRAME,
        **common,
        "summary": inventory.get("summary"),
        "pages": [
            {
                **row,
                "file": str(by_id[row["page_id"]]["file"]),
                "guards": list(by_id[row["page_id"]].get("guards") or []),
                "page_loc": mechanical_by_id[row["page_id"]]["complexity"]["page_loc"],
                "closure_loc": mechanical_by_id[row["page_id"]]["complexity"]["closure_loc"],
                "endpoint_classes": dict(
                    sorted(
                        Counter(
                            str(use["class"])
                            for use in by_id[row["page_id"]].get("endpoints") or []
                        ).items()
                    )
                ),
            }
            for row in index_rows
        ],
        "routes": inventory.get("routes") or [],
        "endpoints": [
            {
                "ref": f"endpoints[{index}]",
                "method": item["method"],
                "path": item["path"],
                "class": item["class"],
                "pages": users.get(index, []),
            }
            for index, item in enumerate(inventory.get("endpoints") or [])
        ],
        "orphan_pages_deleted_by_hermes": [
            row["page_id"] for row in index_rows if row["reachability"] == "ORPHAN"
        ],
        "dead_modules_deleted_by_hermes": {
            "count": len(mechanical["dead_modules"]),
            "loc": sum(int(item.get("loc") or 0) for item in inventory.get("dead_modules") or []),
        },
        "batches": [{"index": index, "page_ids": batch} for index, batch in enumerate(batches, 1)],
    }
    files: dict[str, Any] = {
        "part-frame-input.json": frame_input,
        "kit-catalogue.json": kit_slice(kit),
    }
    parts: list[dict[str, Any]] = [
        {"kind": FRAME, "index": 0, "page_ids": [], "input": "part-frame-input.json"}
    ]
    for index, batch in enumerate(batches, 1):
        used = sorted(
            {
                references[(str(use["method"]), str(use["path"]))]
                for page_id in batch
                for use in by_id[page_id].get("endpoints") or []
                if (str(use["method"]), str(use["path"])) in references
            }
        )
        name = f"part-b{index:03d}-input.json"
        files[name] = {
            "kind": BATCH,
            **common,
            "index": index,
            "batch_count": len(batches),
            "gap_id_prefix": batch_gap_prefix(index),
            "pages": [
                {
                    "page_id": page_id,
                    "inventory": by_id[page_id],
                    "hermes_fills": mechanical_by_id[page_id],
                }
                for page_id in batch
            ],
            "endpoint_details": [
                {"ref": f"endpoints[{ref}]", **inventory["endpoints"][ref]} for ref in used
            ],
            "all_pages": index_rows,
        }
        parts.append({"kind": BATCH, "index": index, "page_ids": batch, "input": name})
    return {
        "mechanical": mechanical,
        "mechanical_sha256": canonical_sha256(mechanical),
        "files": files,
        "parts": parts,
        "kit_slice": "kit-catalogue.json",
    }


# --------------------------------------------------------------------------------------
# Part outputs
# --------------------------------------------------------------------------------------


def _check_part_inputs(
    value: dict[str, Any], inputs: PlanInputs, part_id: str, prefix: str
) -> None:
    _check_inputs(value, inputs, prefix)
    if (value.get("inputs") or {}).get("part_id") != part_id:
        raise BlueprintRejected(f"{prefix}_BINDING_MISMATCH", ["inputs.part_id"])


def check_part_draft(
    value: Any,
    kind: str,
    inputs: PlanInputs,
    part_id: str,
    *,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Validate one part draft against its schema, binding and references; return it.

    `context`: all_page_ids, and for a batch its index, page_ids and the reviewed frame."""
    errors = schema_errors(value, part_schema(kind))
    if errors:
        raise BlueprintRejected("DRAFT_SCHEMA_INVALID", [f"{part_id}: {item}" for item in errors])
    assert isinstance(value, dict)
    if value.get("mode") != "draft":
        raise BlueprintRejected("DRAFT_MODE_INVALID", [f"{part_id}: mode"])
    if value.get("status") != "COMPLETE" or not isinstance(value.get(kind), dict):
        raise BlueprintRejected(
            "DRAFT_BLOCKED", [f"{part_id}: {item}" for item in value.get("blockers") or []]
        )
    _check_part_inputs(value, inputs, part_id, "DRAFT")
    content: dict[str, Any] = value[kind]
    problems = _content_structure(kind, content, context)
    if problems:
        raise BlueprintRejected("DRAFT_STRUCTURE_INVALID", [f"{part_id}: {p}" for p in problems])
    return content


def _content_structure(kind: str, content: dict[str, Any], context: dict[str, Any]) -> list[str]:
    if kind == FRAME:
        return frame_problems(content, context["all_page_ids"])
    return batch_problems(
        content,
        index=int(context["index"]),
        page_ids=list(context["page_ids"]),
        all_page_ids=context["all_page_ids"],
        frame=context["frame"],
    )


def part_content_problems(kind: str, content: Any, context: dict[str, Any]) -> list[str]:
    """Revalidate stored part content (a reused approval) against the current part schema
    and the current references; empty when it is still valid."""
    schema = part_schema(kind)
    errors = schema_errors(content, {"$ref": f"#/$defs/{kind}", "$defs": schema["$defs"]})
    if errors or not isinstance(content, dict):
        return errors or ["part content is not an object"]
    return _content_structure(kind, content, context)


def check_part_review(
    value: Any, kind: str, inputs: PlanInputs, part_id: str, draft_sha256: str
) -> dict[str, Any]:
    errors = schema_errors(value, part_schema(kind))
    if errors:
        raise BlueprintRejected("REVIEW_SCHEMA_INVALID", [f"{part_id}: {item}" for item in errors])
    assert isinstance(value, dict)
    if value.get("mode") != "review":
        raise BlueprintRejected("REVIEW_MODE_INVALID", [f"{part_id}: mode"])
    if value.get("status") != "COMPLETE" or not isinstance(value.get("review"), dict):
        raise BlueprintRejected(
            "REVIEW_BLOCKED", [f"{part_id}: {item}" for item in value.get("blockers") or []]
        )
    _check_part_inputs(value, inputs, part_id, "REVIEW")
    if (value.get("inputs") or {}).get("draft_sha256") != draft_sha256:
        raise BlueprintRejected("REVIEW_BINDING_MISMATCH", [f"{part_id}: inputs.draft_sha256"])
    review: dict[str, Any] = value["review"]
    return review


def _duplicates(values: list[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def _gap_problems(gaps: list[dict[str, Any]], targets: set[str]) -> list[str]:
    problems = [f"duplicate gap id {gap_id}" for gap_id in _duplicates([g["id"] for g in gaps])]
    for gap in gaps:
        problems += [
            f"gap {gap['id']} blocks unknown {b}" for b in gap["blocks"] if b not in targets
        ]
        expected = REPOSITORY_FALCONE if gap["type"] == "BACKEND_GAP" else REPOSITORY_GNTIK_UI
        if gap["target_repository"] != expected:
            problems.append(f"gap {gap['id']} {gap['type']} must target {expected}")
    return problems


def frame_problems(frame: dict[str, Any], all_page_ids: list[str]) -> list[str]:
    epics = [epic["id"] for epic in frame["foundation_epics"]]
    gap_ids = {gap["id"] for gap in frame["gaps"]}
    problems = [f"duplicate epic id {value}" for value in _duplicates(epics)]
    for epic in frame["foundation_epics"]:
        problems += [
            f"epic {epic['id']} depends on unknown {d}"
            for d in epic["depends_on"]
            if d not in epics
        ]
        problems += [
            f"epic {epic['id']} names unknown gap {g}" for g in epic["gap_ids"] if g not in gap_ids
        ]
    problems += [
        f"frame gap {gap['id']} uses a batch gap prefix"
        for gap in frame["gaps"]
        if _BATCH_GAP.match(gap["id"])
    ]
    problems += _gap_problems(frame["gaps"], {*epics, *all_page_ids, _CUTOVER})
    return problems[:50]


def batch_problems(
    batch: dict[str, Any],
    *,
    index: int,
    page_ids: list[str],
    all_page_ids: list[str],
    frame: dict[str, Any],
) -> list[str]:
    drafted = [page["page_id"] for page in batch["pages"]]
    problems = [f"duplicate page {value}" for value in _duplicates(drafted)]
    problems += [f"page {p} is not in this batch" for p in drafted if p not in page_ids]
    problems += [f"page {p} is missing" for p in page_ids if p not in drafted]
    epics = {epic["id"] for epic in frame["foundation_epics"]}
    frame_gaps = {gap["id"] for gap in frame["gaps"]}
    prefix = batch_gap_prefix(index)
    own = [gap["id"] for gap in batch["gaps"]]
    problems += [f"new gap {g} must start with {prefix}" for g in own if not g.startswith(prefix)]
    problems += _gap_problems(batch["gaps"], {*epics, *all_page_ids, _CUTOVER})
    known_gaps = frame_gaps | set(own)
    targets = epics | set(all_page_ids)
    for page in batch["pages"]:
        name = page["page_id"]
        problems += [
            f"page {name} names unknown gap {g}" for g in page["gap_ids"] if g not in known_gaps
        ]
        problems += [
            f"page {name} depends on unknown {d}"
            for d in page["depends_on"]
            if d not in targets or d == name
        ]
        if page["disposition"] == "DELETE":
            if page["wave"] is not None or page["parity_acceptance"]:
                problems.append(f"page {name} is deleted with a wave or parity criteria")
        elif page["wave"] is None:
            problems.append(f"page {name} is migrated without a wave")
    return problems[:50]


def part_content_sha256(content: dict[str, Any]) -> str:
    return canonical_sha256(content)


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------


def _strip_gap(gap: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in gap.items() if not key.startswith("cutover_")}


def assemble(
    mechanical: dict[str, Any], frame: dict[str, Any], batches: list[dict[str, Any]]
) -> dict[str, Any]:
    """The full blueprint: Hermes's mechanical fields plus the reviewed judgement parts."""
    judgement = {page["page_id"]: page for batch in batches for page in batch["pages"]}
    pages: list[dict[str, Any]] = []
    for page in mechanical["pages"]:
        if page["reachability"] == "ORPHAN":
            pages.append(copy.deepcopy(page))
            continue
        drafted = judgement.get(page["page_id"])
        if drafted is None:
            raise BlueprintRejected("ASSEMBLY_INVALID", [f"page {page['page_id']} was not drafted"])
        pages.append(copy.deepcopy(_ordered_page(page, drafted)))
    gaps = [*frame["gaps"], *(gap for batch in batches for gap in batch["gaps"])]
    return {
        "foundation_epics": copy.deepcopy(frame["foundation_epics"]),
        "pages": pages,
        "gaps": [_strip_gap(copy.deepcopy(gap)) for gap in gaps],
        "dead_modules": copy.deepcopy(mechanical["dead_modules"]),
        "cutover": {
            "id": _CUTOVER,
            "wave": "u5",
            "steps": copy.deepcopy(frame["cutover"]["steps"]),
            "depends_on_pages": [p["page_id"] for p in pages if p["disposition"] != "DELETE"],
            "requires_parity_pass": True,
            "requires_i18n_complete": True,
            "blocking_gaps": [
                {
                    "gap_id": gap["id"],
                    "resolution": gap["cutover_resolution"],
                    "reason": gap["cutover_reason"],
                }
                for gap in gaps
            ],
            "acceptance_criteria": copy.deepcopy(frame["cutover"]["acceptance_criteria"]),
        },
    }


def assembled_output(blueprint: dict[str, Any], inputs: PlanInputs) -> dict[str, Any]:
    """The assembled blueprint as a draft-mode output of the full schema (inputs by Hermes)."""
    return {
        "status": "COMPLETE",
        "mode": "draft",
        "project": inputs.project,
        "inputs": {
            "inventory_sha256": inputs.inventory_sha256,
            "legacy_source_sha": inputs.source_sha,
            "gntik_ui_version": inputs.gntik_ui_version,
            "kit_source": "snapshot",
            "kit_snapshot_sha256": inputs.kit_snapshot_sha256,
            "draft_sha256": None,
        },
        "blueprint": blueprint,
        "review": None,
        "blockers": [],
    }


def check_assembled(
    blueprint: dict[str, Any], inputs: PlanInputs, mechanical: dict[str, Any]
) -> None:
    errors = schema_errors(assembled_output(blueprint, inputs), blueprint_schema())
    if errors:
        raise BlueprintRejected("ASSEMBLY_INVALID", errors)
    problems = structure_problems(blueprint)
    expected = [page["page_id"] for page in mechanical["pages"]]
    if [page["page_id"] for page in blueprint["pages"]] != expected:
        problems.append("assembled pages differ from the inventoried pages")
    if problems:
        raise BlueprintRejected("ASSEMBLY_INVALID", problems)
    graph = graph_problems(blueprint)
    if graph:
        raise BlueprintRejected("BLUEPRINT_GRAPH_INVALID", graph)


_WAVES = ("u0", "u1", "u2", "u3", "u4", "u5")


def graph_problems(blueprint: dict[str, Any]) -> list[str]:
    """Cross-batch dependency graph of the assembled blueprint (no batch reviewer sees it):
    no cycles across epics and pages, a page depends only on items of its own or an earlier
    wave (epics are u0) and never on a deleted page, every gap a page or the cutover names
    exists, and the cutover's pages and gap decisions match the blueprint exactly."""
    problems: list[str] = []
    epics = {epic["id"]: epic for epic in blueprint["foundation_epics"]}
    pages = {page["page_id"]: page for page in blueprint["pages"]}
    gaps = {gap["id"] for gap in blueprint["gaps"]}
    wave = {identity: "u0" for identity in epics}
    wave |= {p: page["wave"] for p, page in pages.items() if page["disposition"] != "DELETE"}
    edges: dict[str, list[str]] = {}
    for identity, epic in epics.items():
        edges[identity] = list(epic["depends_on"])
    for identity, page in pages.items():
        if page["disposition"] == "DELETE":
            continue
        edges[identity] = list(page["depends_on"])
        for target in page["depends_on"]:
            edge = f"{identity} -> {target}"
            if target in pages and pages[target]["disposition"] == "DELETE":
                problems.append(f"{edge}: depends on a deleted page")
            elif target not in wave:
                problems.append(f"{edge}: depends on an unknown item")
            elif _WAVES.index(str(wave[target])) > _WAVES.index(str(wave[identity])):
                problems.append(
                    f"{edge}: wave {wave[identity]} depends on later wave {wave[target]}"
                )
        problems += [
            f"{identity} -> {gap}: names an unknown gap"
            for gap in page["gap_ids"]
            if gap not in gaps
        ]
    problems += _cycles(edges)
    cutover = blueprint["cutover"]
    migrated = [p for p, page in pages.items() if page["disposition"] != "DELETE"]
    if sorted(cutover["depends_on_pages"]) != sorted(migrated):
        problems.append("u5-cutover: depends_on_pages differs from the MIGRATE and WRAP pages")
    decided = [item["gap_id"] for item in cutover["blocking_gaps"]]
    problems += [f"u5-cutover -> {g}: decided twice" for g in _duplicates(decided)]
    problems += [f"u5-cutover -> {g}: names an unknown gap" for g in decided if g not in gaps]
    problems += [f"u5-cutover -> {g}: has no cutover decision" for g in sorted(gaps - set(decided))]
    return problems[:50]


def _cycles(edges: dict[str, list[str]]) -> list[str]:
    """Each dependency cycle once, as its edges (deterministic, iterative DFS)."""
    found: list[str] = []
    state: dict[str, int] = {}
    seen: set[frozenset[str]] = set()
    for root in sorted(edges):
        if state.get(root):
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = []
        while stack:
            node, position = stack.pop()
            if position == 0:
                state[node] = 1
                path.append(node)
            targets = [t for t in edges.get(node, []) if t in edges]
            if position < len(targets):
                stack.append((node, position + 1))
                target = targets[position]
                if state.get(target) == 1:
                    cycle = path[path.index(target) :] + [target]
                    key = frozenset(cycle)
                    if key not in seen:
                        seen.add(key)
                        found.append("cycle: " + " -> ".join(cycle))
                elif not state.get(target):
                    stack.append((target, 0))
            else:
                state[node] = 2
                path.pop()
    return found


# --------------------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------------------

_USAGE_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens")


def usage_entry(
    agent_result: Any, *, part_id: str, phase: str, attempt: int
) -> dict[str, Any] | None:
    """One provider call's token usage, from its agent result (None without a result)."""
    if not isinstance(agent_result, dict):
        return None
    raw = agent_result.get("usage")
    usage: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {
        "part_id": part_id,
        "phase": phase,
        "attempt": attempt,
        "provider": str(agent_result.get("provider") or ""),
        "run_id": str(agent_result.get("run_id") or ""),
        **{name: max(0, int(usage.get(name) or 0)) for name in _USAGE_FIELDS},
        "source": str(usage.get("source") or "unknown")[:40],
    }


def usage_summary(entries: Any) -> dict[str, Any]:
    """Totals per provider and overall (tokens as reported by each provider's CLI)."""
    rows = [entry for entry in entries or [] if isinstance(entry, dict)]
    providers: dict[str, dict[str, int]] = {}
    for entry in rows:
        bucket = providers.setdefault(
            str(entry.get("provider") or "unknown"), {"calls": 0, **dict.fromkeys(_USAGE_FIELDS, 0)}
        )
        bucket["calls"] += 1
        for name in _USAGE_FIELDS:
            bucket[name] += int(entry.get(name) or 0)
    total = {
        "calls": len(rows),
        **{n: sum(b[n] for b in providers.values()) for n in _USAGE_FIELDS},
    }
    return {"total": total, "providers": dict(sorted(providers.items()))}


def render_usage(summary: dict[str, Any]) -> str:
    """The operator's usage footer for ui-blueprint-show."""
    lines = ["", "## Model usage", ""]
    rows = [*summary.get("providers", {}).items(), ("total", summary.get("total", {}))]
    for name, bucket in rows:
        lines.append(
            f"- {name}: {bucket.get('calls', 0)} calls, {bucket.get('input_tokens', 0)} input "
            f"tokens ({bucket.get('cached_input_tokens', 0)} cached), "
            f"{bucket.get('output_tokens', 0)} output tokens"
        )
    return "\n".join(lines) + "\n"


def review_digest(rounds: Any) -> dict[str, Any] | None:
    """The review an operator sees for a blueprint: its last round, or for a batched
    blueprint (rounds keyed by part) one summary of every part's independent review."""
    items = [item for item in rounds or [] if isinstance(item, dict)]
    if not items:
        return None
    if not any("part_id" in item for item in items):
        return items[-1]
    final: dict[str, dict[str, Any]] = {}
    for item in items:
        final[str(item.get("part_id"))] = item
    revised = sorted(
        {str(item.get("part_id")) for item in items if item.get("decision") == "REVISE"}
    )
    reused = sum(1 for item in final.values() if item.get("reused_from"))
    approved = all(item.get("decision") == "APPROVE" for item in final.values())
    return {
        "decision": "APPROVE" if approved else "REVISE",
        "summary": (
            f"{len(final)} parts (the frame and {len(final) - 1} page batches) independently "
            f"reviewed by the opposite provider; {len(revised)} redrafted once after REVISE; "
            f"{reused} reused from an earlier blueprint with identical inputs; "
            + ("every part APPROVED." if approved else "not every part is approved.")
        ),
        "findings": [],
    }
