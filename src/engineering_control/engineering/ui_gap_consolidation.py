"""NEW-154: operator-driven consolidation of duplicate UI blueprint gap change requests.

A blueprint fans out one gap request per gap per page batch, so one need (cursor
pagination, a create wizard, a reveal-once dialog) is drafted several times. A bounded model
step (Claude) proposes clusters of equivalent gap requests; Hermes validates the plan
deterministically and stores it with its sha256; the operator applies it by that sha. Apply
creates one canonical request per cluster (drafted by Claude and reviewed by Codex through
the fan-out), marks the members MERGED and re-points every blocked_by entry.

Everything here is pure: the repository and the activities own the durable state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.change_requests import (
    UiChangeContext,
    UiChangeDependency,
    UiContinuedSection,
)
from engineering_control.engineering.ui_blueprint import (
    WAVE_ORDER,
    BlueprintRejected,
    FanoutItem,
    item_change_request_id,
    item_context,
    schema_errors,
)

CONSOLIDATE_PHRASE = "CONSOLIDATE_GAPS"
CONSOLIDATION_KINDS = ("kit-gap", "backend-gap")
MERGED = "MERGED"
PLAN_VERSION = 1
CANONICAL_PREFIX = "gap:merged-"
# Statuses a gap request can be merged from: never PUBLISHED, APPROVED (the operator may be
# publishing it), REJECTED, MERGED or an operator revision in flight (DRAFTING, revision > 1).
MERGEABLE_STATUSES = frozenset({"CANDIDATE", "FAILED"})
MAX_CLUSTER_MEMBERS = 20
MAX_CLUSTERS = 60
# The plan prompt's data budget (the instruction bundle and the task text come on top).
PLAN_DATA_BUDGET = 120_000
PLAN_TIMEOUT_SECONDS = 1800
# The verbatim variants a canonical issue body carries (the body limit is 60000).
CONTINUED_BUDGET = 36_000

UI_GAP_PLAN_INPUT_TOO_LARGE = "UI_GAP_PLAN_INPUT_TOO_LARGE"
UI_GAP_INPUTS_CHANGED = "UI_GAP_INPUTS_CHANGED"
UI_GAP_NOTHING_TO_CONSOLIDATE = "UI_GAP_NOTHING_TO_CONSOLIDATE"


class ConsolidationRejected(BlueprintRejected):
    """A consolidation plan (or apply) that the deterministic checks refuse."""


def consolidation_workflow_id(consolidation_id: str) -> str:
    return f"ui-gap-consolidate-{consolidation_id}"


def is_mergeable(status: str, revision: int, issue_url: str | None) -> bool:
    """A gap request may be merged: not published, decided or being revised. A fresh
    fan-out item never drafted (DRAFTING at revision 1) may be merged too."""
    if issue_url:
        return False
    return status in MERGEABLE_STATUSES or (status == "DRAFTING" and revision == 1)


def is_canonical_key(item_key: str) -> bool:
    return item_key.startswith(CANONICAL_PREFIX)


def canonical_key(member_keys: Iterable[str]) -> str:
    """The deterministic item key of the canonical request of these members."""
    digest = hashlib.sha256("\n".join(sorted(set(member_keys))).encode("utf-8")).hexdigest()
    return f"{CANONICAL_PREFIX}{digest[:16]}"


# Plan inputs ------------------------------------------------------------------------------


def gap_links(blueprint: Mapping[str, Any], gap_id: str) -> list[str]:
    """The blueprint items that name this gap: pages (gap_ids or the gap's blocks), epics
    (gap_ids or blocks) and the cutover (blocking_gaps)."""
    gaps = {gap["id"]: gap for gap in blueprint.get("gaps") or []}
    blocks = set((gaps.get(gap_id) or {}).get("blocks") or [])
    links: list[str] = []
    for epic in blueprint.get("foundation_epics") or []:
        if gap_id in (epic.get("gap_ids") or []) or epic.get("id") in blocks:
            links.append(f"epic:{epic['id']}")
    for page in blueprint.get("pages") or []:
        if page.get("disposition") == "DELETE":
            continue
        if gap_id in (page.get("gap_ids") or []) or page.get("page_id") in blocks:
            links.append(f"page:{page['page_id']}")
    cutover = blueprint.get("cutover") or {}
    if any(item.get("gap_id") == gap_id for item in cutover.get("blocking_gaps") or []):
        links.append("cutover:u5-cutover")
    return list(dict.fromkeys(links))


def candidate(
    item: FanoutItem,
    *,
    change_request_id: UUID,
    status: str,
    revision: int,
    proposal: Mapping[str, Any] | None,
    linked: Sequence[str],
) -> dict[str, Any]:
    """One gap request as the plan step sees it (and as the plan input hash binds it)."""
    proposal = proposal or {}
    spec = item.spec
    return {
        "change_request_id": str(change_request_id),
        "item_key": item.key,
        "item_kind": item.kind,
        "target_repository": item.target_repository,
        "wave": item.wave,
        "status": status,
        "revision": revision,
        "summary": str(spec.get("summary") or ""),
        "title": str(proposal.get("title") or ""),
        "why": str(proposal.get("why") or ""),
        "what_changes": [str(entry) for entry in proposal.get("what_changes") or []],
        "acceptance_criteria": [str(entry) for entry in proposal.get("acceptance_criteria") or []],
        "linked": list(linked),
    }


def inputs_sha256(
    *, blueprint_id: str, blueprint_sha256: str, kind: str | None, candidates: list[dict[str, Any]]
) -> str:
    return canonical_sha256(
        {
            "version": PLAN_VERSION,
            "blueprint_id": blueprint_id,
            "blueprint_sha256": blueprint_sha256,
            "kind": kind,
            "candidates": candidates,
        }
    )


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def prompt_data(candidates: list[dict[str, Any]], budget: int = PLAN_DATA_BUDGET) -> str:
    """The bounded JSON the plan step reads: every candidate, shortened deterministically
    (fewer and shorter entries) until it fits. Raises when even the shortest form does not
    (the operator then plans one kind at a time)."""
    for why, entry, entries in ((900, 400, 12), (500, 240, 8), (300, 160, 6), (160, 100, 4)):
        data = [
            {
                "key": item["item_key"],
                "kind": item["item_kind"],
                "target_repository": item["target_repository"],
                "status": item["status"],
                "title": _clip(item["title"] or item["summary"], 160),
                "summary": _clip(item["summary"], why),
                "why": _clip(item["why"], why),
                "what_changes": [_clip(text, entry) for text in item["what_changes"][:entries]],
                "acceptance_criteria": [
                    _clip(text, entry) for text in item["acceptance_criteria"][:entries]
                ],
                "linked_items": item["linked"][:40],
            }
            for item in candidates
        ]
        text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False)
        if len(text) <= budget:
            return text
    raise ConsolidationRejected(UI_GAP_PLAN_INPUT_TOO_LARGE, [f"{len(candidates)} gap requests"])


# The pinned plan contract ------------------------------------------------------------------


def plan_schema() -> dict[str, Any]:
    """The structured output the plan step must return (Hermes-pinned: the step's request
    sends exactly this schema and the result is validated against it again)."""
    key = {"type": "string", "minLength": 5, "maxLength": 104}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["input_sha256", "clusters", "unclustered_keys", "notes"],
        "properties": {
            "input_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "clusters": {
                "type": "array",
                "maxItems": MAX_CLUSTERS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "member_keys", "rationale"],
                    "properties": {
                        "title": {"type": "string", "minLength": 3, "maxLength": 100},
                        "member_keys": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": MAX_CLUSTER_MEMBERS,
                            "items": key,
                        },
                        "rationale": {"type": "string", "minLength": 10, "maxLength": 1500},
                    },
                },
            },
            "unclustered_keys": {"type": "array", "maxItems": 600, "items": key},
            "notes": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
            },
        },
    }


def check_plan(
    value: Any,
    *,
    input_sha256: str,
    candidates: list[dict[str, Any]],
    known: Mapping[str, str],
) -> dict[str, Any]:
    """The deterministic plan check. `candidates` are the mergeable gap requests the plan
    was asked about (in blueprint order); `known` maps every gap item key of the blueprint
    to its status. Refuses (with every problem found): a schema violation, another input
    hash, an unknown or ineligible key, a key in two clusters (or twice in one, or both in a
    cluster and unclustered), and a cluster that mixes repositories or gap kinds. Keys the
    plan does not mention stay unclustered."""
    errors = schema_errors(value, plan_schema())
    if errors:
        raise ConsolidationRejected("UI_GAP_PLAN_SCHEMA_INVALID", errors)
    assert isinstance(value, dict)
    if value["input_sha256"] != input_sha256:
        raise ConsolidationRejected("UI_GAP_PLAN_INPUT_MISMATCH", ["input_sha256"])
    eligible = {item["item_key"]: item for item in candidates}
    order = {key: index for index, key in enumerate(eligible)}
    problems: list[str] = []
    seen: dict[str, str] = {}

    def claim(key: str, where: str) -> None:
        if key not in eligible:
            if key in known:
                problems.append(f"{where}: {key} is not eligible (status {known[key]})")
            else:
                problems.append(f"{where}: {key} is not a gap request of this plan")
            return
        if key in seen:
            problems.append(f"{where}: {key} is already in {seen[key]}")
            return
        seen[key] = where

    clusters: list[dict[str, Any]] = []
    for index, cluster in enumerate(value["clusters"], start=1):
        where = f"cluster {index}"
        for key in cluster["member_keys"]:
            claim(key, where)
        members = [
            eligible[key] for key in dict.fromkeys(cluster["member_keys"]) if key in eligible
        ]
        repositories = sorted({item["target_repository"] for item in members})
        kinds = sorted({item["item_kind"] for item in members})
        if len(repositories) > 1:
            problems.append(f"{where}: mixes repositories {', '.join(repositories)}")
        if len(kinds) > 1:
            problems.append(f"{where}: mixes gap kinds {', '.join(kinds)}")
        clusters.append(
            {
                "title": " ".join(cluster["title"].split()),
                "rationale": " ".join(cluster["rationale"].split()),
                "member_keys": sorted(
                    dict.fromkeys(cluster["member_keys"]), key=lambda key: order.get(key, 10**6)
                ),
            }
        )
    for key in value["unclustered_keys"]:
        claim(key, "unclustered")
    if problems:
        raise ConsolidationRejected("UI_GAP_PLAN_REJECTED", problems)
    clusters.sort(key=lambda cluster: order[cluster["member_keys"][0]])
    unclustered = [key for key in eligible if key not in seen or seen[key] == "unclustered"]
    return {
        "clusters": clusters,
        "unclustered": unclustered,
        "notes": [" ".join(note.split()) for note in value["notes"]],
    }


def build_plan(
    checked: dict[str, Any],
    *,
    blueprint_id: str,
    blueprint_sha256: str,
    kind: str | None,
    input_sha256: str,
    candidates: list[dict[str, Any]],
    drafter: dict[str, Any],
) -> dict[str, Any]:
    """The stored plan: every cluster with its deterministic canonical key and id, its
    members (id, key, status and revision at plan time) and the drafter's rationale."""
    by_key = {item["item_key"]: item for item in candidates}
    clusters = []
    for cluster in checked["clusters"]:
        members = [by_key[key] for key in cluster["member_keys"]]
        key = canonical_key(cluster["member_keys"])
        clusters.append(
            {
                "cluster_key": key,
                "canonical_id": str(item_change_request_id(blueprint_sha256, key)),
                "title": cluster["title"],
                "rationale": cluster["rationale"],
                "item_kind": members[0]["item_kind"],
                "target_repository": members[0]["target_repository"],
                "members": [
                    {
                        "change_request_id": item["change_request_id"],
                        "item_key": item["item_key"],
                        "status": item["status"],
                        "revision": item["revision"],
                    }
                    for item in members
                ],
            }
        )
    return {
        "version": PLAN_VERSION,
        "blueprint_id": blueprint_id,
        "blueprint_sha256": blueprint_sha256,
        "kind": kind,
        "input_sha256": input_sha256,
        "clusters": clusters,
        "unclustered": list(checked["unclustered"]),
        "notes": list(checked["notes"]),
        "drafter": dict(drafter),
    }


def plan_sha256(plan: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(plan))


def plan_preview(plan: Mapping[str, Any]) -> list[str]:
    """One readable line per cluster for the operator."""
    return [
        f"{cluster['title']} [{cluster['item_kind']} -> {cluster['target_repository']}]: "
        f"{len(cluster['members'])} requests "
        f"({', '.join(member['item_key'] for member in cluster['members'])}) -> "
        f"{cluster['canonical_id']} ({cluster['cluster_key']}). {cluster['rationale']}"
        for cluster in plan.get("clusters") or []
    ]


# Canonical requests --------------------------------------------------------------------------


def _earliest(waves: Iterable[str | None]) -> str | None:
    present = {wave for wave in waves if wave}
    return next((wave for wave in WAVE_ORDER if wave in present), None)


def canonical_record(
    cluster: Mapping[str, Any],
    *,
    member_items: Mapping[str, FanoutItem],
    variants: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    """Everything the canonical request is built from, fixed at apply time and stored with
    the consolidation (so a resumed fan-out rebuilds exactly the same item): a synthetic gap
    spec in the blueprint's gap shape, the union of the members' evidence, blocks and
    links, and every member's drafted proposal as a variant."""
    keys = [member["item_key"] for member in cluster["members"]]
    items = [member_items[key] for key in keys]
    specs = [item.spec for item in items]
    gap_type = "BACKEND_GAP" if cluster["item_kind"] == "backend-gap" else "KIT_GAP"
    interims = list(
        dict.fromkeys(str(spec.get("interim")) for spec in specs if spec.get("interim"))
    )
    spec = {
        "id": str(cluster["cluster_key"]).removeprefix("gap:"),
        "type": gap_type,
        "target_repository": cluster["target_repository"],
        "summary": (
            f"Consolidated gap, replacing {len(keys)} duplicate gap requests: "
            f"{cluster['title']}. {cluster['rationale']}"
        ),
        "evidence": list(
            dict.fromkeys(str(entry) for spec in specs for entry in spec.get("evidence") or [])
        ),
        "blocks": list(
            dict.fromkeys(str(entry) for spec in specs for entry in spec.get("blocks") or [])
        ),
        "interim": "; ".join(interims) if interims else None,
        "seed_ref": next((spec.get("seed_ref") for spec in specs if spec.get("seed_ref")), None),
        "title": cluster["title"],
        "rationale": cluster["rationale"],
        "members": [
            {"change_request_id": member["change_request_id"], "item_key": member["item_key"]}
            for member in cluster["members"]
        ],
        "variants": [
            {
                "item_key": key,
                "title": variants[key]["title"] or variants[key]["summary"],
                "why": variants[key]["why"],
                "what_changes": list(variants[key]["what_changes"]),
                "acceptance_criteria": list(variants[key]["acceptance_criteria"]),
            }
            for key in keys
        ],
        "needed_by": list(
            dict.fromkeys(link for key in keys for link in variants[key].get("linked") or [])
        ),
    }
    return {
        "cluster_key": cluster["cluster_key"],
        "canonical_id": cluster["canonical_id"],
        "item_kind": cluster["item_kind"],
        "target_repository": cluster["target_repository"],
        "wave": _earliest(item.wave for item in items),
        "spec": spec,
    }


def canonical_item(record: Mapping[str, Any]) -> FanoutItem:
    return FanoutItem(
        key=str(record["cluster_key"]),
        kind=str(record["item_kind"]),
        wave=record.get("wave"),
        target_repository=str(record["target_repository"]),
        blocked_by=(),
        spec=dict(record["spec"]),
    )


def canonical_items(results: Iterable[Mapping[str, Any]]) -> list[FanoutItem]:
    """The canonical fan-out items of the applied consolidations (their result records)."""
    items: dict[str, FanoutItem] = {}
    for result in results:
        for record in result.get("canonicals") or []:
            if isinstance(record, dict) and record.get("cluster_key") not in items:
                items[str(record["cluster_key"])] = canonical_item(record)
    return list(items.values())


def canonical_continued(item: FanoutItem) -> tuple[UiContinuedSection, ...]:
    """Every member's acceptance criteria (and, within the body budget, what_changes),
    verbatim and labelled with its item key, rendered in full in the canonical issue body:
    no variant's criterion is ever dropped. Past CONTINUED_BUDGET characters the
    what_changes section is left to the drafted proposal, which merges them."""
    sections: dict[str, UiContinuedSection] = {}
    for name in ("acceptance_criteria", "what_changes"):
        entries = [
            f"[{variant['item_key']}] {' '.join(str(entry).split())}"[:19_000]
            for variant in item.spec.get("variants") or []
            for entry in variant.get(name) or []
            if str(entry).strip()
        ][:300]
        if entries:
            sections[name] = UiContinuedSection(field=name, entries=tuple(entries))
    size = sum(len(entry) for section in sections.values() for entry in section.entries)
    if size > CONTINUED_BUDGET:
        sections.pop("what_changes", None)
    return tuple(
        sections[name] for name in ("what_changes", "acceptance_criteria") if name in sections
    )


def canonical_context(
    item: FanoutItem, *, blueprint_id: str, blueprint_sha256: str
) -> UiChangeContext:
    context = item_context(
        item,
        blueprint_id=blueprint_id,
        blueprint_sha256=blueprint_sha256,
        continued=canonical_continued(item),
    )
    return UiChangeContext.model_validate(
        {
            **context.model_dump(mode="json"),
            "merged_from": [
                UiChangeDependency(
                    change_request_id=str(member["change_request_id"]),
                    item_key=str(member["item_key"]),
                ).model_dump(mode="json")
                for member in item.spec.get("members") or []
            ],
            "needed_by": list(item.spec.get("needed_by") or [])[:300],
        }
    )


def canonical_variants_text(item: FanoutItem, limit: int = 40_000) -> str:
    """The members' drafted proposals, for the canonical drafter (bounded)."""
    text = json.dumps(item.spec.get("variants") or [], indent=1, sort_keys=True, ensure_ascii=False)
    return (
        text
        if len(text) <= limit
        else text[:limit] + " [truncated: every entry is in the issue body]"
    )
