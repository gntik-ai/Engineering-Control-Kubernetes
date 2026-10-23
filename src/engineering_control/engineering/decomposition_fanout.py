"""NEW-234 PR6: the backlog decomposition fan-out contract (pure and deterministic).

Modelled on the UI blueprint fan-out (NEW-123/NEW-146, engineering.ui_blueprint): after the
operator approves a decomposition plan, every task becomes one change request under the
deterministic id ``uuid5("hermes-backlog-decomposition:{plan_sha}:{key}")``, its proposal
rendered from the plan (no model drafts it) and its body bound to a DecompositionChangeContext
(story, epic, plan, predecessor tasks and, once published, their issues). Here: the task
order (predecessors first), the identities, the intake statement, the import risk of the
real rendered body, the ready-set binding of a bulk approval, and the story amendments the
operator publishes (Hermes renders the new body and its diff; it never edits an issue).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import NAMESPACE_URL, UUID, uuid5

from engineering_control.engineering.backlog import (
    CHANGE_REQUEST_LABEL,
    calculated_issue_risk,
    canonical_sha256,
)
from engineering_control.engineering.change_requests import (
    DESCRIPTION_MAX_BYTES,
    DecompositionChangeContext,
    decomposition_lineage,
)
from engineering_control.engineering.decomposition import (
    PROJECT,
    SOURCE_REPOSITORY,
    resolved_dependencies,
)

FANOUT_BATCH = 10
FANOUT_MAX_ITEMS_DEFAULT = 250
FANOUT_MAX_ITEMS_LIMIT = 400
FANOUT_FAILURES_SHOWN = 10
RETRY_FANOUT_PHRASE: Final = "RETRY_DECOMPOSITION_FANOUT"
FANOUT_WORKFLOW_PREFIX: Final = "backlog-decomposition-fanout-"
FANOUT_RUN_TYPE: Final = "backlog-decomposition-fanout"
READY_BINDING_KIND: Final = "hermes-backlog-decomposition-ready-v1"
# Dedupe (design section 4): an existing request of the same lineage in one of these states
# supersedes the task (SKIPPED) unless --replace; a published one always does.
SUPERSEDED_BY_EXISTING: Final = "SUPERSEDED_BY_EXISTING"
LINEAGE_BLOCKING_STATUSES = frozenset({"DRAFTING", "CANDIDATE", "APPROVED", "PUBLISHED"})
# A candidate whose body still lacks a published predecessor's issue (never approvable).
DEPENDENCIES_AWAITING: Final = "DECOMPOSITION_DEPENDENCIES_AWAITING"
DEPENDENCIES_STALE: Final = "DECOMPOSITION_DEPENDENCIES_STALE"
# The import reads an issue body excerpt of this many characters (BacklogIssue.body_excerpt).
IMPORT_BODY_EXCERPT = 6000
AMENDMENT_MARKER_PREFIX: Final = "<!-- hermes-decomposition-amendment"
_RISK_ORDER = ("R0", "R1", "R2", "R3")
_PLAN_KEY = re.compile(r"^falcone-serverless:([A-Z]{2,4}-[0-9]{2})\.(t[1-9][0-9]?)$")


def fanout_workflow_id(decomposition_id: str, sequence: int) -> str:
    """One workflow id per fan-out execution (a Run binds to exactly one execution)."""
    if not 1 <= sequence <= 999_999:
        raise ValueError("decomposition fan-out sequence is out of range")
    return f"{FANOUT_WORKFLOW_PREFIX}{decomposition_id}-{sequence}"


def task_change_request_id(plan_sha256: str, key: str) -> UUID:
    """The deterministic change-request id of one task (idempotent, resumable)."""
    return uuid5(NAMESPACE_URL, f"hermes-backlog-decomposition:{plan_sha256}:{key}")


def short_key(key: str) -> str:
    """`falcone-serverless:API-01.t2` -> `API-01.t2` (the change-request context form)."""
    match = _PLAN_KEY.fullmatch(key)
    if match is None:
        raise ValueError(f"decomposition task key {key!r} is invalid")
    return f"{match.group(1)}.{match.group(2)}"


def lineage_of(project: str, task_key: str) -> str:
    """The dedupe identity `project:story:key` of a (short) task key across plans."""
    return f"{project}:{task_key.split('.', 1)[0]}:{task_key}"


@dataclass(frozen=True)
class FanoutTask:
    key: str
    story_id: str
    story_issue: int
    epic_issue: int
    task: Mapping[str, Any]
    # The predecessor task keys with every `contract:<ctr-id>` bound to its provider tasks
    # (decomposition.resolved_dependencies); what the change requests link.
    dependencies: tuple[str, ...] = ()

    @property
    def repository(self) -> str:
        return str(self.task["target"]["repository"])

    @property
    def short_key(self) -> str:
        return short_key(self.key)

    @property
    def depends_on(self) -> tuple[str, ...]:
        return tuple(short_key(item) for item in self.dependencies)


def fanout_tasks(plan: Mapping[str, Any]) -> list[FanoutTask]:
    """Every task of the plan, predecessors first (plan order breaks ties); a contract
    dependency counts as its provider tasks."""
    stories = list(plan.get("stories") or [])
    bound = resolved_dependencies(stories)
    tasks = [
        FanoutTask(
            key=str(task["key"]),
            story_id=str(story["story_id"]),
            story_issue=int(story["story_issue"]),
            epic_issue=int(story["epic_issue"]),
            task=task,
            dependencies=bound.get(str(task["key"]), ()),
        )
        for story in stories
        for task in story.get("tasks") or []
    ]
    by_key = {task.key: task for task in tasks}
    if len(by_key) != len(tasks):
        raise ValueError("the decomposition plan repeats a task key")
    ordered: list[FanoutTask] = []
    placed: set[str] = set()
    remaining = list(tasks)
    while remaining:
        progressed = False
        for task in list(remaining):
            waiting = [item for item in task.dependencies if item in by_key and item not in placed]
            if not waiting:
                ordered.append(task)
                placed.add(task.key)
                remaining.remove(task)
                progressed = True
                break
        if not progressed:
            raise ValueError("the decomposition plan task graph has a cycle")
    return ordered


def task_context(
    task: FanoutTask, *, decomposition_id: str, plan_sha256: str, depends_on: Sequence[str] = ()
) -> DecompositionChangeContext:
    return DecompositionChangeContext(
        decomposition_id=decomposition_id,
        plan_sha256=plan_sha256,
        task_key=task.short_key,
        story_key=task.story_id,
        story_issue=task.story_issue,
        epic_issue=task.epic_issue,
        depends_on_tasks=task.depends_on,
        depends_on=tuple(depends_on),
    )


def task_lineage(task: FanoutTask, context: DecompositionChangeContext) -> str:
    lineage = decomposition_lineage(PROJECT, context)
    if lineage != lineage_of(PROJECT, task.short_key):
        raise ValueError("decomposition lineage does not match its task")
    return lineage


def _joined(values: Sequence[Any], limit: int = 12) -> str:
    items = [" ".join(str(value).split()) for value in values[:limit]]
    if len(values) > limit:
        items.append(f"and {len(values) - limit} more")
    return "; ".join(items) if items else "none"


def task_description(task: FanoutTask, *, decomposition_id: str) -> str:
    """The bounded task statement (intake description): what the proposal does not carry --
    the plan fields Codex reviews against and the implementer works from."""
    value = task.task
    target = value["target"]
    interfaces = value.get("interfaces") or {}
    covers = sorted(
        {str(item) for criterion in value["acceptance_criteria"] for item in criterion["covers"]}
    )
    lines = [
        f"Backlog decomposition {decomposition_id}, task {task.key} of story {task.story_id} "
        f"({SOURCE_REPOSITORY}#{task.story_issue}).",
        f"Target: {target['repository']} ({target['role']}), component {target['component']}; "
        f"size {value['size']}, about {value['estimated_files']} files.",
        f"Covers: {_joined(covers)}; requirements: "
        f"{_joined(list(value.get('requirements') or []))}.",
        f"Interfaces provided: {_joined(list(interfaces.get('provides') or []))}; consumed: "
        f"{_joined(list(interfaces.get('consumes') or []))}.",
        f"Paths: {_joined(list(value.get('paths') or []))}.",
        f"Test commands: {_joined(list(value.get('test_commands') or []))}.",
        f"Predecessor tasks: {_joined(list(task.dependencies))}.",
        f"Sources: {_joined(list(value.get('sources') or []))}.",
    ]
    text = "\n".join(lines)
    while len(text.encode("utf-8")) > DESCRIPTION_MAX_BYTES and len(lines) > 2:
        lines.pop()
        text = "\n".join(lines)
    return text


def import_risk(
    repository: str, kind: str, proposed_risk: str, title: str, body: str
) -> dict[str, Any]:
    """The risk the backlog import will calculate for the real rendered issue (its labels,
    title and body excerpt, including the declared Risk floor). Never rewords the body."""
    calculated = calculated_issue_risk(
        repository, (CHANGE_REQUEST_LABEL, kind), title, body[:IMPORT_BODY_EXCERPT]
    )
    return {
        "proposed_risk": proposed_risk,
        "calculated_risk": calculated,
        "escalated": _RISK_ORDER.index(calculated) > _RISK_ORDER.index(proposed_risk),
    }


def ready_binding(
    *,
    decomposition_id: str,
    plan_sha256: str,
    decomposition_binding: str,
    items: Sequence[tuple[str, str]],
) -> str:
    """The typed confirmation of one bulk approval: the approved plan and exactly these
    requests with exactly their current bindings (a re-render or a new ready request
    changes it)."""
    return canonical_sha256(
        {
            "kind": READY_BINDING_KIND,
            "decomposition_id": decomposition_id,
            "plan_sha256": plan_sha256,
            "decomposition_binding": decomposition_binding,
            "requests": [[identity, binding] for identity, binding in items],
        }
    )


# --------------------------------------------------------------------------------------
# Story amendments (operator-published edits; Hermes never edits an issue)
# --------------------------------------------------------------------------------------

_SECTION_TITLES = {
    "acceptance_criteria": "Acceptance criteria",
    "requirements": "Requirements",
    "dependencies": "Dependencies",
    "scope": "Scope",
    "notes": "Notes",
}
_CRITERION_LINE = re.compile(r"^(\s*[-*]\s+(?:\[[ xX]\]\s+)?)(\S.*?)\s*$")
_DEPENDS_LINE = re.compile(r"^(\s*[-*]?\s*Depends on:)\s*(.*?)\s*$", re.IGNORECASE)
_NO_DEPENDENCY = frozenset({"", "none", "-", "n/a"})
_LIST_LINE = re.compile(r"^\s*[-*]\s+\S")


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def _normalized(body: str) -> str:
    return body.replace("\r\n", "\n").strip()


def _section_lines(lines: list[str], heading: str) -> list[int]:
    indexes: list[int] = []
    inside = False
    for index, line in enumerate(lines):
        if line.startswith("## "):
            inside = line[3:].strip().lower() == heading
            continue
        if inside:
            indexes.append(index)
    return indexes


@dataclass
class _Edits:
    replace: dict[int, str]
    remove: set[int]
    after: dict[int, list[str]]


def _criterion_edits(
    lines: list[str], story_id: str, changes: Sequence[Mapping[str, Any]], edits: _Edits
) -> list[Mapping[str, Any]]:
    section = _section_lines(lines, "acceptance criteria")
    criteria = [index for index in section if _CRITERION_LINE.match(lines[index])]
    unapplied: list[Mapping[str, Any]] = []
    for change in changes:
        target, text = change["target"], change["text"]
        match = re.fullmatch(rf"{re.escape(story_id)}#ac([1-9][0-9]?)", str(target or ""))
        if change["action"] == "ADD":
            if not criteria:
                unapplied.append(change)
                continue
            edits.after.setdefault(criteria[-1], []).append(f"- [ ] {_one_line(text)}")
            continue
        if match is None or int(match.group(1)) > len(criteria):
            unapplied.append(change)
            continue
        index = criteria[int(match.group(1)) - 1]
        if change["action"] == "REMOVE":
            edits.remove.add(index)
        else:
            prefix = _CRITERION_LINE.match(lines[index])
            edits.replace[index] = f"{prefix.group(1) if prefix else '- [ ] '}{_one_line(text)}"
    return unapplied


def _requirement_edits(
    lines: list[str], changes: Sequence[Mapping[str, Any]], edits: _Edits
) -> list[Mapping[str, Any]]:
    section = _section_lines(lines, "requirements")
    listed = [index for index in section if _LIST_LINE.match(lines[index])]
    unapplied: list[Mapping[str, Any]] = []
    for change in changes:
        target, text = str(change["target"] or ""), change["text"]
        if change["action"] == "ADD":
            if not listed:
                unapplied.append(change)
                continue
            edits.after.setdefault(listed[-1], []).append(f"- **{target}:** {_one_line(text)}")
            continue
        token = re.compile(rf"(?<![A-Za-z0-9.-]){re.escape(target)}(?![A-Za-z0-9-])")
        found = [index for index in section if token.search(lines[index])]
        if len(found) != 1:
            unapplied.append(change)
            continue
        index = found[0]
        if change["action"] == "REMOVE":
            edits.remove.add(index)
            continue
        prefix = re.match(rf"^(.*?{token.pattern}[*:\s]*)", lines[index])
        edits.replace[index] = f"{prefix.group(1) if prefix else ''}{_one_line(text)}"
    return unapplied


def _dependency_edits(
    lines: list[str], changes: Sequence[Mapping[str, Any]], edits: _Edits
) -> list[Mapping[str, Any]]:
    found = [index for index, line in enumerate(lines) if _DEPENDS_LINE.match(line)]
    if len(found) != 1:
        return list(changes)
    index = found[0]
    match = _DEPENDS_LINE.match(lines[index])
    assert match is not None  # noqa: S101 - found above
    items = [
        item.strip()
        for item in match.group(2).split(",")
        if item.strip().lower() not in _NO_DEPENDENCY
    ]
    unapplied: list[Mapping[str, Any]] = []
    for change in changes:
        target = str(change["target"] or "")
        if change["action"] == "ADD":
            if target not in items:
                items.append(target)
        elif target not in items:
            unapplied.append(change)
        elif change["action"] == "REMOVE":
            items.remove(target)
        else:
            replacement = _one_line(change["text"])
            items[items.index(target)] = replacement
            items = list(dict.fromkeys(items))
    edits.replace[index] = f"{match.group(1)} {', '.join(items) if items else 'None'}"
    return unapplied


def amendment_marker(*, decomposition_id: str, story_id: str) -> str:
    return f"{AMENDMENT_MARKER_PREFIX} plan={decomposition_id} story={PROJECT}:{story_id} -->"


def amended_story_body(
    body: str,
    story_id: str,
    amendment: Mapping[str, Any],
    *,
    decomposition_id: str,
    plan_sha256: str,
) -> tuple[str, list[dict[str, Any]]]:
    """The story issue body with the amendment applied (deterministic), and the changes
    that could not be placed in the body's own sections (kept, verbatim, in the appended
    amendment section for the operator to place by hand).

    Criterion ids are positional (`<STORY>#acN` is the Nth criterion of the bound body):
    every target is resolved against the bound body before any change is applied. Scope
    and notes changes are listed in the appended section."""
    lines = _normalized(body).split("\n")
    edits = _Edits(replace={}, remove=set(), after={})
    changes = list(amendment.get("changes") or [])
    by_section: dict[str, list[Mapping[str, Any]]] = {}
    for change in changes:
        by_section.setdefault(str(change["section"]), []).append(change)
    unapplied = [
        *_criterion_edits(lines, story_id, by_section.get("acceptance_criteria", []), edits),
        *_requirement_edits(lines, by_section.get("requirements", []), edits),
        *(
            _dependency_edits(lines, by_section["dependencies"], edits)
            if by_section.get("dependencies")
            else []
        ),
    ]
    amended: list[str] = []
    for index, line in enumerate(lines):
        if index not in edits.remove:
            amended.append(edits.replace.get(index, line))
        amended.extend(edits.after.get(index, []))
    listed = [
        change
        for change in changes
        if change["section"] in {"scope", "notes"} or any(change is item for item in unapplied)
    ]
    footer = [
        "",
        "## Decomposition amendment",
        "",
        f"- Reason: {_one_line(amendment.get('reason') or '')}",
        f"- Sources: {', '.join(_one_line(item) for item in amendment.get('sources') or [])}",
        f"- Plan: `{decomposition_id}` (sha256 `{plan_sha256}`)",
    ]
    for change in listed:
        target = f" {_one_line(change['target'])}" if change.get("target") else ""
        text = _one_line(change["text"]) if change.get("text") else "(removed)"
        footer.append(
            f"- {_SECTION_TITLES[str(change['section'])]} ({change['action']}{target}): {text}"
        )
    footer += ["", amendment_marker(decomposition_id=decomposition_id, story_id=story_id)]
    rendered = "\n".join([*amended, *footer]) + "\n"
    return rendered, [
        {key: change[key] for key in ("section", "action", "target")}
        for change in unapplied
        if change["section"] not in {"scope", "notes"}
    ]


def amendment_diff(current: str, amended: str, *, repository: str, issue: int) -> str:
    return "".join(
        line + "\n"
        for line in difflib.unified_diff(
            _normalized(current).split("\n"),
            amended.rstrip("\n").split("\n"),
            fromfile=f"{repository}#{issue} (current)",
            tofile=f"{repository}#{issue} (amended)",
            lineterm="",
        )
    )


def amendment_command(repository: str, issue: int, body_file: str) -> list[str]:
    """The operator's own gh call that publishes one amended story body (never Hermes's)."""
    return ["gh", "api", "-X", "PATCH", f"repos/{repository}/issues/{issue}", "-F",
            f"body=@{body_file}"]  # fmt: skip
