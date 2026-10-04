"""NEW-123: the UI migration blueprint (flow A): contract, binding, rendering and fan-out.

Everything here is pure and deterministic. The workflow activities, the API and the CLI
recompute the same blueprint hash, approval binding, Markdown and fan-out items from the
same stored content, so an approval binds exactly what the operator saw.

Models never get an MCP connection (design section 11): the kit catalogue reaches the
drafter and the reviewer only as Hermes-collected data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.change_requests import (
    UiChangeContext,
    UiChangeDependency,
    UiContinuedSection,
)

UI_PLAN_PROJECTS = frozenset({"falcone"})
APPROVAL_PHRASE = "APPROVE_UI_BLUEPRINT"
AGENT_PACK_MIN_VERSION = (1, 4, 0)
AGENT_PACK_SKILL_MISSING = "AGENT_PACK_SKILL_MISSING"
# skill id -> (role, the skill's output schema in the agent pack and in this package)
UI_PLAN_SKILLS: dict[str, tuple[str, str]] = {
    "plan-ui-migration": ("ui-migration-architect", "ui-migration-blueprint.schema.json"),
    "triage-ui-gap": ("ui-gap-triager", "ui-gap-draft.schema.json"),
}
FANOUT_DEFAULT_MAX_ITEMS = 80
FANOUT_MAX_ITEMS_LIMIT = 600
FANOUT_BATCH = 10
WAVE_ORDER = ("u0", "u1", "u2", "u3", "u4", "u5")
REPOSITORY_FALCONE = "gntik-ai/falcone"
REPOSITORY_FALCONE_CHARTS = "gntik-ai/falcone-charts"
REPOSITORY_GNTIK_UI = "gntik-ai/gntik-ui"
KIND_LABELS = {
    "epic": "ui-epic",
    "page": "ui-page",
    "cutover": "ui-cutover",
    "backend-gap": "backend-gap",
    "kit-gap": "kit-gap",
}
_SHA = re.compile(r"^[a-f0-9]{40}$")
_BINDING = re.compile(r"^[a-f0-9]{64}$")
_MAX_PACK_FILE = 262_144


class BlueprintRejected(ValueError):  # noqa: N818 - a typed verdict, not a programming error
    """A provider output that cannot become (part of) a blueprint candidate."""

    def __init__(self, code: str, details: list[str] | tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.details = [str(item)[:500] for item in details][:20]


# --------------------------------------------------------------------------------------
# Contract: schemas and the released agent pack
# --------------------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _schema_text(name: str) -> str:
    return files("engineering_control.engineering").joinpath("schemas", name).read_text("utf-8")


def load_schema(name: str) -> dict[str, Any]:
    """A fresh copy of a synced agent-pack output schema (see tests for the sync check)."""
    value = json.loads(_schema_text(name))
    if not isinstance(value, dict):
        raise ValueError("embedded schema is not an object")
    return value


def blueprint_schema() -> dict[str, Any]:
    return load_schema(UI_PLAN_SKILLS["plan-ui-migration"][1])


def gap_draft_schema() -> dict[str, Any]:
    return load_schema(UI_PLAN_SKILLS["triage-ui-gap"][1])


def _pack_bytes(root: Path, relative: str) -> bytes | None:
    try:
        base = root.resolve(strict=True)
        target = (base / relative).resolve(strict=True)
    except OSError:
        return None
    if not target.is_relative_to(base) or not target.is_file():
        return None
    if target.stat().st_size > _MAX_PACK_FILE:
        return None
    return target.read_bytes()


def _version(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(\.[0-9]+){1,3}", value):
        return None
    return tuple(int(part) for part in value.split("."))


def agent_pack_problems(root: Path, skills: tuple[str, ...] | None = None) -> list[str]:
    """What the released agent pack lacks for flow A (empty when it is usable).

    The workers load roles, skills and schemas from the released pack (not the local
    repository), so a pack older than profile 1.4.0 or without the UI skills BLOCKs the
    flow before any model step instead of failing inside one."""
    problems: list[str] = []
    profile = _pack_bytes(root, "profiles/engineering.yaml")
    try:
        parsed = yaml.safe_load(profile) if profile is not None else None
    except yaml.YAMLError:
        parsed = None
    version = _version(parsed.get("version")) if isinstance(parsed, dict) else None
    if version is None:
        problems.append("profiles/engineering.yaml has no readable version")
    elif version < AGENT_PACK_MIN_VERSION:
        wanted = ".".join(str(part) for part in AGENT_PACK_MIN_VERSION)
        found = ".".join(str(part) for part in version)
        problems.append(f"profile version {found} is older than {wanted}")
    catalog_bytes = _pack_bytes(root, "skills/catalog.yaml")
    try:
        catalog = yaml.safe_load(catalog_bytes) if catalog_bytes is not None else None
    except yaml.YAMLError:
        catalog = None
    records = catalog.get("skills") if isinstance(catalog, dict) else None
    bound = {
        str(record.get("id")): str(record.get("role"))
        for record in (records if isinstance(records, list) else [])
        if isinstance(record, dict)
    }
    for skill in skills or tuple(UI_PLAN_SKILLS):
        role, schema_name = UI_PLAN_SKILLS[skill]
        for relative in (
            f"skills/{skill}/SKILL.md",
            f"skills/{skill}/references/contract.md",
            f"roles/{role}.yaml",
            f"generated/claude/agents/{role}.md",
            f"generated/codex/agents/{role}.toml",
        ):
            if _pack_bytes(root, relative) is None:
                problems.append(f"missing {relative}")
        if bound.get(skill) != role:
            problems.append(f"skills/catalog.yaml does not bind {skill} to {role}")
        released = _pack_bytes(root, f"skills/{skill}/references/output.schema.json")
        if released is None:
            problems.append(f"missing skills/{skill}/references/output.schema.json")
            continue
        try:
            same = json.loads(released) == load_schema(schema_name)
        except json.JSONDecodeError:
            same = False
        if not same:
            problems.append(f"skills/{skill} output schema differs from Hermes's {schema_name}")
    return problems


def schema_errors(value: Any, schema: dict[str, Any]) -> list[str]:
    """Bounded, specific messages (an anyOf failure reports its deepest branch errors)."""
    validator = Draft202012Validator(schema)
    found: list[Any] = []
    for error in sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path)):
        nested = sorted(error.context or (), key=lambda item: -len(item.absolute_path))
        found.extend([item for item in nested if item.absolute_path][:3] or [error])
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"[:300]
        for error in found[:20]
    ]


# --------------------------------------------------------------------------------------
# Draft and review outputs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanInputs:
    """The immutable inputs one blueprint is bound to."""

    project: str
    source_sha: str
    inventory_sha256: str
    kit_snapshot_sha256: str
    gntik_ui_version: str | None


def _check_inputs(value: dict[str, Any], inputs: PlanInputs, prefix: str) -> None:
    if value.get("project") != inputs.project:
        raise BlueprintRejected(f"{prefix}_BINDING_MISMATCH", ["project"])
    bound = value.get("inputs") or {}
    problems: list[str] = []
    if bound.get("inventory_sha256") != inputs.inventory_sha256:
        problems.append("inputs.inventory_sha256")
    legacy = bound.get("legacy_source_sha")
    if not isinstance(legacy, str) or not inputs.source_sha.startswith(legacy):
        problems.append("inputs.legacy_source_sha")
    if bound.get("kit_source") != "snapshot":
        problems.append("inputs.kit_source")
    if bound.get("kit_snapshot_sha256") != inputs.kit_snapshot_sha256:
        problems.append("inputs.kit_snapshot_sha256")
    if inputs.gntik_ui_version and bound.get("gntik_ui_version") != inputs.gntik_ui_version:
        problems.append("inputs.gntik_ui_version")
    if problems:
        raise BlueprintRejected(f"{prefix}_BINDING_MISMATCH", problems)


def check_draft(value: Any, inputs: PlanInputs) -> dict[str, Any]:
    """Validate one draft-mode output and return its blueprint (or raise a typed verdict)."""
    errors = schema_errors(value, blueprint_schema())
    if errors:
        raise BlueprintRejected("DRAFT_SCHEMA_INVALID", errors)
    assert isinstance(value, dict)
    if value.get("mode") != "draft":
        raise BlueprintRejected("DRAFT_MODE_INVALID", ["mode"])
    if value.get("status") != "COMPLETE" or not isinstance(value.get("blueprint"), dict):
        raise BlueprintRejected("DRAFT_BLOCKED", list(value.get("blockers") or []))
    _check_inputs(value, inputs, "DRAFT")
    blueprint: dict[str, Any] = value["blueprint"]
    problems = structure_problems(blueprint)
    if problems:
        raise BlueprintRejected("DRAFT_STRUCTURE_INVALID", problems)
    return blueprint


def check_review(value: Any, inputs: PlanInputs, draft_sha256: str) -> dict[str, Any]:
    """Validate one review-mode output bound to this exact draft; return its review."""
    errors = schema_errors(value, blueprint_schema())
    if errors:
        raise BlueprintRejected("REVIEW_SCHEMA_INVALID", errors)
    assert isinstance(value, dict)
    if value.get("mode") != "review":
        raise BlueprintRejected("REVIEW_MODE_INVALID", ["mode"])
    if value.get("status") != "COMPLETE" or not isinstance(value.get("review"), dict):
        raise BlueprintRejected("REVIEW_BLOCKED", list(value.get("blockers") or []))
    _check_inputs(value, inputs, "REVIEW")
    if (value.get("inputs") or {}).get("draft_sha256") != draft_sha256:
        raise BlueprintRejected("REVIEW_BINDING_MISMATCH", ["inputs.draft_sha256"])
    review: dict[str, Any] = value["review"]
    return review


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return sorted(repeated)


def structure_problems(blueprint: dict[str, Any]) -> list[str]:
    """Identity and reference integrity the deterministic fan-out relies on."""
    epics = [str(item["id"]) for item in blueprint["foundation_epics"]]
    pages = [str(item["page_id"]) for item in blueprint["pages"]]
    gaps = [str(item["id"]) for item in blueprint["gaps"]]
    problems = [
        f"duplicate {label} id {value}"
        for label, values in (("epic", epics), ("page", pages), ("gap", gaps))
        for value in _duplicates(values)
    ]
    epic_ids, page_ids, gap_ids = set(epics), set(pages), set(gaps)
    items = epic_ids | page_ids | {"u5-cutover"}
    for epic in blueprint["foundation_epics"]:
        problems += [
            f"epic {epic['id']} depends on unknown {d}"
            for d in epic["depends_on"]
            if d not in epic_ids
        ]
        problems += [
            f"epic {epic['id']} names unknown gap {g}" for g in epic["gap_ids"] if g not in gap_ids
        ]
    for page in blueprint["pages"]:
        problems += [
            f"page {page['page_id']} depends on unknown {d}"
            for d in page["depends_on"]
            if d not in epic_ids | page_ids
        ]
        problems += [
            f"page {page['page_id']} names unknown gap {g}"
            for g in page["gap_ids"]
            if g not in gap_ids
        ]
        if page["disposition"] != "DELETE" and page["wave"] is None:
            problems.append(f"page {page['page_id']} is migrated without a wave")
    for gap in blueprint["gaps"]:
        problems += [f"gap {gap['id']} blocks unknown {b}" for b in gap["blocks"] if b not in items]
        expected = REPOSITORY_FALCONE if gap["type"] == "BACKEND_GAP" else REPOSITORY_GNTIK_UI
        if gap["target_repository"] != expected:
            problems.append(f"gap {gap['id']} {gap['type']} must target {expected}")
    cutover = blueprint["cutover"]
    problems += [
        f"cutover depends on unknown page {p}"
        for p in cutover["depends_on_pages"]
        if p not in page_ids
    ]
    problems += [
        f"cutover names unknown gap {g['gap_id']}"
        for g in cutover["blocking_gaps"]
        if g["gap_id"] not in gap_ids
    ]
    return problems[:50]


def blueprint_sha256(blueprint: dict[str, Any]) -> str:
    return canonical_sha256(blueprint)


def approval_binding(
    *,
    blueprint_id: str,
    project: str,
    blueprint_sha256: str,
    reviewed_sha256: str,
    inventory_sha256: str,
    kit_snapshot_sha256: str,
    source_sha: str,
) -> str:
    """sha256 of the canonical blueprint hash, the hash of the draft the independent
    reviewer read, and every immutable input the blueprint was drafted from."""
    if not _SHA.fullmatch(source_sha):
        raise ValueError("source SHA is invalid")
    if reviewed_sha256 != blueprint_sha256:
        raise ValueError("only the reviewed blueprint can be bound for approval")
    for value in (blueprint_sha256, reviewed_sha256, inventory_sha256, kit_snapshot_sha256):
        if not _BINDING.fullmatch(value):
            raise ValueError("blueprint input hash is invalid")
    return canonical_sha256(
        {
            "kind": "hermes-ui-blueprint-v1",
            "blueprint_id": blueprint_id,
            "project": project,
            "blueprint_sha256": blueprint_sha256,
            "reviewed_sha256": reviewed_sha256,
            "inventory_sha256": inventory_sha256,
            "kit_snapshot_sha256": kit_snapshot_sha256,
            "source_sha": source_sha,
        }
    )


def ui_plan_workflow_id(project: str, blueprint_id: str) -> str:
    return f"ui-plan-{project}-{blueprint_id}"


def ui_fanout_workflow_id(blueprint_id: str, sequence: int) -> str:
    """One workflow id per fan-out execution: a Run binds to exactly one first execution,
    so a resumed fan-out (an approval replay) is a new execution under the next sequence."""
    if not 1 <= sequence <= 999_999:
        raise ValueError("ui fan-out sequence is out of range")
    return f"ui-blueprint-fanout-{blueprint_id}-{sequence}"


# --------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------

_INERT = str.maketrans({"<": "&lt;", ">": "&gt;", "`": "'", "|": "\\|", "[": "\\[", "]": "\\]"})


def _text(value: Any, limit: int = 300) -> str:
    flattened = " ".join(str(value).split()).translate(_INERT)
    return flattened if len(flattened) <= limit else flattened[: limit - 3] + "..."


def render_markdown(
    blueprint: dict[str, Any],
    *,
    blueprint_id: str,
    project: str,
    source_sha: str,
    inventory_sha256: str,
    kit_snapshot_sha256: str,
    gntik_ui_version: str | None,
    blueprint_sha256: str,
    review: dict[str, Any] | None,
) -> str:
    """The operator's summary of one blueprint (deterministic; model text is inert)."""
    pages = blueprint["pages"]
    waves = {wave: [page for page in pages if page["wave"] == wave] for wave in WAVE_ORDER}
    gaps = blueprint["gaps"]
    lines = [
        f"# UI migration blueprint: {project}",
        "",
        f"- Blueprint: `{blueprint_id}`",
        f"- Blueprint sha256: `{blueprint_sha256}`",
        f"- Legacy source: `{source_sha}`",
        f"- Inventory sha256: `{inventory_sha256}`",
        f"- Kit snapshot sha256: `{kit_snapshot_sha256}` (gntik-ui "
        f"{_text(gntik_ui_version or 'unknown', 100)})",
        f"- Pages: {len(pages)} ({sum(1 for p in pages if p['disposition'] == 'MIGRATE')} "
        f"migrate, {sum(1 for p in pages if p['disposition'] == 'WRAP')} wrap, "
        f"{sum(1 for p in pages if p['disposition'] == 'DELETE')} delete)",
        f"- Gaps: {sum(1 for g in gaps if g['type'] == 'BACKEND_GAP')} backend, "
        f"{sum(1 for g in gaps if g['type'] == 'KIT_GAP')} kit",
        f"- Dead modules: {len(blueprint['dead_modules'])}",
    ]
    if review is not None:
        lines += [
            f"- Independent review: {_text(review.get('decision', ''), 20)}: "
            f"{_text(review.get('summary', ''), 400)}",
        ]
    lines += [
        "",
        "## u0 foundation epics",
        "",
        "| Epic | Kind | Title | Depends on |",
        "|---|---|---|---|",
    ]
    for epic in blueprint["foundation_epics"]:
        lines.append(
            f"| `{epic['id']}` | {epic['kind']} | {_text(epic['title'], 100)} | "
            f"{', '.join(epic['depends_on']) or '-'} |"
        )
    for wave in ("u1", "u2", "u3", "u4"):
        lines += ["", f"## Wave {wave} ({len(waves[wave])} pages)", ""]
        if not waves[wave]:
            lines.append("- None")
            continue
        lines += [
            "| Page | Route | Disposition | Tier | Template | Gaps |",
            "|---|---|---|---|---|---|",
        ]
        for page in waves[wave]:
            lines.append(
                f"| `{page['page_id']}` | {_text(page['route'] or '-', 120)} | "
                f"{page['disposition']} | {page['complexity']['tier']} | "
                f"{page['target']['template'] or '-'} | {', '.join(page['gap_ids']) or '-'} |"
            )
    deleted = [page for page in pages if page["disposition"] == "DELETE"]
    lines += ["", f"## Deleted pages ({len(deleted)})", ""]
    lines += [f"- `{page['page_id']}`: {_text(page['disposition_reason'])}" for page in deleted]
    lines += [] if deleted else ["- None"]
    lines += ["", f"## Gaps ({len(gaps)})", ""]
    lines += [
        f"- `{gap['id']}` {gap['type']} in `{gap['target_repository']}`: "
        f"{_text(gap['summary'])} (blocks {', '.join(gap['blocks']) or 'nothing'})"
        for gap in gaps
    ]
    lines += [] if gaps else ["- None"]
    cutover = blueprint["cutover"]
    lines += ["", "## u5 cutover", ""]
    lines += [
        f"{index}. {step['action']}: {_text(step['detail'])}"
        for index, step in enumerate(cutover["steps"], start=1)
    ]
    lines += [
        "",
        f"Depends on {len(cutover['depends_on_pages'])} pages reaching PARITY_PASS and on "
        f"{len(cutover['blocking_gaps'])} gap decisions.",
        "",
        "## Approval",
        "",
        "Approving binds this exact blueprint and its inputs; Hermes then drafts one change "
        "request per epic, page, cutover and gap. Hermes never publishes them.",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Fan-out
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FanoutItem:
    key: str
    kind: str
    wave: str | None
    target_repository: str
    blocked_by: tuple[str, ...]
    spec: dict[str, Any] = field(repr=False)

    @property
    def labels(self) -> tuple[str, ...]:
        wave = (f"wave:{self.wave}",) if self.wave else ()
        return ("ui-migration", *wave, KIND_LABELS[self.kind])

    @property
    def is_gap(self) -> bool:
        return self.kind in {"backend-gap", "kit-gap"}


def _earliest(waves: list[str]) -> str | None:
    ordered = [wave for wave in WAVE_ORDER if wave in waves]
    return ordered[0] if ordered else None


def fanout_items(blueprint: dict[str, Any]) -> list[FanoutItem]:
    """Every change request an approved blueprint fans out into, in a stable order:
    foundation epics, gaps, pages (u1..u4) and the cutover.

    Dependencies: a page is blocked by its backend gaps (named either way) and by the
    epics and pages it depends on; an epic by its epics and backend gaps; the cutover by
    its pages and the gaps it requires RESOLVED."""
    epics = blueprint["foundation_epics"]
    pages = [page for page in blueprint["pages"] if page["disposition"] != "DELETE"]
    gaps = blueprint["gaps"]
    epic_ids = {epic["id"] for epic in epics}
    page_ids = {page["page_id"] for page in pages}
    gap_by_id = {gap["id"]: gap for gap in gaps}

    def page_gaps(page: dict[str, Any]) -> list[str]:
        named = list(page["gap_ids"])
        named += [gap["id"] for gap in gaps if page["page_id"] in gap["blocks"]]
        return list(dict.fromkeys(named))

    def backend(gap_ids: list[str]) -> list[str]:
        return [
            f"gap:{gap_id}"
            for gap_id in gap_ids
            if gap_by_id.get(gap_id, {}).get("type") == "BACKEND_GAP"
        ]

    def item_ref(identity: str) -> str | None:
        if identity in epic_ids:
            return f"epic:{identity}"
        if identity in page_ids:
            return f"page:{identity}"
        return None

    items: list[FanoutItem] = []
    for epic in epics:
        blocked = [f"epic:{d}" for d in epic["depends_on"] if d in epic_ids and d != epic["id"]]
        items.append(
            FanoutItem(
                key=f"epic:{epic['id']}",
                kind="epic",
                wave="u0",
                target_repository=REPOSITORY_FALCONE,
                blocked_by=tuple(dict.fromkeys(blocked + backend(list(epic["gap_ids"])))),
                spec=epic,
            )
        )
    for gap in gaps:
        waves = [page["wave"] for page in pages if gap["id"] in page_gaps(page) and page["wave"]]
        waves += [
            "u0" for epic in epics if gap["id"] in epic["gap_ids"] or epic["id"] in gap["blocks"]
        ]
        items.append(
            FanoutItem(
                key=f"gap:{gap['id']}",
                kind="backend-gap" if gap["type"] == "BACKEND_GAP" else "kit-gap",
                wave=_earliest(waves),
                target_repository=gap["target_repository"],
                blocked_by=(),
                spec=gap,
            )
        )
    ordered = sorted(pages, key=lambda page: WAVE_ORDER.index(page["wave"]) if page["wave"] else 9)
    for page in ordered:
        refs = [item_ref(d) for d in page["depends_on"] if d != page["page_id"]]
        blocked = backend(page_gaps(page)) + [ref for ref in refs if ref is not None]
        items.append(
            FanoutItem(
                key=f"page:{page['page_id']}",
                kind="page",
                wave=page["wave"],
                target_repository=REPOSITORY_FALCONE,
                blocked_by=tuple(dict.fromkeys(blocked)),
                spec=page,
            )
        )
    cutover = blueprint["cutover"]
    blocked = [f"page:{p}" for p in cutover["depends_on_pages"] if p in page_ids]
    blocked += [
        f"gap:{item['gap_id']}"
        for item in cutover["blocking_gaps"]
        if item["resolution"] == "RESOLVED" and item["gap_id"] in gap_by_id
    ]
    items.append(
        FanoutItem(
            key="cutover:u5-cutover",
            kind="cutover",
            wave="u5",
            target_repository=REPOSITORY_FALCONE,
            blocked_by=tuple(dict.fromkeys(blocked)),
            spec=cutover,
        )
    )
    return items


def item_change_request_id(blueprint_sha256: str, item_key: str) -> UUID:
    """The deterministic change-request id of one fan-out item (idempotent, resumable)."""
    return uuid5(NAMESPACE_URL, f"hermes-ui-blueprint:{blueprint_sha256}:{item_key}")


def item_context(
    item: FanoutItem,
    *,
    blueprint_id: str,
    blueprint_sha256: str,
    continued: tuple[UiContinuedSection, ...] | None = None,
) -> UiChangeContext:
    if continued is None:
        continued = () if item.is_gap else render_item(item)[1]
    return UiChangeContext(
        blueprint_id=blueprint_id,
        blueprint_sha256=blueprint_sha256,
        item_key=item.key,
        item_kind=item.kind,  # type: ignore[arg-type]
        wave=item.wave,  # type: ignore[arg-type]
        labels=item.labels,
        blocked_by=tuple(
            UiChangeDependency(
                change_request_id=str(item_change_request_id(blueprint_sha256, key)),
                item_key=key,
            )
            for key in item.blocked_by
        ),
        continued=continued,
    )


def _entry(value: Any) -> str:
    """One whitespace-normalised entry; never shortened."""
    return " ".join(str(value).split())


def _all(values: list[Any]) -> str:
    """Every value, comma-separated (deterministic rendering never drops one)."""
    return ", ".join(str(value) for value in values) or "none"


def _join(values: list[str], limit: int = 20) -> str:
    """A summary list for the item statement only (the proposal lists every entry)."""
    shown = ", ".join(values[:limit])
    return shown + (f" and {len(values) - limit} more" if len(values) > limit else "") or "none"


STATEMENT_MAX_BYTES = 7900


def item_description(item: FanoutItem, *, blueprint_id: str) -> str:
    """The bounded, human-readable item statement (a summary: the complete requirements are
    in the rendered proposal and its continued sections)."""
    spec = item.spec
    head = f"UI migration blueprint {blueprint_id}, item {item.key} ({item.kind}"
    head += f", wave {item.wave})." if item.wave else ")."
    if item.kind == "page":
        endpoints = [f"{e['method']} {e['path']} ({e['xcheck_class']})" for e in spec["endpoints"]]
        body = [
            f"Migrate the legacy console page {spec['page_id']} (route {spec['route'] or 'none'}) "
            f"to apps/web-console-next on gntik-ui ({spec['disposition']}): "
            f"{spec['disposition_reason']}",
            f"Target: template {spec['target']['template'] or 'none'}, layout "
            f"{spec['target']['layout'] or 'none'}, blocks {_join(spec['target']['blocks'])}, "
            f"components {_join(spec['target']['components'])}.",
            f"Preserve {len(spec['behaviour']['actions'])} actions and "
            f"{len(spec['behaviour']['validations'])} validations; endpoints: {_join(endpoints)}.",
            f"Legacy files: {_join(spec['legacy_files'], 10)}.",
        ]
    elif item.kind == "epic":
        body = [f"Foundation epic {spec['id']} ({spec['kind']}): {spec['title']}."]
        body += [f"Scope: {len(spec['scope'])} entries, all listed in the proposal."]
    elif item.kind == "cutover":
        body = [
            "Cut over from apps/web-console to apps/web-console-next: "
            + "; ".join(f"{s['action']}: {s['detail']}" for s in spec["steps"])
        ]
        body += [
            f"Requires PARITY_PASS on {len(spec['depends_on_pages'])} pages and i18n "
            "complete in es and en."
        ]
    else:
        body = [
            f"{spec['type']} for {spec['target_repository']}: {spec['summary']}",
            f"Evidence: {_all([_entry(e) for e in spec['evidence']])}.",
            f"Blocks: {_all(list(spec['blocks']))}. Interim: {spec['interim'] or 'none'}.",
        ]
    if item.blocked_by:
        body.append(f"Blocked by: {_all(list(item.blocked_by))}.")
    text = " ".join([head, *(" ".join(part.split()) for part in body)])
    if len(text.encode("utf-8")) <= STATEMENT_MAX_BYTES:
        return text
    marker = " [statement shortened; the complete item is in the proposal]"
    budget = STATEMENT_MAX_BYTES - len(marker.encode("utf-8"))
    return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + marker


def _title(text: str) -> str:
    safe = " ".join(re.sub(r"[^A-Za-z0-9._:/@+ =,\"'-]+", " ", text).split())
    return safe[:100].strip() or "UI migration item"


_EPIC_HIGH_RISK = frozenset({"console-api", "shell-navigation-auth", "csp-assets", "image-chart"})
# The NEW-108 proposal bounds (ChangeProposal): a list that does not fit is rendered in full
# in a "continued" section of the bound issue body instead (never silently cut).
_PROPOSAL_LIST_FIELDS = (
    "what_changes",
    "out_of_scope",
    "acceptance_criteria",
    "risks",
    "rollout",
    "kill_switch",
    "open_questions",
)
_PROPOSAL_MAX_ENTRIES = 15
_PROPOSAL_MAX_ENTRY = 600
_SCALAR_LIMITS = {"why": 3000, "risk_rationale": 1500}


def render_item_proposal(item: FanoutItem) -> dict[str, Any]:
    """The deterministic NEW-108 proposal of an epic, page or cutover item."""
    return render_item(item)[0]


def render_item(item: FanoutItem) -> tuple[dict[str, Any], tuple[UiContinuedSection, ...]]:
    """(proposal, continued sections): every requirement of the item, none dropped.

    A list that fits the proposal bounds stays in the proposal; one that does not is
    replaced by a pointer entry and rendered in full in a continued section. A scalar that
    does not fit is a typed ITEM_RENDER_TOO_LARGE verdict, never a cut."""
    full = _item_lists(item)
    proposal = dict(full)
    continued: list[UiContinuedSection] = []
    for field_name in _PROPOSAL_LIST_FIELDS:
        entries = [entry for entry in (_entry(value) for value in full[field_name]) if entry]
        if len(entries) <= _PROPOSAL_MAX_ENTRIES and all(
            len(entry) <= _PROPOSAL_MAX_ENTRY for entry in entries
        ):
            proposal[field_name] = entries
            continue
        # NEW-123 review 2: an entry that joins many requirements can exceed one continued
        # entry; split it into ordered parts (never cut), and turn any bound violation
        # into a typed verdict the fan-out records, never an uncaught error.
        parts = [part for entry in entries for part in _split_entry(entry)]
        proposal[field_name] = [
            f"All {len(entries)} entries are listed under UI migration: {field_name} "
            "(continued) below."
        ]
        try:
            continued.append(UiContinuedSection(field=field_name, entries=tuple(parts)))  # type: ignore[arg-type]
        except ValidationError as error:
            raise BlueprintRejected("ITEM_RENDER_TOO_LARGE", [field_name]) from error
    for field_name, limit in _SCALAR_LIMITS.items():
        proposal[field_name] = _entry(proposal[field_name])
        if len(proposal[field_name]) > limit:
            raise BlueprintRejected("ITEM_RENDER_TOO_LARGE", [field_name])
    return proposal, tuple(continued)


_CONTINUED_MAX_ENTRY = 19_000


def _split_entry(entry: str) -> list[str]:
    """One requirement entry in ordered parts no longer than a continued entry allows."""
    if len(entry) <= _CONTINUED_MAX_ENTRY:
        return [entry]
    chunks: list[str] = []
    remaining = entry
    while remaining:
        cut = len(remaining)
        if cut > _CONTINUED_MAX_ENTRY:
            cut = remaining.rfind("; ", 0, _CONTINUED_MAX_ENTRY)
            cut = cut + 1 if cut > 0 else remaining.rfind(", ", 0, _CONTINUED_MAX_ENTRY) + 1
            if cut <= 0:
                cut = _CONTINUED_MAX_ENTRY
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    total = len(chunks)
    return [f"(part {index} of {total}) {chunk}" for index, chunk in enumerate(chunks, 1)]


def _item_lists(item: FanoutItem) -> dict[str, Any]:
    spec = item.spec
    if item.kind == "page":
        return _page_proposal(item, spec)
    if item.kind == "epic":
        deployment = spec["kind"] == "image-chart"
        return {
            "title": _title(f"UI u0: {spec['title']}"),
            "kind": "feature",
            "why": f"Foundation epic {spec['id']} ({spec['kind']}) of the approved UI migration "
            "blueprint: every page migration builds on it.",
            "what_changes": list(spec["scope"]),
            "out_of_scope": [
                "Page migrations: one change request per page in waves u1 to u4.",
                "Deleting the legacy console: the u5 cutover.",
            ],
            "affected_repositories": [
                {"repository": REPOSITORY_FALCONE, "role": "source"},
                *(
                    [{"repository": REPOSITORY_FALCONE_CHARTS, "role": "deployment"}]
                    if deployment
                    else []
                ),
            ],
            "acceptance_criteria": list(spec["acceptance_criteria"]),
            "risks": [
                "The new console runs side by side; the legacy console is unchanged until "
                "the u5 cutover."
            ],
            "rollout": ["Merge behind the side-by-side /next/ path; no legacy route changes."],
            "kill_switch": ["Stop serving /next/; the legacy console keeps serving every route."],
            "open_questions": [],
            "proposed_risk": "R2" if spec["kind"] in _EPIC_HIGH_RISK else "R1",
            "risk_rationale": f"Foundation epic of kind {spec['kind']}: "
            + (
                "it touches authentication, the API layer, CSP or the image and chart, which "
                "other pages depend on."
                if spec["kind"] in _EPIC_HIGH_RISK
                else "a bounded addition to the new console only."
            ),
        }
    return {
        "title": "UI u5: cut over to web-console-next and delete the legacy console",
        "kind": "chore",
        "why": "The approved UI migration blueprint ends with a cutover once every migrated "
        "page passes its parity gate and i18n is complete in es and en.",
        "what_changes": [f"{s['action']}: {s['detail']}" for s in spec["steps"]],
        "out_of_scope": [
            "Any page migration or backend change: separate change requests this cutover is "
            "blocked by."
        ],
        "affected_repositories": [
            {"repository": REPOSITORY_FALCONE, "role": "source"},
            {"repository": REPOSITORY_FALCONE_CHARTS, "role": "deployment"},
        ],
        "acceptance_criteria": [
            *spec["acceptance_criteria"],
            f"Every one of these {len(spec['depends_on_pages'])} migrated pages has "
            f"PARITY_PASS: {_all(spec['depends_on_pages'])}.",
            "i18n is complete in es (default) and en.",
        ],
        "risks": [
            "Removing the legacy console is hard to reverse after the one-release grace period."
        ],
        "rollout": [
            "Switch the default route, keep the legacy console for one release, then delete it "
            "with the dead modules."
        ],
        "kill_switch": ["Until deletion, route the default path back to the legacy console."],
        "open_questions": [
            f"Gap {g['gap_id']} is {g['resolution']}: {g['reason']}"
            for g in spec["blocking_gaps"]
            if g["resolution"] == "DEFERRED"
        ],
        "proposed_risk": "R2",
        "risk_rationale": "A cross-component switch of the default console and a deletion that "
        "is hard to reverse after the grace release.",
    }


def _page_proposal(item: FanoutItem, page: dict[str, Any]) -> dict[str, Any]:
    target = page["target"]
    behaviour = page["behaviour"]
    tier = page["complexity"]["tier"]
    endpoints = [f"{e['method']} {e['path']} ({e['xcheck_class']})" for e in page["endpoints"]]
    states = [f"{s['state']}: {s['expected']}" for s in behaviour["states"]]
    guards = [f"{p['guard']}: {p['rule']}" for p in behaviour["permissions"]]
    validations = [f"{v['field']}: {v['rule']}" for v in behaviour["validations"]]
    actions = [
        f"{a['name']} ({a['trigger']}"
        + (f", permission {a['permission']}" if a["permission"] else "")
        + (f", endpoints {_all(a['endpoint_refs'])}" if a["endpoint_refs"] else "")
        + ")"
        for a in behaviour["actions"]
    ]
    tests = list(page["legacy_tests"]["vitest"]) + list(page["legacy_tests"]["playwright_journeys"])
    what = [
        f"Build page {page['page_id']} (route {page['route'] or 'none'}) in "
        f"apps/web-console-next with template {target['template'] or 'none'} and layout "
        f"{target['layout'] or 'none'}: {target['justification']}",
        f"Kit blocks {_all(target['blocks'])}; components {_all(target['components'])}.",
        *([f"Wrap rather than rewrite: {_all(target['wrapped'])}."] if target["wrapped"] else []),
        *([f"Preserve the actions {_all(actions)}."] if actions else []),
        *([f"Preserve the validations {_all(validations)}."] if validations else []),
        *([f"Preserve the permission guards {_all(guards)}."] if guards else []),
        *([f"Render the states {_all(states)}."] if states else []),
        f"Keep the API calls identical: {_all(endpoints)}.",
        f"i18n namespace {page['i18n']['namespace']}: {page['i18n']['string_scope']} "
        f"(about {page['i18n']['estimated_strings']} strings in es, default, and en).",
        *([f"Port the legacy tests {_all(tests)}."] if tests else []),
    ]
    acceptance = [
        f"[{entry['check']}] {entry['criterion']}"
        + (f" (fixtures: {_all(entry['fixtures'])})" if entry["fixtures"] else "")
        for entry in page["parity_acceptance"]
    ] or ["The page passes the flow C parity gate on the mocked backend."]
    return {
        "title": _title(f"UI {item.wave}: migrate {page['page_id']} page to gntik-ui"),
        "kind": "enhancement",
        "why": f"The approved UI migration blueprint moves the legacy console page "
        f"{page['page_id']} to apps/web-console-next on gntik-ui ({page['disposition']}): "
        f"{page['disposition_reason']}",
        "what_changes": what,
        "out_of_scope": [
            "Backend changes: the backend-gap change requests this page is blocked by.",
            "New kit components: kit-gap change requests in gntik-ai/gntik-ui.",
            "Deleting the legacy page: the u5 cutover.",
        ],
        "affected_repositories": [{"repository": REPOSITORY_FALCONE, "role": "source"}],
        "acceptance_criteria": acceptance,
        "risks": [
            "Behaviour drift from the legacy page; the flow C parity gate compares both builds "
            "on identical mocked-backend fixtures."
        ],
        "rollout": ["Served under /next/ next to the legacy page until the u5 cutover."],
        "kill_switch": [
            "Keep using the legacy page; the new page is reachable only under /next/ until the "
            "cutover."
        ],
        "open_questions": [],
        "proposed_risk": "R1" if tier in {"S", "M"} else "R2",
        "risk_rationale": f"Complexity tier {tier} ({page['complexity']['page_loc']} page LOC, "
        f"{page['complexity']['closure_loc']} closure LOC); built side by side without "
        "changing the legacy page.",
    }


def gap_finding(item: FanoutItem) -> dict[str, Any]:
    """The typed finding the triage-ui-gap drafter receives for one blueprint gap."""
    gap = item.spec
    return {
        "finding_ref": item.key,
        "type": gap["type"],
        "severity": "high" if gap["blocks"] else "medium",
        "summary": gap["summary"],
        "evidence": list(gap["evidence"]),
        "blocks": list(gap["blocks"]),
        "interim": gap["interim"],
        "seed_ref": gap["seed_ref"],
        "target_repository": gap["target_repository"],
        "wave": item.wave,
        "source_flow": "A",
    }


def gap_proposal(value: Any, item: FanoutItem) -> dict[str, Any]:
    """The NEW-108 proposal inside one triage-ui-gap draft for this exact gap."""
    errors = schema_errors(value, gap_draft_schema())
    if errors:
        raise BlueprintRejected("GAP_DRAFT_SCHEMA_INVALID", errors)
    assert isinstance(value, dict)
    if value.get("decision") != "DRAFTED" or not isinstance(value.get("change_request"), dict):
        raise BlueprintRejected("GAP_NOT_DRAFTED", [str(value.get("decision"))])
    if value.get("finding_type") != item.spec["type"] or value.get("source_flow") != "A":
        raise BlueprintRejected("GAP_DRAFT_BINDING_MISMATCH", ["finding_type"])
    target = value.get("target") or {}
    if target.get("repository") != item.target_repository:
        raise BlueprintRejected("GAP_ROUTING_MISMATCH", ["target.repository"])
    proposal: dict[str, Any] = value["change_request"]
    if not any(
        entry.get("repository") == item.target_repository
        for entry in proposal["affected_repositories"]
    ):
        raise BlueprintRejected("GAP_ROUTING_MISMATCH", ["affected_repositories"])
    return proposal
