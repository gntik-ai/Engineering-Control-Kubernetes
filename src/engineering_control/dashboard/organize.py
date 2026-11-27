"""Organize and Coverage: the ground they stand on (O0 of
docs/design/organize-and-coverage.md).

Reviews becomes two pages of one shape. Both start the same kind of run, which goes
through the same stages and leaves what it found in the same three lists:

- **Needs you**: the cases in doubt, the most doubtful first.
- **Done**: what was linked or corrected, each with its reason. It can be undone.
- **Created**: the records the run made, each with what it was made from.

A record is *settled*, *in doubt* or *has none*; no other word is used for it.

This module holds the run and its lines as records, says of every check Reviews has
today which run and stage it becomes, and counts what a first run would meet in a
project. It asks no model and changes nothing of the plan: the engines are put behind it
phase by phase.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import doc_parts, reorg
from engineering_control.dashboard.connection_plan import Across
from engineering_control.dashboard.connection_records import catalog
from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.planning import PlanningRecords
from engineering_control.dashboard.planning_health import _rows
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.workspace_issues import planning_kind
from engineering_control.db.models import (
    BacklogItem,
    ConnectionFinding,
    OrganizeLine,
    OrganizeRun,
    PlanningLink,
    ProjectConnection,
    ProjectRecord,
    StoryDelivery,
    WorkspaceAction,
    WorkspaceSource,
)

# What a run organizes, and what the page calls it.
KINDS: Final = {
    "stories_issues": "User Stories and Issues",
    "epics_stories": "Epics and User Stories",
    "coverage": "Documentation coverage",
    "connection": "Connection",
}
# The stages every run goes through, in order, and what each does.
STAGES: Final = {
    "read": "Read",
    "match": "Match",
    "verify": "Verify",
    "create": "Create",
    "done": "Done",
}
LISTS: Final = {"needs_you": "Needs you", "done": "Done", "created": "Created"}
# How a record stands after a stage.
STANDS: Final = ("settled", "in_doubt", "none")
# A run that has not ended, and one that has.
LIVE: Final = ("proposed", "running", "held")
OVER: Final = ("stopped", "complete", "failed")
# For how long what a run did can be undone.
UNDO: Final = timedelta(days=30)
PAGE: Final = 50

# Every check Reviews has today, with the run it becomes part of and the stage it does
# there. The names are those the code uses: the checks that ask a model (``CHECKS`` of
# the Needs-you list), the batches of changes (``KINDS`` of the reorganization), and the
# engines behind them. ``None`` for the run means every run uses it.
BECOMES: Final[dict[str, tuple[str | None, str, str]]] = {
    # The checks that ask a model.
    "issue_story": ("stories_issues", "verify", "Which User Story an Issue belongs to"),
    "story_epic": ("epics_stories", "verify", "The one Epic of a User Story under several"),
    "epic_links": ("epics_stories", "verify", "Whether a Story's other Epics are related"),
    "story_needed": (
        "stories_issues",
        "verify",
        "Whether a User Story with no Issue and nothing done is still wanted",
    ),
    "epic_overview": ("epics_stories", "create", "Writing, and checking each sentence written"),
    # The batches of changes.
    "reasons": ("stories_issues", "done", "The reason of a link, recorded"),
    "place": ("stories_issues", "done", "An Issue put under its User Story"),
    "unsupported": ("stories_issues", "verify", "A link nothing supports, proposed off"),
    "primary": ("epics_stories", "done", "The one Epic of a User Story, chosen"),
    "extra_epics": ("epics_stories", "done", "An unrelated Epic link, taken off"),
    "close": ("stories_issues", "verify", "A delivered User Story, proposed closed"),
    "retire_stories": ("stories_issues", "verify", "A User Story nothing supports, in doubt"),
    "retire_epics": ("epics_stories", "verify", "An Epic nothing stands behind, in doubt"),
    "retire_issues": ("stories_issues", "verify", "An Issue nothing stands behind, in doubt"),
    # The engines.
    "links": (None, "read", "Links stated in a record's own text, and those you chose"),
    "ranking": (None, "match", "The closest records by meaning, by rare words, pair by pair"),
    "scorer": (None, "match", "How sure, from all signals; it abstains where it cannot say"),
    "judge": (None, "verify", "A second model of another maker, blind to the first"),
    "delivery": ("stories_issues", "read", "Whether a User Story's work is done"),
    "structure": ("epics_stories", "create", "Stories an Epic lacks; Epics to split, in doubt"),
    "review_evidence": ("stories_issues", "verify", "What the code says of an Issue's Story"),
    "planning_reviews": (None, "read", "A new record is organized when it arrives"),
    "doc_inventory": ("coverage", "read", "The documents as the repository holds them"),
    "import": ("coverage", "create", "Records written from the documentation"),
}

_KEPT: dict[str, tuple[float, dict[str, Any]]] = {}
FRESH: Final = 600.0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _on_github(db: Session, project: ProjectRecord) -> dict[str, Any]:
    """The Epics and User Stories that are issues on GitHub: an issue that calls itself
    one, by its type, a label, its title or the head of its text. Its state is the one
    the last import read."""
    repository = project.source_repository.casefold()
    # The text of each issue as it was imported, where it was: its body says more of
    # what it calls itself than the title and the labels alone.
    bodies: dict[int, dict[str, Any]] = {}
    for evidence in db.scalars(
        select(WorkspaceSource.evidence).where(
            WorkspaceSource.project_id == project.id, WorkspaceSource.kind == "issue"
        )
    ):
        number = evidence.get("number") if isinstance(evidence, dict) else None
        if isinstance(number, int):
            bodies[number] = evidence
    # An issue whose Epic or Story became a record here is not read again by the import:
    # what is known of it since is the closure the platform made on GitHub itself.
    closed_here = {
        number
        for number in db.scalars(
            select(WorkspaceAction.number).where(
                WorkspaceAction.project_id == project.id,
                WorkspaceAction.operation == "close",
                WorkspaceAction.status == "done",
                WorkspaceAction.number.is_not(None),
            )
        )
    }
    seen: set[int] = set()
    found: dict[str, dict[str, Any]] = {
        kind: {"open": 0, "closed": 0, "kept_here": 0, "open_numbers": []}
        for kind in ("epics", "stories")
    }

    def count(number: int, kind: str, state: str, here: bool) -> None:
        entry = found[kind]
        entry["open" if state == "open" else "closed"] += 1
        if here and state == "open":
            entry["kept_here"] += 1
        if state == "open" and len(entry["open_numbers"]) < 40:
            entry["open_numbers"].append(number)

    for item in db.scalars(
        select(BacklogItem).where(
            BacklogItem.project_id == project.id, BacklogItem.issue_number.is_not(None)
        )
    ):
        if (item.repository or project.source_repository).casefold() != repository:
            continue
        number = int(item.issue_number or 0)
        evidence = bodies.get(number) or {}
        kind = planning_kind(
            {
                "title": item.title,
                "labels": item.issue_labels or evidence.get("labels"),
                "body": evidence.get("body") or item.description,
                "type": evidence.get("type"),
                "issue_type": evidence.get("issue_type"),
            }
        )
        seen.add(number)
        if kind:
            # ``planning`` marks the issue whose Epic or Story is a record here already.
            state = "closed" if number in closed_here else item.issue_state or "open"
            count(number, kind, state, item.workspace_review == "planning")
    for number, evidence in bodies.items():
        if number in seen:
            continue
        kind = planning_kind(evidence)
        if kind:
            state = "closed" if number in closed_here else str(evidence.get("state") or "open")
            count(number, kind, state, False)
    closing = dict(
        db.execute(
            select(WorkspaceAction.status, func.count())
            .where(WorkspaceAction.project_id == project.id, WorkspaceAction.operation == "close")
            .group_by(WorkspaceAction.status)
        )
        .tuples()
        .all()
    )
    return {
        **found,
        "open": found["epics"]["open"] + found["stories"]["open"],
        # Closures of such issues that were approved earlier and how they stand.
        "closures": {str(status): int(number) for status, number in closing.items()},
    }


def _count(planning: PlanningRecords, db: Session, name: str) -> dict[str, Any]:
    project = active_project(db, name)
    epics = _rows(planning, name, "epics", state="open")
    stories = _rows(planning, name, "stories", state="open")
    listed = {
        state: _rows(planning, name, "issues", issue_state=state) for state in ("open", "draft")
    }
    open_epics = {str(row["id"]) for row in epics}
    # A bug stands apart from the plan: it has no Story and is left out.
    bugs = [row for rows in listed.values() for row in rows if row.get("story_optional")]
    issues = [row for rows in listed.values() for row in rows if not row.get("story_optional")]
    open_stories = {str(row["id"]) for row in stories}

    # Epics and User Stories: a Story is settled under exactly one open Epic.
    under: dict[str, int] = dict.fromkeys(open_epics, 0)
    one = several = no_epic = closed_epic_only = 0
    for row in stories:
        linked = [str(key) for key in row.get("epic_ids") or []]
        live = [key for key in linked if key in open_epics]
        for key in live:
            under[key] += 1
        if len(live) == 1:
            one += 1
        elif len(live) > 1:
            several += 1
        elif linked:
            closed_epic_only += 1
        else:
            no_epic += 1
    # Of the Stories under several Epics: those whose one Epic the administrator chose.
    chosen = set(
        db.scalars(
            select(PlanningLink.child_id).where(
                PlanningLink.project_id == project.id,
                PlanningLink.kind == "story_epic",
                PlanningLink.removed_at.is_(None),
                PlanningLink.is_primary,
                PlanningLink.primary_by == "administrator",
            )
        )
    )
    under_several = {
        UUID(str(row["id"]))
        for row in stories
        if len([key for key in row.get("epic_ids") or [] if str(key) in open_epics]) > 1
    }
    several_chosen = len(under_several & chosen)
    # And those for which one is only suggested, by how alike the texts are.
    suggested = set(
        db.scalars(
            select(PlanningLink.child_id).where(
                PlanningLink.project_id == project.id,
                PlanningLink.kind == "story_epic",
                PlanningLink.removed_at.is_(None),
                PlanningLink.is_primary,
                PlanningLink.primary_by == "suggested",
            )
        )
    )
    several_suggested = len((under_several & suggested) - chosen)
    largest = max(under.values(), default=0)

    # User Stories and Issues: an Issue is settled under an open Story.
    with_story = no_story = closed_story_only = several_stories = elsewhere = 0
    has_open_issue: set[str] = set()
    for row in issues:
        linked = [str(key) for key in row.get("story_ids") or []]
        live = [key for key in linked if key in open_stories]
        has_open_issue.update(live)
        if live:
            with_story += 1
            several_stories += len(live) > 1
        elif linked:
            closed_story_only += 1
        elif not row.get("orphan"):
            # Its Story is a record of another project.
            elsewhere += 1
        else:
            no_story += 1
    standing = dict(
        db.execute(
            select(StoryDelivery.story_id, StoryDelivery.state).where(
                StoryDelivery.project_id == project.id
            )
        )
        .tuples()
        .all()
    )
    no_issue: dict[str, int] = {}
    closed_issues_only = 0
    for row in stories:
        if str(row["id"]) in has_open_issue:
            continue
        if int(row.get("issue_count") or 0):
            closed_issues_only += 1
            continue
        state = standing.get(UUID(str(row["id"])), "unknown")
        no_issue[state] = no_issue.get(state, 0) + 1
    known_bugs = set(db.scalars(bugs_with_optional_stories(db, project.id)))
    # Open issues of the repository that were never taken into the plan: no bug, and
    # none that calls itself an Epic or a User Story. A run has to take them in first.
    outside = 0
    for item in db.scalars(
        select(BacklogItem).where(
            BacklogItem.project_id == project.id,
            BacklogItem.issue_number.is_not(None),
            BacklogItem.issue_state == "open",
            BacklogItem.workspace_review.is_(None),
        )
    ):
        if item.id in known_bugs:
            continue
        if planning_kind(
            {"title": item.title, "labels": item.issue_labels, "body": item.description}
        ):
            continue
        outside += 1
    return {
        "project": project.name,
        "epics_stories": {
            "epics": len(epics),
            "stories": len(stories),
            # Settled: under exactly one open Epic.
            "stories_under_one_epic": one,
            "stories_under_several_epics": several,
            "stories_under_several_epics_chosen": several_chosen,
            "stories_under_several_epics_suggested": several_suggested,
            "stories_with_no_epic": no_epic,
            "stories_under_closed_epics_only": closed_epic_only,
            "epics_with_no_story": sum(1 for count in under.values() if not count),
            "largest_epic_stories": largest,
            "largest_epic_share": round(largest / len(stories), 3) if stories else 0.0,
        },
        "stories_issues": {
            "issues": len(issues),
            # Kept here and not on GitHub yet.
            "issues_not_on_github": sum(
                1 for row in listed["draft"] if not row.get("story_optional")
            ),
            "issues_under_a_story": with_story,
            "issues_under_several_stories": several_stories,
            "issues_with_no_story": no_story,
            "issues_under_closed_stories_only": closed_story_only,
            "issues_under_a_story_of_another_project": elsewhere,
            "bugs_left_out": len(bugs),
            "bugs_in_all": len(known_bugs),
            "issues_outside_the_plan": outside,
            "stories": len(stories),
            "stories_with_open_issues": len(has_open_issue),
            "stories_with_closed_issues_only": closed_issues_only,
            "stories_with_no_issue": sum(no_issue.values()),
            # Of those with no Issue at all: where the evidence says each stands.
            "stories_with_no_issue_by_standing": dict(sorted(no_issue.items())),
        },
        # The projects connected to it, which every run reads as part of it.
        "scope": _connected(db, project),
        "github": _on_github(db, project),
        "documentation": doc_parts.survey(db, project),
    }


def scope(db: Session, project: UUID) -> list[UUID]:
    """The projects a run of this project reads: itself first, then the projects it is
    connected to. Direct connections only: a project connected to a connected project
    is not read."""
    found = [project]
    for first, second in db.execute(
        select(ProjectConnection.first_id, ProjectConnection.second_id)
        .where(
            (ProjectConnection.first_id == project) | (ProjectConnection.second_id == project),
            ProjectConnection.archived_at.is_(None),
        )
        .order_by(ProjectConnection.created_at, ProjectConnection.id)
    ):
        other = second if first == project else first
        peer = db.get(ProjectRecord, other)
        if peer is not None and peer.archived_at is None and other not in found:
            found.append(other)
    return found


PARENT: Final = {"issues": "stories", "stories": "epics"}


def _connected(db: Session, project: ProjectRecord) -> list[dict[str, Any]]:
    """The projects a run of this project reads beside it, each with what it brings:
    its records, and the links across the two that were approved or still wait."""
    found = []
    for c in db.scalars(
        select(ProjectConnection)
        .where(
            (ProjectConnection.first_id == project.id)
            | (ProjectConnection.second_id == project.id),
            ProjectConnection.archived_at.is_(None),
        )
        .order_by(ProjectConnection.created_at, ProjectConnection.id)
    ):
        peer = db.get(ProjectRecord, c.second_id if c.first_id == project.id else c.first_id)
        if peer is None or peer.archived_at is not None:
            continue
        links = dict(
            db.execute(
                select(ConnectionFinding.status, func.count())
                .where(
                    ConnectionFinding.connection_id == c.id,
                    ConnectionFinding.action == "link",
                    ConnectionFinding.status.in_(("approved", "pending")),
                )
                .group_by(ConnectionFinding.status)
            )
            .tuples()
            .all()
        )
        found.append(
            {
                "project": peer.name,
                "label": peer.display_name,
                "connection": str(c.id),
                "relation": "related"
                if not c.dependent_id
                else "depends_on"
                if c.dependent_id == project.id
                else "used_by",
                "records": (c.progress or {}).get("readiness", {}).get(peer.name, {}),
                "links": {
                    "approved": int(links.get("approved", 0)),
                    "pending": int(links.get("pending", 0)),
                },
            }
        )
    return found


def candidates(
    sessions: sessionmaker[Session], name: str, record: UUID, limit: int = 10
) -> dict[str, Any]:
    """The parents a record could be put under, drawn from the whole scope: for an
    Issue the User Stories, for a User Story the Epics, of the project and of every
    project connected to it, the nearest first. Each says which project it belongs to.
    By meaning where the ranking service read both projects with the same model, and by
    the words they share."""
    with sessions() as db:
        project = active_project(db, name)
        mine = catalog(db, project)
        item = mine.local.get(record)
        if item is None or item.kind not in PARENT:
            raise ProjectNotFound(str(record))
        kind = PARENT[item.kind]
        found: list[dict[str, Any]] = []
        for key in scope(db, project.id):
            other = db.get(ProjectRecord, key)
            if other is None:
                continue
            theirs = mine if key == project.id else catalog(db, other)
            across = Across.read(db, [mine, theirs])
            # A record that is not approved yet is no part of the pair: it is compared
            # by its words alone.
            for position, (parent, close, shared) in enumerate(across.ranked(0, item, kind)):
                if parent.id == item.id:
                    continue
                found.append(
                    {
                        "project": other.name,
                        "label": other.display_name,
                        "own": key == project.id,
                        "kind": kind,
                        "id": str(parent.id) if parent.id else None,
                        "title": parent.title,
                        "closeness": round(close, 3) if close is not None else None,
                        "shared": shared,
                        "place": position + 1,
                    }
                )
        # The closest by meaning first; what has no vector follows by its place in its
        # own project's ranking.
        found.sort(
            key=lambda entry: (
                entry["closeness"] is None,
                -(entry["closeness"] or 0.0),
                entry["place"],
                entry["project"],
            )
        )
        return {
            "project": project.name,
            "record": {"id": str(item.id), "kind": item.kind, "title": item.title},
            "scope": [entry["project"] for entry in _connected(db, project)],
            "candidates": found[:limit],
        }


def ground(sessions: sessionmaker[Session], name: str, *, fresh: bool = False) -> dict[str, Any]:
    """What a first run would meet in the project: how its Epics, User Stories and Issues
    stand to each other, which of its Epics and Stories are issues on GitHub, and how its
    documentation cuts into parts. Counted from the records; no model is asked."""
    key = f"{id(sessions)}:{name}"
    kept = _KEPT.get(key)
    now = time.monotonic()
    if kept and not fresh and now - kept[0] < FRESH:
        return kept[1]
    planning = PlanningRecords(sessions)
    with sessions() as db:
        counted = _count(planning, db, name)
    counted["measured_at"] = datetime.now(UTC).isoformat()
    counted["becomes"] = [
        {
            "check": check,
            "run": run,
            "stage": stage,
            "says": says,
            "batch": check in reorg.KINDS,
        }
        for check, (run, stage, says) in BECOMES.items()
    ]
    _KEPT[key] = (now, counted)
    return counted


def forget(name: str) -> None:
    """What was counted for the project is counted anew at the next asking."""
    for key in [key for key in _KEPT if key.endswith(":" + name)]:
        _KEPT.pop(key, None)


def can_undo(line: OrganizeLine, now: datetime) -> bool:
    """Whether the line changed the plan and that can still be put back."""
    since = line.answered_at or line.created_at
    return line.listed != "needs_you" and line.state == "applied" and now - _aware(since) < UNDO


def shown_line(line: OrganizeLine, now: datetime) -> dict[str, Any]:
    return {
        "id": str(line.id),
        "run": str(line.run_id),
        "list": line.listed,
        "case": line.case,
        "subject": {
            "kind": line.subject_kind,
            "id": str(line.subject_id) if line.subject_id else None,
            "project": str(line.subject_project) if line.subject_project else None,
            "ref": line.subject_ref,
            "title": line.title,
        },
        "candidates": line.candidates or [],
        "said": line.said or [],
        "reason": line.reason,
        "doubt": line.doubt,
        "action": line.action,
        "made": (
            {"kind": line.made_kind, "id": str(line.made_id), "sources": line.sources or []}
            if line.made_id
            else None
        ),
        "state": line.state,
        "answer": line.answer,
        "answered_at": line.answered_at.isoformat() if line.answered_at else None,
        "undone_at": line.undone_at.isoformat() if line.undone_at else None,
        # A line that changed the plan can be undone for a while after it was written.
        "can_undo": can_undo(line, now),
    }


def shown_run(run: OrganizeRun, lists: dict[str, int]) -> dict[str, Any]:
    counts = run.counts or {}
    reached = list(STAGES).index(run.stage) if run.stage in STAGES else 0
    return {
        "id": str(run.id),
        "kind": run.kind,
        "connection": str(run.connection_id) if run.connection_id else None,
        "scope": list(run.scope or []),
        "name": KINDS.get(run.kind, run.kind),
        "state": run.state,
        "stage": run.stage,
        "stages": [
            {
                "stage": stage,
                "name": title,
                "reached": index <= reached and run.state != "proposed",
                **{key: int((counts.get(stage) or {}).get(key, 0)) for key in ("records", *STANDS)},
            }
            for index, (stage, title) in enumerate(STAGES.items())
        ],
        "estimate": run.estimate or {},
        "dollar_cap": run.dollar_cap,
        "spent": run.spent,
        "cost": run.cost,
        "detail": run.detail,
        "auto": run.auto,
        "lists": {name: int(lists.get(name, 0)) for name in LISTS},
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "undone_at": run.undone_at.isoformat() if run.undone_at else None,
    }


# -- what the runs behind the buttons of Organize share ------------------------------------


def runs_of(db: Session, project: UUID, kind: str) -> list[OrganizeRun]:
    """The project's runs of that kind, the newest first."""
    return list(
        db.scalars(
            select(OrganizeRun)
            .where(
                OrganizeRun.project_id == project,
                OrganizeRun.kind == kind,
                OrganizeRun.connection_id.is_(None),
            )
            .order_by(OrganizeRun.created_at.desc(), OrganizeRun.id)
        )
    )


def lines_where(project: UUID, kind: str) -> Any:
    """The lines of every run of that kind in the project."""
    return OrganizeLine.run_id.in_(
        select(OrganizeRun.id).where(
            OrganizeRun.project_id == project,
            OrganizeRun.kind == kind,
            OrganizeRun.connection_id.is_(None),
        )
    )


def list_counts(db: Session, project: UUID, kind: str) -> dict[str, int]:
    """How many lines each list holds: what still waits, and what was done and created
    and still stands."""
    found = dict.fromkeys(LISTS, 0)
    for listed, state, number in db.execute(
        select(OrganizeLine.listed, OrganizeLine.state, func.count())
        .where(lines_where(project, kind))
        .group_by(OrganizeLine.listed, OrganizeLine.state)
    ):
        if (listed == "needs_you" and state == "open") or (
            listed != "needs_you" and state == "applied"
        ):
            found[listed] = found.get(listed, 0) + int(number)
    return found


def shown_stages(run: OrganizeRun) -> list[dict[str, Any]]:
    """The stages of a run, each with whether it was reached and its counts."""
    counts = run.counts or {}
    reached = list(STAGES).index(run.stage) if run.stage in STAGES else 0
    return [
        {
            "stage": stage,
            "name": title,
            "reached": index <= reached,
            **{key: int((counts.get(stage) or {}).get(key, 0)) for key in ("records", *STANDS)},
        }
        for index, (stage, title) in enumerate(STAGES.items())
    ]


def list_page(
    db: Session, project: UUID, kind: str, listed: str, page: int, page_size: int
) -> tuple[int, list[OrganizeLine], int, int]:
    """A page of one list over every run of that kind: what still waits, the most
    doubtful first; what was done and still stands, the newest first."""
    if listed not in LISTS:
        raise ProjectInvalid("That is no list of a run")
    page, page_size = max(1, page), min(100, max(1, page_size))
    where = [
        lines_where(project, kind),
        OrganizeLine.listed == listed,
        OrganizeLine.state == ("open" if listed == "needs_you" else "applied"),
    ]
    total = int(db.scalar(select(func.count()).select_from(OrganizeLine).where(*where)) or 0)
    order = (
        (OrganizeLine.doubt.desc(), OrganizeLine.created_at, OrganizeLine.id)
        if listed == "needs_you"
        else (OrganizeLine.answered_at.desc(), OrganizeLine.id)
    )
    rows = list(
        db.scalars(
            select(OrganizeLine)
            .where(*where)
            .order_by(*order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return total, rows, page, max(1, -(-total // page_size))


def own_line(db: Session, project: UUID, kind: str, key: UUID) -> OrganizeLine:
    """The line, where it is one of that kind of run in the project."""
    line = db.get(OrganizeLine, key)
    run = db.get(OrganizeRun, line.run_id) if line else None
    if line is None or run is None or run.project_id != project or run.kind != kind:
        raise ProjectNotFound(str(key))
    return line


class Organize:
    """The runs of Organize and Coverage, and what each found."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def ground(self, name: str, *, fresh: bool = False) -> dict[str, Any]:
        return ground(self.sessions, name, fresh=fresh)

    def candidates(self, name: str, record: UUID) -> dict[str, Any]:
        return candidates(self.sessions, name, record)

    # -- a run, from the word that proposes it to its end ---------------------------

    def propose(
        self,
        name: str,
        kind: str,
        *,
        estimate: dict[str, Any] | None = None,
        dollar_cap: float | None = None,
        auto: bool = False,
        connection: UUID | None = None,
    ) -> UUID:
        """A run that says what it would ask and cost, and waits for the word. One run
        of a kind at a time in a project, and one at a time for a connection: a second
        is refused while the first is live."""
        if kind not in KINDS or (kind == "connection") != (connection is not None):
            raise ProjectInvalid("Choose what to organize")
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            live = db.scalar(
                select(OrganizeRun.id).where(
                    OrganizeRun.project_id == project.id,
                    OrganizeRun.kind == kind,
                    OrganizeRun.connection_id == connection
                    if connection
                    else OrganizeRun.connection_id.is_(None),
                    OrganizeRun.state.in_(LIVE),
                )
            )
            if live is not None:
                raise ProjectConflict("A run of this kind is under way in this project")
            run = OrganizeRun(
                project_id=project.id,
                kind=kind,
                connection_id=connection,
                scope=[str(key) for key in scope(db, project.id)],
                state="proposed",
                stage="read",
                counts={},
                estimate=estimate or {},
                dollar_cap=dollar_cap,
                auto=auto,
            )
            db.add(run)
            db.flush()
            return run.id

    def _run(self, db: Session, name: str, key: UUID) -> OrganizeRun:
        project = active_project(db, name, lock=True)
        run = db.get(OrganizeRun, key)
        if run is None or run.project_id != project.id:
            raise ProjectNotFound(str(key))
        return run

    def start(self, name: str, key: UUID, *, now: datetime | None = None) -> None:
        """The administrator's word: the run begins."""
        with self.sessions.begin() as db:
            run = self._run(db, name, key)
            if run.state not in ("proposed", "held"):
                raise ProjectConflict("This run is not waiting to start")
            run.state = "running"
            run.started_at = run.started_at or (now or datetime.now(UTC))
            run.detail = ""

    def enter(self, name: str, key: UUID, stage: str, counts: dict[str, int]) -> None:
        """The run is in that stage, with how many records it took in and how it left
        them. A stage is never gone back to."""
        if stage not in STAGES or set(counts) - {"records", *STANDS}:
            raise ProjectInvalid("That is no stage of a run")
        with self.sessions.begin() as db:
            run = self._run(db, name, key)
            if run.state != "running":
                raise ProjectConflict("This run is not running")
            order = list(STAGES)
            if order.index(stage) < order.index(run.stage):
                raise ProjectConflict("A run does not go back to a stage it left")
            run.stage = stage
            run.counts = {**(run.counts or {}), stage: {key: int(n) for key, n in counts.items()}}

    def used(self, name: str, key: UUID, *, tokens: int, cost: float) -> bool:
        """What asking cost so far. False when the run reached the dollars it stops at:
        it is held then, with what it has, for the administrator to raise the limit."""
        with self.sessions.begin() as db:
            run = self._run(db, name, key)
            run.spent += max(0, int(tokens))
            run.cost = round(run.cost + max(0.0, float(cost)), 6)
            if run.dollar_cap is not None and run.cost >= run.dollar_cap and run.state == "running":
                run.state = "held"
                run.detail = f"It reached the {run.dollar_cap:.2f} US dollars it may cost."
                return False
            return True

    def end(
        self,
        name: str,
        key: UUID,
        state: str,
        detail: str = "",
        *,
        now: datetime | None = None,
    ) -> None:
        """The run is over: complete, stopped by the administrator, or failed."""
        if state not in OVER:
            raise ProjectInvalid("That is no way for a run to end")
        with self.sessions.begin() as db:
            run = self._run(db, name, key)
            if run.state in OVER:
                raise ProjectConflict("This run is over")
            if state == "complete":
                run.stage = "done"
            run.state, run.detail = state, detail[:2000]
            run.ended_at = now or datetime.now(UTC)

    # -- what a run found -----------------------------------------------------------

    def add(self, name: str, key: UUID, listed: str, **line: Any) -> UUID:
        """One result of the run, in one of its three lists. A case in doubt waits for
        the administrator; a link that was made and a record that was created are
        written when they were applied."""
        if listed not in LISTS:
            raise ProjectInvalid("That is no list of a run")
        with self.sessions.begin() as db:
            run = self._run(db, name, key)
            row = OrganizeLine(
                run_id=run.id,
                project_id=run.project_id,
                listed=listed,
                state="open" if listed == "needs_you" else "applied",
                **{
                    "candidates": [],
                    "said": [],
                    "reason": "",
                    "doubt": 0.0,
                    "title": "",
                    **line,
                },
            )
            row.title = row.title[:300]
            row.doubt = min(1.0, max(0.0, float(row.doubt)))
            db.add(row)
            db.flush()
            return row.id

    def overview(self, name: str, kind: str) -> dict[str, Any]:
        """The run of that kind that is live, or the last one that ended, with how many
        lines each of its lists holds; and the runs before it."""
        if kind not in KINDS:
            raise ProjectInvalid("Choose what to organize")
        with self.sessions() as db:
            project = active_project(db, name)
            runs = list(
                db.scalars(
                    select(OrganizeRun)
                    .where(OrganizeRun.project_id == project.id, OrganizeRun.kind == kind)
                    .order_by(OrganizeRun.created_at.desc(), OrganizeRun.id)
                    .limit(20)
                )
            )
            lists: dict[UUID, dict[str, int]] = {}
            if runs:
                for run_id, listed, state, number in db.execute(
                    select(
                        OrganizeLine.run_id, OrganizeLine.listed, OrganizeLine.state, func.count()
                    )
                    .where(OrganizeLine.run_id.in_([run.id for run in runs]))
                    .group_by(OrganizeLine.run_id, OrganizeLine.listed, OrganizeLine.state)
                ):
                    # What needs the administrator is what is still open; what was done
                    # and created is what still stands.
                    counts = (listed == "needs_you" and state == "open") or (
                        listed != "needs_you" and state == "applied"
                    )
                    if counts:
                        entry = lists.setdefault(run_id, {})
                        entry[listed] = entry.get(listed, 0) + int(number)
            shown = [shown_run(run, lists.get(run.id, {})) for run in runs]
            return {
                "project": project.name,
                "kind": kind,
                "name": KINDS[kind],
                "stages": [{"stage": stage, "name": title} for stage, title in STAGES.items()],
                "run": shown[0] if shown else None,
                "before": shown[1:],
                "undo_days": UNDO.days,
            }

    def lines(
        self,
        name: str,
        key: UUID,
        listed: str,
        page: int = 1,
        page_size: int = PAGE,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """One list of a run, a page of it. "Needs you" shows what still waits, the most
        doubtful first; the other two show the newest first."""
        if listed not in LISTS:
            raise ProjectInvalid("That is no list of a run")
        now = _aware(now or datetime.now(UTC))
        page, page_size = max(1, page), min(200, max(1, page_size))
        with self.sessions() as db:
            project = active_project(db, name)
            run = db.get(OrganizeRun, key)
            if run is None or run.project_id != project.id:
                raise ProjectNotFound(str(key))
            where = [OrganizeLine.run_id == run.id, OrganizeLine.listed == listed]
            if listed == "needs_you":
                where.append(OrganizeLine.state == "open")
            total = int(
                db.scalar(select(func.count()).select_from(OrganizeLine).where(*where)) or 0
            )
            order = (
                (OrganizeLine.doubt.desc(), OrganizeLine.created_at, OrganizeLine.id)
                if listed == "needs_you"
                else (OrganizeLine.created_at.desc(), OrganizeLine.id)
            )
            rows = db.scalars(
                select(OrganizeLine)
                .where(*where)
                .order_by(*order)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {
                "run": str(run.id),
                "list": listed,
                "name": LISTS[listed],
                "total": total,
                "page": page,
                "pages": max(1, -(-total // page_size)),
                "items": [shown_line(row, now) for row in rows],
            }
