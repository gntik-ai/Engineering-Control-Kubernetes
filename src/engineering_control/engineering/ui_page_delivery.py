"""NEW-227: a published UI page change request is delivered with the UI page skills.

Recognition is Hermes-held provenance, never labels a person could add: the delivered issue
is the one the operator published for a fan-out page change request (its id is the
deterministic ``item_change_request_id`` of the page under its blueprint), and that
blueprint is still APPROVED and verifies against its binding. Its maker is the
ui-page-implementer (skill migrate-ui-page), its checker the ui-page-reviewer (skill
review-ui-page). Every other delivery is unchanged.

Everything here is pure and deterministic: the page context the delivery carries, the
bounded legacy drift of the page's closure since the blueprint's source commit (git output
in, text out), the bounded kit snapshot excerpt, the maker and checker input section (under
24 KiB, the AGENTS.md bound per model input) and the mapping of both skills' released
outputs to the delivery maker result and checker verdict the workflow consumes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from engineering_control.engineering.backlog import canonical_sha256
from engineering_control.engineering.change_requests import UiChangeContext
from engineering_control.engineering.ui_blueprint import (
    UI_PAGE_SKILLS,
    item_change_request_id,
    load_schema,
)

UI_PAGE_MAKER_ROLE = "ui-page-implementer"
UI_PAGE_CHECKER_ROLE = "ui-page-reviewer"
# The delivery maker and checker roles Hermes's own lookups match (checker findings carried
# to a correction round, amended-work checks, the correction extension).
MAKER_ROLES = ("implementer", UI_PAGE_MAKER_ROLE)
CHECKER_ROLES = ("code-reviewer", UI_PAGE_CHECKER_ROLE)
HARD_RULES = (
    "api-call-parity",
    "scope-no-backend-legacy-contract-change",
    "kit-only-styling",
    "no-sample-data-fallback",
    "i18n-complete",
    "tests-present",
    "environment-skips-reported",
)
CHECKS = (
    "spec-conformance",
    "data",
    "action",
    "error-empty",
    "accessibility",
    "kit-conformance",
    "runtime-health",
    "responsive",
    "visual-review",
    "i18n",
    "tests",
)
VIEWPORTS = frozenset({"mobile", "tablet", "desktop"})
LOCALES = frozenset({"es", "en"})
CLOSURE_MAX_FILES = 300
KIT_SELECTION_MAX = 100
COMMITS_SHOWN = 20
COMMITS_MAX = 200
CHANGED_FILES_SHOWN = 60
# Bytes (UTF-8) of the section Hermes adds to one maker or checker prompt, fences and
# headers included (AGENTS.md: 24 KiB per model input). The approved page entry and its
# epics and gaps are authoritative: never cut, a larger one BLOCKs (SPEC_LIMIT, fenced).
# The kit excerpt, the maker's evidence (checker) and the legacy drift share the rest; the
# drift is cut last, with the git command that shows the remainder.
INPUT_BUDGET = 24_576
SPEC_LIMIT = {"maker": 16_384, "checker": 12_288}
KIT_BUDGET = 3_584
DRIFT_BUDGET = 5_120
EVIDENCE_BUDGET = 4_096
_SHA = re.compile(r"^[a-f0-9]{40}$")
_KIT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_DIFF_OPTIONS = ("--no-color", "--no-ext-diff", "--no-textconv", "--no-renames")
_KIT_ITEM_TOOLS = ("get_template", "get_block", "get_component")


class UiPageRejected(ValueError):  # noqa: N818 - a typed blocker, not a programming error
    """A UI page delivery input that cannot be trusted or built; ``code`` is the blocker."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail[:300]


def maker_schema() -> dict[str, Any]:
    return load_schema(UI_PAGE_SKILLS["migrate-ui-page"][1])


def checker_schema() -> dict[str, Any]:
    return load_schema(UI_PAGE_SKILLS["review-ui-page"][1])


# --------------------------------------------------------------------------------------
# Recognition: the page a delivered issue was published from
# --------------------------------------------------------------------------------------


def page_entry(blueprint: Mapping[str, Any], page_id: str) -> dict[str, Any]:
    """The approved, fanned-out (not DELETE) page entry of the blueprint."""
    for page in blueprint.get("pages") or []:
        if isinstance(page, dict) and page.get("page_id") == page_id:
            if page.get("disposition") == "DELETE":
                break
            return page
    raise UiPageRejected("UI_PAGE_NOT_IN_BLUEPRINT", f"page {page_id} is not a migrated page")


def closure(page: Mapping[str, Any]) -> list[str]:
    """The page's legacy closure: its file, the components and libraries the inventory lists
    for it (the blueprint's mechanical legacy_files) and its Vitest and Playwright tests."""
    tests = page.get("legacy_tests") or {}
    files = [
        *(page.get("legacy_files") or []),
        *(tests.get("vitest") or []),
        *(tests.get("playwright_journeys") or []),
    ]
    unique = list(dict.fromkeys(files))
    for path in unique:
        if (
            not isinstance(path, str)
            or not 0 < len(path) <= 300
            or path.startswith(("/", "-"))
            or any(character in path for character in "\0\n\r")
            or ".." in PurePosixPath(path).parts
        ):
            raise UiPageRejected("UI_PAGE_CLOSURE_INVALID", "a legacy file path is not relative")
    if not unique or len(unique) > CLOSURE_MAX_FILES:
        raise UiPageRejected("UI_PAGE_CLOSURE_INVALID", f"{len(unique)} legacy files")
    return unique


def page_context(
    change_request_id: str, ui_context: Any, loaded: Mapping[str, Any]
) -> dict[str, Any]:
    """The bounded page context a delivery carries (no page content: makers re-load it).

    ``loaded`` is ``UiBlueprintRepository.load_approved_sync`` of the context's blueprint."""
    try:
        context = UiChangeContext.model_validate(ui_context)
    except ValidationError as error:
        raise UiPageRejected(
            "UI_PAGE_PROVENANCE_INVALID", "stored UI context is invalid"
        ) from error
    if context.item_kind != "page":
        raise UiPageRejected("UI_PAGE_PROVENANCE_INVALID", "not a page change request")
    if str(item_change_request_id(context.blueprint_sha256, context.item_key)) != str(
        change_request_id
    ):
        # Only the blueprint's own fan-out creates a page request under this identity.
        raise UiPageRejected("UI_PAGE_PROVENANCE_INVALID", "not its blueprint's fan-out item")
    if loaded.get("status") != "APPROVED":
        raise UiPageRejected(
            "UI_PAGE_BLUEPRINT_NOT_APPROVED", f"blueprint is {loaded.get('current_status')}"
        )
    if loaded.get("blueprint_sha256") != context.blueprint_sha256:
        raise UiPageRejected("UI_PAGE_BLUEPRINT_CHANGED", "blueprint hash differs from the request")
    source_sha = loaded.get("source_sha")
    if not isinstance(source_sha, str) or not _SHA.fullmatch(source_sha):
        raise UiPageRejected("UI_PAGE_PROVENANCE_INVALID", "blueprint has no source commit")
    page = page_entry(loaded.get("blueprint") or {}, context.item_key.removeprefix("page:"))
    if page.get("wave") != context.wave:
        raise UiPageRejected("UI_PAGE_PROVENANCE_INVALID", "page wave differs from the request")
    target = page.get("target") or {}
    template = target.get("template")

    def kit_ids(values: Any) -> list[str]:
        # Every spec item is a required kit call: one Hermes cannot request blocks.
        items = sorted(set(values or []))
        if len(items) > KIT_SELECTION_MAX or not all(
            isinstance(item, str) and _KIT_ID.fullmatch(item) for item in items
        ):
            raise UiPageRejected("UI_PAGE_KIT_SELECTION_INVALID", "unsupported kit item ids")
        return items

    return {
        "change_request_id": str(change_request_id),
        "blueprint_id": context.blueprint_id,
        "blueprint_sha256": context.blueprint_sha256,
        "page_id": str(page["page_id"]),
        "wave": context.wave,
        "page_sha256": canonical_sha256(page),
        "source_sha": source_sha,
        "closure": closure(page),
        "kit_selection": {
            "templates": kit_ids([template] if template else []),
            "blocks": kit_ids(target.get("blocks")),
            "components": kit_ids(target.get("components")),
        },
    }


def page_view(blueprint: Mapping[str, Any], ui_page: Mapping[str, Any]) -> dict[str, Any]:
    """The page entry with its referenced epics and gaps, re-checked against the context."""
    page = page_entry(blueprint, str(ui_page.get("page_id")))
    if canonical_sha256(page) != ui_page.get("page_sha256"):
        raise UiPageRejected("UI_PAGE_BLUEPRINT_CHANGED", "page entry differs from its context")
    epics = {
        epic["id"]: epic
        for epic in blueprint.get("foundation_epics") or []
        if isinstance(epic, dict) and "id" in epic
    }
    gaps = [gap for gap in blueprint.get("gaps") or [] if isinstance(gap, dict) and "id" in gap]
    # The same references the fan-out rendered (ui_blueprint.fanout_items: page_gaps).
    gap_ids = list(
        dict.fromkeys(
            [
                *(page.get("gap_ids") or []),
                *(gap["id"] for gap in gaps if page["page_id"] in (gap.get("blocks") or [])),
            ]
        )
    )
    by_id = {gap["id"]: gap for gap in gaps}
    return {
        "page": page,
        "epics": [
            {key: epics[name].get(key) for key in ("id", "kind", "title", "depends_on")}
            for name in page.get("depends_on") or []
            if name in epics
        ],
        "gaps": [
            {
                key: by_id[name].get(key)
                for key in ("id", "type", "target_repository", "summary", "interim")
            }
            for name in gap_ids
            if name in by_id
        ],
    }


# --------------------------------------------------------------------------------------
# Inputs: legacy drift and the kit excerpt (computed on the scan worker)
# --------------------------------------------------------------------------------------


def bounded_text(text: str, limit: int, note: str) -> str:
    """At most ``limit`` UTF-8 bytes, cut at a line where possible, with a marker."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    marker = f"\n[{note}: {{shown}} of {len(data)} bytes shown]"
    budget = max(0, limit - len(marker.encode("utf-8")) - 12)
    cut = data[:budget].decode("utf-8", errors="ignore")
    newline = cut.rfind("\n")
    if newline > budget // 2:
        cut = cut[:newline]
    return cut + marker.format(shown=len(cut.encode("utf-8")))


def legacy_drift(
    git: Callable[[list[str]], str], *, source_sha: str, base_sha: str, files: Sequence[str]
) -> dict[str, Any]:
    """The legacy closure's changes from the blueprint's source commit to the delivery base:
    the commits touching it, the changed files and the diff, bounded to DRIFT_BUDGET.

    Deterministic for the two commits and the file list (full commit ids, no renames, no
    external diff or textconv drivers, literal pathspecs)."""
    for name, value in (("source_sha", source_sha), ("base_sha", base_sha)):
        if not _SHA.fullmatch(value):
            raise UiPageRejected("UI_PAGE_LEGACY_DRIFT_UNAVAILABLE", f"{name} is not a commit")
    pathspec = ["--", *files]
    log = git(
        [
            "--literal-pathspecs",
            "log",
            "--format=%H%x09%s",
            f"--max-count={COMMITS_MAX + 1}",
            f"{source_sha}..{base_sha}",
            *pathspec,
        ]
    )
    numstat = git(
        ["--literal-pathspecs", "diff", "--numstat", "-z", *_DIFF_OPTIONS, source_sha, base_sha]
        + pathspec
    )
    diff = git(
        ["--literal-pathspecs", "diff", *_DIFF_OPTIONS, "--unified=3", source_sha, base_sha]
        + pathspec
    )
    commits = [line.split("\t", 1) for line in log.splitlines() if line.strip()]
    changed = []
    for entry in numstat.split("\0"):
        fields = entry.split("\t", 2)
        if len(fields) == 3:
            changed.append((fields[2], fields[0], fields[1]))
    lines = [
        f"Legacy closure: {len(files)} files; blueprint source {source_sha}, delivery base "
        f"{base_sha}."
    ]
    if not changed:
        lines.append(
            "No file of the legacy closure changed since the blueprint: nothing to carry over."
        )
    else:
        more = "+" if len(commits) > COMMITS_MAX else ""
        lines.append(f"Commits touching it ({min(len(commits), COMMITS_MAX)}{more}, newest first):")
        lines.extend(
            f"- {commit[0][:12]} {' '.join((commit[1] if len(commit) > 1 else '').split())[:120]}"
            for commit in commits[:COMMITS_SHOWN]
        )
        if len(commits) > COMMITS_SHOWN:
            lines.append(f"- and {min(len(commits), COMMITS_MAX) - COMMITS_SHOWN}{more} more")
        lines.append(f"Changed files ({len(changed)}):")
        lines.extend(
            f"- {path} (+{added} -{deleted})"
            for path, added, deleted in changed[:CHANGED_FILES_SHOWN]
        )
        if len(changed) > CHANGED_FILES_SHOWN:
            lines.append(f"- and {len(changed) - CHANGED_FILES_SHOWN} more")
        lines.extend(["Diff:", diff.rstrip("\n")])
    text = "\n".join(lines)
    note = (
        f"legacy diff truncated; run `git diff {source_sha} {base_sha} -- <file>` in the "
        "source worktree for the rest"
    )
    data = diff.encode("utf-8")
    return {
        "from_sha": source_sha,
        "to_sha": base_sha,
        "files": len(files),
        "changed_files": len(changed),
        "commits": min(len(commits), COMMITS_MAX),
        "diff_bytes": len(data),
        "diff_sha256": hashlib.sha256(data).hexdigest(),
        "truncated": len(text.encode("utf-8")) > DRIFT_BUDGET,
        "text": bounded_text(text, DRIFT_BUDGET, note),
    }


def kit_failures(document: Mapping[str, Any]) -> list[str]:
    """The required calls (get_rules and every spec item) that failed or are missing."""
    calls = [call for call in document.get("calls") or [] if isinstance(call, dict)]
    failed = [
        f"{call.get('tool')} {(call.get('arguments') or {}).get('id') or ''}".strip()
        for call in calls
        if call.get("tool") in ("get_rules", *_KIT_ITEM_TOOLS) and call.get("is_error")
    ]
    if not any(call.get("tool") == "get_rules" for call in calls):
        failed.append("get_rules (not called)")
    return failed


def kit_excerpt(document: Mapping[str, Any]) -> dict[str, Any]:
    """A bounded excerpt of a page's kit snapshot: the rules, then each spec item."""
    calls = [call for call in document.get("calls") or [] if isinstance(call, dict)]
    rules = [call for call in calls if call.get("tool") == "get_rules"]
    items = [call for call in calls if call.get("tool") in _KIT_ITEM_TOOLS]

    def described(call: Mapping[str, Any]) -> str:
        raw = call.get("arguments")
        arguments: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
        state = "ERROR, " if call.get("is_error") else ""
        name = f" {arguments.get('id')}" if arguments.get("id") else ""
        size = f"{call.get('bytes')} bytes, sha256 {call.get('sha256')}"
        return f"{call.get('tool')}{name} ({state}{size})"

    rules_budget = KIT_BUDGET * 2 // 5
    item_budget = max(160, (KIT_BUDGET - rules_budget) // max(1, len(items)) - 120)
    parts = [
        f"gntik-ui {document.get('gntik_ui_version')} snapshot {document.get('snapshot_sha256')} "
        f"from {document.get('image')}."
    ]
    for call in rules:
        parts.append(
            described(call)
            + ":\n"
            + bounded_text(str(call.get("text") or ""), rules_budget, "rules truncated")
        )
    for call in items:
        parts.append(
            described(call)
            + ": "
            + bounded_text(str(call.get("text") or ""), item_budget, "item truncated")
        )
    return {
        "snapshot_sha256": document.get("snapshot_sha256"),
        "gntik_ui_version": document.get("gntik_ui_version"),
        "image": document.get("image"),
        "calls": len(calls),
        "errors": sum(1 for call in calls if call.get("is_error")),
        "text": bounded_text("\n".join(parts), KIT_BUDGET, "kit excerpt truncated"),
    }


# --------------------------------------------------------------------------------------
# The maker and checker prompt sections
# --------------------------------------------------------------------------------------


def _compact(value: Any) -> str:
    # The exact form page_sha256 hashes (backlog.canonical_sha256).
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def maker_instructions(ui_page: Mapping[str, Any], inputs: Mapping[str, Any]) -> str:
    kit = inputs.get("kit") or {}
    return (
        "UI page migration (NEW-227): this ChangeSet migrates the legacy console page "
        f"{ui_page['page_id']} (wave {ui_page['wave']}) of the approved UI migration "
        f"blueprint {ui_page['blueprint_id']} with the migrate-ui-page skill. The approved "
        "page entry and its referenced epics and gaps below are the spec; they override any "
        "summary of them in the OpenSpec or the issue. Return page_id "
        f"{ui_page['page_id']} and blueprint_sha256 {ui_page['blueprint_sha256']} (the page "
        f"entry itself hashes to {ui_page['page_sha256']}). Models get no MCP server: the "
        "gntik_ui tools the skill names are not available, so use the Hermes kit snapshot "
        f"below for the pinned gntik-ui {kit.get('gntik_ui_version')} and report "
        "kit.validate_page NOT_RUN with that reason. The legacy page changed after the "
        "blueprint was approved: carry every change in the legacy changes since the "
        "blueprint below (behaviour, API calls, validations, states and copy) over into the "
        "new page, or list each one you do not carry over in spec_deviations. COMPLETE "
        "requires spec_deviations empty, i18n.missing_keys empty, kit.explicit_props true "
        "and every api_calls entry identical; Hermes treats any other COMPLETE as BLOCKED.\n"
    )


def checker_instructions(ui_page: Mapping[str, Any]) -> str:
    return (
        "UI page review (NEW-227): review this ChangeSet with the review-ui-page skill, "
        f"review_context delivery and page_id {ui_page['page_id']}, against the approved page "
        "entry, its epics and gaps, the kit snapshot, the legacy changes since the blueprint "
        "and the UI page maker's result below. Every legacy change since the blueprint must "
        "be carried over or listed in the maker's spec_deviations; one silently dropped is a "
        "BUG_NEW_UI finding. Models get no MCP server: use the kit snapshot below. No parity "
        "evidence bundle exists at this stage: the pull request's CI runs the mirrored "
        "mocked-backend Playwright journeys at the three viewports and decides the merge. "
        "Evaluate each check from the diff, the legacy source, the tests and the maker's "
        "evidence, citing file:line references, and report a check NOT_EVALUATED only when "
        "none of them lets you judge it. Hermes accepts PASS only when every hard rule and "
        "check is PASS or NOT_APPLICABLE and no finding is blocking. Never edit files.\n"
    )


def _fenced(fence: Callable[[str, str], str], label: str, text: str, limit: int, note: str) -> str:
    """``fence(label, text)`` with ``text`` cut until the block holds at most ``limit``
    content bytes: the fence neutralises delimiter-like markers, which can expand it."""
    overhead = len(fence(label, "").encode("utf-8"))
    cut = limit
    while True:
        block = fence(label, bounded_text(text, cut, note))
        excess = len(block.encode("utf-8")) - overhead - limit
        if excess <= 0 or cut == 0:
            return block
        cut = max(0, cut - excess)


def input_section(
    view: Mapping[str, Any],
    ui_page: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    role: str,
    fence: Callable[[str, str], str],
    maker_result: Mapping[str, Any] | None = None,
) -> str:
    """The page spec and its references (complete), the kit excerpt, the maker's UI
    evidence (checker) and the legacy drift: at most INPUT_BUDGET bytes as rendered."""
    references = "\n".join(
        [
            *(f"epic {_compact(epic)}" for epic in view.get("epics") or []),
            *(f"gap {_compact(gap)}" for gap in view.get("gaps") or []),
        ]
    )
    spec = fence(
        "ui page spec",
        f"{_compact(view['page'])}\nReferenced epics and gaps:\n{references or 'none'}",
    )
    size = len(spec.encode("utf-8"))
    if size > SPEC_LIMIT[role]:
        raise UiPageRejected(
            "UI_PAGE_SPEC_TOO_LARGE", f"page entry with references is {size} bytes fenced"
        )
    kit = inputs.get("kit") or {}
    drift = inputs.get("drift") or {}
    parts = [
        f"Approved page entry {ui_page['page_id']} of blueprint {ui_page['blueprint_id']} "
        f"(blueprint sha256 {ui_page['blueprint_sha256']}; the page entry, canonical JSON, "
        f"sha256 {ui_page['page_sha256']}) and its referenced epics and gaps, complete:",
        spec,
        f"gntik-ui kit snapshot {str(kit.get('snapshot_sha256'))[:64]} for the pinned "
        f"version {str(kit.get('gntik_ui_version'))[:40]} (Hermes-collected):",
        _fenced(
            fence,
            "gntik-ui kit snapshot",
            str(kit.get("text") or ""),
            KIT_BUDGET,
            "kit excerpt truncated",
        ),
    ]
    if maker_result is not None:
        evidence = {
            key: maker_result.get(key)
            for key in ("decision", "ui_contract", "tests", "ui_page")
            if key in maker_result
        }
        parts += [
            "The UI page maker's result (decision, tests and its migrate-ui-page evidence):",
            _fenced(
                fence,
                "ui page maker result",
                _compact(evidence),
                EVIDENCE_BUDGET,
                "maker result truncated",
            ),
        ]
    from_sha, to_sha = str(drift.get("from_sha"))[:40], str(drift.get("to_sha"))[:40]
    parts.append(
        f"Legacy changes since the blueprint ({from_sha}..{to_sha}; full diff sha256 "
        f"{str(drift.get('diff_sha256'))[:64]}, `git diff {from_sha} {to_sha} -- <file>` in "
        "the source worktree shows any part cut below):"
    )
    label = "legacy changes since the blueprint"
    used = len(("\n".join(parts) + "\n\n").encode("utf-8"))
    room = INPUT_BUDGET - used - len(fence(label, "").encode("utf-8"))
    parts.append(
        _fenced(
            fence,
            label,
            str(drift.get("text") or ""),
            max(0, min(DRIFT_BUDGET, room)),
            "legacy diff truncated",
        )
    )
    section = "\n".join(parts) + "\n"
    if len(section.encode("utf-8")) > INPUT_BUDGET:
        raise UiPageRejected("UI_PAGE_INPUTS_TOO_LARGE", f"{len(section.encode())} bytes")
    return section


# --------------------------------------------------------------------------------------
# Contracts: the skills' released outputs -> the delivery maker result / checker verdict
# --------------------------------------------------------------------------------------

_HERMES = "Hermes (NEW-227): "


def _binding(value: Mapping[str, Any], ui_page: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(ui_page, Mapping):
        return ["the delivery carries no UI page context"]
    problems = []
    if value.get("page_id") != ui_page.get("page_id"):
        problems.append(f"page_id {value.get('page_id')} is not {ui_page.get('page_id')}")
    return problems


def maker_result(value: Mapping[str, Any], ui_page: Mapping[str, Any] | None) -> dict[str, Any]:
    """migrate-ui-page output (already valid against its released schema) as the delivery
    maker result: COMPLETE only with spec_deviations and i18n.missing_keys empty,
    kit.explicit_props true, every API call identical and the output bound to this page
    (page_id, blueprint_sha256, locales es and en, the three viewports); otherwise BLOCKED.
    The skill's typed evidence is kept under ``ui_page`` for the checker and the record."""
    binding = _binding(value, ui_page)
    if isinstance(ui_page, Mapping) and value.get("blueprint_sha256") != ui_page.get(
        "blueprint_sha256"
    ):
        binding.append("blueprint_sha256 is not the delivered page's blueprint")
    decision = str(value.get("decision"))
    violations = list(binding)
    if decision == "COMPLETE":
        kit = value.get("kit") or {}
        i18n = value.get("i18n") or {}
        deviations = list(value.get("spec_deviations") or [])
        if deviations:
            violations.append(f"{len(deviations)} spec deviations: " + "; ".join(deviations)[:2000])
        if i18n.get("missing_keys"):
            violations.append(f"{len(i18n['missing_keys'])} i18n keys are missing")
        if kit.get("explicit_props") is not True:
            violations.append("kit.explicit_props is not true")
        different = [
            str(call.get("action"))
            for call in value.get("api_calls") or []
            if call.get("identical") is not True
        ]
        if different:
            violations.append("API calls not identical: " + ", ".join(different)[:1000])
        locales = list(i18n.get("locales") or [])
        if len(locales) != 2 or set(locales) != LOCALES:
            violations.append("i18n.locales is not es and en once each")
        viewports = [item.get("viewport") for item in value.get("viewports") or []]
        if len(viewports) != 3 or set(viewports) != VIEWPORTS:
            violations.append("viewports are not mobile, tablet and desktop once each")
    mapped = "BLOCKED" if binding or (decision == "COMPLETE" and violations) else decision
    questions = [str(item) for item in value.get("open_questions") or []]
    if violations and mapped != decision:
        questions.append(
            f"{_HERMES}migrate-ui-page decision {decision} refused: " + "; ".join(violations)
        )
    return {
        "decision": mapped,
        "summary": str(value.get("summary") or ""),
        "dependency_resolution": list(value.get("dependency_resolution") or []),
        "changed_files": list(value.get("changed_files") or []),
        "tests": list(value.get("tests") or []),
        "open_questions": [item[:4000] for item in questions][:100],
        "ui_contract": {
            "skill": "migrate-ui-page",
            "skill_decision": decision,
            "violations": [item[:4000] for item in violations][:20],
        },
        "ui_page": {
            key: value.get(key)
            for key in (
                "page_id",
                "blueprint_sha256",
                "api_calls",
                "kit",
                "i18n",
                "states",
                "viewports",
                "test_skips",
                "spec_deviations",
            )
        },
    }


def _finding_text(item: Mapping[str, Any], *, blocking: bool) -> str:
    where = item.get("location") or "no location"
    if item.get("viewport"):
        where = f"{where}, {item['viewport']}"
    refs = ", ".join(str(ref) for ref in item.get("evidence_refs") or [])
    text = (
        f"{item.get('id')} {item.get('type')} ({item.get('severity')}"
        f"{', blocking' if blocking else ''}): {item.get('title')}. {item.get('detail')} "
        f"[{where}; evidence: {refs}]"
    )
    return text[:4000]


def checker_result(value: Mapping[str, Any], ui_page: Mapping[str, Any] | None) -> dict[str, Any]:
    """review-ui-page output (already valid against its released schema) as the delivery
    checker verdict. PASS only when the review is bound to this page in the delivery
    context, reports each of the seven hard rules and eleven checks exactly once, all PASS
    or NOT_APPLICABLE, and no finding blocks (a BUG_NEW_UI always blocks). Otherwise a FAIL
    or a blocking finding is REQUEST_CHANGES, anything else BLOCKED; never upgraded.

    ``findings`` holds what the next maker round must fix (failed hard rules and checks,
    blocking findings); every typed finding is kept under ``ui_page_review``."""
    binding = _binding(value, ui_page)
    if value.get("review_context") != "delivery":
        binding.append(f"review_context {value.get('review_context')} is not delivery")
    decision = str(value.get("decision"))
    hard = [item for item in value.get("hard_rules") or [] if isinstance(item, dict)]
    checks = [item for item in value.get("checks") or [] if isinstance(item, dict)]
    findings = [item for item in value.get("findings") or [] if isinstance(item, dict)]
    incomplete = []
    for label, entries, key, expected in (
        ("hard rules", hard, "rule", HARD_RULES),
        ("checks", checks, "check", CHECKS),
    ):
        names = [str(entry.get(key)) for entry in entries]
        if sorted(names) != sorted(expected):
            incomplete.append(f"{label} are not each reported exactly once")
    failed = [
        f"{label} {entry.get(key)}: FAIL (evidence: "
        + ", ".join(str(ref) for ref in entry.get("evidence_refs") or [])
        + ")"
        for label, entries, key in (("Hard rule", hard, "rule"), ("Check", checks, "check"))
        for entry in entries
        if entry.get("status") == "FAIL"
    ]
    not_evaluated = [
        str(entry.get("check")) for entry in checks if entry.get("status") == "NOT_EVALUATED"
    ]
    blocking = [
        item
        for item in findings
        if item.get("blocking") is True or item.get("type") == "BUG_NEW_UI"
    ]
    problems = [*binding, *incomplete, *failed]
    if not_evaluated:
        problems.append("checks not evaluated: " + ", ".join(not_evaluated))
    if blocking:
        problems.append(f"{len(blocking)} blocking findings")
    mapped = decision
    if binding:
        mapped = "BLOCKED"
    elif decision == "PASS" and problems:
        mapped = "REQUEST_CHANGES" if failed or blocking else "BLOCKED"
    questions = [str(item) for item in value.get("open_questions") or []]
    if mapped != decision:
        questions.append(
            f"{_HERMES}review-ui-page decision {decision} refused: " + "; ".join(problems)
        )
    return {
        "decision": mapped,
        "summary": str(value.get("summary") or ""),
        "findings": [
            *(item[:4000] for item in failed),
            *(_finding_text(item, blocking=True) for item in blocking),
        ][:500],
        "evidence": list(value.get("evidence") or []),
        "open_questions": [item[:4000] for item in questions][:100],
        "ui_contract": {
            "skill": "review-ui-page",
            "skill_decision": decision,
            "violations": [item[:4000] for item in problems][:20],
        },
        "ui_page_review": {
            "page_id": value.get("page_id"),
            "review_context": value.get("review_context"),
            "hard_rules": hard,
            "checks": checks,
            "findings": findings,
        },
    }
