"""The Bugs section: where a project's bugs are reported, triaged and managed on their own.

A bug stands apart from the plan (``bugs.py``, ``issue_types.py``): it belongs to no User
Story and to no Epic. This is its home. It is read from the project's issues and not from
the plan, so a project that was never through Discovery has it too.

- **Reporting.** One step: the administrator writes what happens, what was expected and
  how to reproduce it, and the issue is created on GitHub with the ``bug`` label, and its
  severity where one is given, in the same request. While the title is typed, the open
  bugs that share the most words with it are shown, so that the same bug is not reported
  twice. A bug opened directly on GitHub arrives with the next reading of the repository.
- **Triage.** A bug is *new* until the administrator gives it a severity; then it is
  *confirmed*. Closed, it is *fixed*, a *duplicate* of a bug that is named, or *not going to
  be fixed*. That state is worked out from the issue and from what was decided here, never
  kept beside them, so it cannot differ from GitHub. A bug also carries the area of the
  code it is in and where it was seen. Severity is mirrored to GitHub as a label.
- **Alike.** The ranking service reads every bug, as its own kind. An open bug is shown
  with the open bugs that lie closest to it in meaning, as possible duplicates. No
  language model is asked, and nothing is closed by likeness: the administrator closes a
  bug as the duplicate of another, and GitHub gets a comment that names it.
- **Several at once.** Severity and area can be given to several bugs together, and
  several can be closed together.

- **Fixing.** A fix is started from a confirmed bug, or the confirmed bugs are put in a
  queue, the most severe first. The bug goes to the section Bugs of the engine and that
  section's flow runs: "Fix a bug" writes a test that fails because of the bug, fixes
  the bug, and publishes the change once the administrator agrees. While a run is under
  way or its pull request is open the bug is *in progress*. The pull request resolves
  the bug's issue: merged, by the administrator on GitHub, it closes the issue, and the
  bug is *fixed*, by that pull request. A bug no test could show says so, with what the
  run said.

GitHub stays where a bug lives. Every change here is written there first and kept here as
GitHub answered it. A text that was changed on GitHub since it was read here is read
again instead of being overwritten.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import numpy
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import ranking
from engineering_control.dashboard.backlog_items import clean_description, clean_title
from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.planning import PlanningRecords
from engineering_control.dashboard.planning_lifecycle import ARCHIVED_DRAFT, CLOSED_DRAFT
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.review_graph import terms
from engineering_control.dashboard.workspace_publishing import (
    WorkspacePublishing,
    confirm_classification,
    enqueue,
)
from engineering_control.db.models import (
    BacklogItem,
    BugRecord,
    ProjectIssueStory,
    ProjectRecord,
    ProjectSection,
    RecordVector,
    SandboxRun,
    WorkQueue,
    WorkQueueEntry,
    WorkspaceAction,
    WorkspaceSource,
)
from engineering_control.engine.library import document_in_force, plan_for_item
from engineering_control.flow_library import WORK_KINDS
from engineering_control.github_app import GitHubApp, GitHubAppError

# What a report is made of, in the order and under the headings its issue carries.
PARTS: Final = (
    ("happens", "What happens"),
    ("expected", "What was expected"),
    ("steps", "How to reproduce"),
    ("seen_in", "Where it was seen"),
)
# How closing a bug is said here, and what GitHub is told.
REASONS: Final = {"fixed": "completed", "wont_fix": "not_planned", "duplicate": "not_planned"}
# The severities, the worst first, each with the colour its label gets on GitHub.
SEVERITIES: Final = {
    "critical": "b60205",
    "high": "d93f0b",
    "medium": "fbca04",
    "low": "c2e0c6",
}
# Where a bug stands, as the section says it.
STATES: Final = {
    "new": "New",
    "confirmed": "Confirmed",
    "in_progress": "In progress",
    "fixed": "Fixed",
    "duplicate": "Duplicate",
    "wont_fix": "Not going to be fixed",
    "closed": "Closed",
}
# What the platform leaves in an issue's text to know it again; never shown or edited.
MARKER: Final = re.compile(r"<!--\s*engineering-control:[^>]*-->")
AREA: Final = re.compile(r"^(?:area|component)\s*[:/]\s*(.+)$", re.I)
SEVERITY: Final = re.compile(r"^severity\s*[:/]\s*(critical|high|medium|low)$", re.I)
LISTED: Final = 5000
# How close two bugs lie in meaning before one is shown as possibly the same as the
# other, and how much of a new title's words an open bug must share to be shown.
ALIKE: Final = 0.9
SHARED: Final = 0.5
SHOWN_ALIKE: Final = 3
SEVERAL: Final = 100
# How many bugs a queue of fixes is made of at most: what a queue holds.
QUEUED: Final = 200


def severity_label(severity: str) -> str:
    return f"severity: {severity}"


def body_of(parts: dict[str, str]) -> str:
    """The text of a reported bug: each part that was filled in, under its heading."""
    said = [
        f"### {heading}\n\n{parts[key].strip()}"
        for key, heading in PARTS
        if (parts.get(key) or "").strip()
    ]
    return "\n\n".join(said)


def shown_text(text: str | None) -> str:
    """An issue's text without the platform's own marks."""
    return MARKER.sub("", text or "").strip()


def _same(one: str | None, other: str | None) -> bool:
    return shown_text(one).split() == shown_text(other).split()


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _labels(item: BacklogItem) -> list[str]:
    found = []
    for entry in item.issue_labels if isinstance(item.issue_labels, list) else []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name.strip():
            found.append(name.strip())
    return found


def _stands(item: BacklogItem) -> str:
    """Where a bug's issue stands: open, closed, being created on GitHub, or a draft that
    was never published."""
    if item.issue_number is None:
        if item.workspace_review == "publishing":
            return "creating"
        return "closed" if item.workspace_review == CLOSED_DRAFT else "draft"
    return "closed" if item.issue_state == "closed" else "open"


def severity_of(item: BacklogItem, record: BugRecord | None) -> str | None:
    """The bug's severity: what was decided here, else what a label on GitHub says."""
    if record is not None and record.severity in SEVERITIES:
        return record.severity
    for label in _labels(item):
        said = SEVERITY.match(label)
        if said:
            return said.group(1).lower()
    return None


def area_of(item: BacklogItem, record: BugRecord | None) -> str | None:
    """The area of the code the bug is in: what was decided here, else an area label."""
    if record is not None and record.area:
        return record.area
    for label in _labels(item):
        said = AREA.match(label)
        if said:
            return said.group(1).strip()[:60] or None
    return None


def triage_of(
    item: BacklogItem, record: BugRecord | None, fix: dict[str, Any] | None = None
) -> str:
    """Where the bug stands, from its issue, what was decided here, and the fix that is
    under way."""
    if _stands(item) == "closed":
        if record is not None and record.duplicate_of is not None:
            return "duplicate"
        reason = (item.facts or {}).get("github_state_reason")
        return {"completed": "fixed", "not_planned": "wont_fix"}.get(str(reason), "closed")
    if fix is not None and fix["in_progress"]:
        return "in_progress"
    return "confirmed" if record is not None and record.confirmed_at is not None else "new"


# The section of the engine bugs are fixed in, and the flow it has unless another is
# assigned.
SECTION: Final = "bugs"
# A run that is not over: waiting to start, at work, or waiting for the administrator.
ACTIVE: Final = ("queued", "running", "waiting")


def _pull_request(item: BacklogItem) -> dict[str, Any] | None:
    """The pull request a run opened for the bug, as the run noted it on the item."""
    pull = (item.facts or {}).get("pull_request")
    if not isinstance(pull, dict) or not isinstance(pull.get("number"), int):
        return None
    return {
        "number": pull["number"],
        "url": pull.get("url"),
        "merged": bool(pull.get("merged")),
    }


def fix_of(item: BacklogItem, run: SandboxRun | None) -> dict[str, Any]:
    """Where fixing the bug stands: its latest run, the pull request a run opened, and
    what follows from them, said in a sentence. Nothing is kept for it: it is read from
    the run and from the item."""
    pull = _pull_request(item)
    data = (run.data or {}) if run is not None else {}
    shown = (data.get("bug") or {}).get("shown")
    under_way = run is not None and run.state in ACTIVE
    opened_here = bool(data.get("pull_request"))
    # A bug no test could show: the check said so, or the agent that was to write the
    # test gave up before any check ran.
    not_reproduced = (
        run is not None
        and not under_way
        and not opened_here
        and (shown is False or (shown is None and run.outcome == "blocked"))
    )
    open_pull = pull is not None and not pull["merged"] and _stands(item) == "open"
    # The pull request resolves the bug's issue: GitHub closes the issue, as completed,
    # when it is merged, whoever merges it.
    fixed = (
        _stands(item) == "closed" and (item.facts or {}).get("github_state_reason") == "completed"
    )
    detail = " ".join(str(run.detail or "").split())[:600] if run is not None else ""
    if under_way and run is not None and run.state == "waiting":
        says = "A fix waits for you" + (f": {detail}" if detail else ".")
    elif under_way:
        says = "A fix is being worked on."
    elif pull is not None and (pull["merged"] or fixed):
        says = f"Fixed by pull request #{pull['number']}."
    elif open_pull and pull is not None:
        says = f"Pull request #{pull['number']} is open: the bug is fixed when it is merged."
    elif _stands(item) == "closed":
        # Closed some other way: what a run once did says nothing of it any more.
        says = ""
    elif not_reproduced:
        says = "No test could show the bug" + (f": {detail}" if detail else ".")
    elif run is not None and run.state == "failed":
        says = "The last fix did not finish" + (f": {detail}" if detail else ".")
    elif run is not None and run.state == "cancelled":
        says = "The last fix was stopped."
    elif run is not None:
        says = "The last fix ended without a change" + (f": {detail}" if detail else ".")
    else:
        says = ""
    return {
        "run": {
            "id": str(run.id),
            "state": run.state,
            "outcome": run.outcome,
            "detail": detail or None,
            "flow": str((data.get("flow") or {}).get("name") or ""),
            "started_at": _iso(run.created_at),
            "ended_at": _iso(run.finished_at),
        }
        if run is not None
        else None,
        "under_way": under_way,
        "waits": bool(under_way and run is not None and run.state == "waiting"),
        "pull_request": pull,
        "not_reproduced": not_reproduced,
        "in_progress": bool(under_way or open_pull),
        "says": says,
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _runs(db: Session, project: ProjectRecord) -> dict[UUID, SandboxRun]:
    """The latest run of work on each item of the project."""
    found: dict[UUID, SandboxRun] = {}
    for run in db.scalars(
        select(SandboxRun)
        .where(
            SandboxRun.project_id == project.id,
            SandboxRun.item_id.is_not(None),
            SandboxRun.kind.in_(WORK_KINDS),
        )
        .order_by(SandboxRun.created_at, SandboxRun.id)
    ):
        if run.item_id is not None:
            found[run.item_id] = run
    return found


def why_no_fix(item: BacklogItem, record: BugRecord | None, fix: dict[str, Any]) -> str | None:
    """Why a fix of the bug cannot be started now; None when it can."""
    if item.issue_number is None or _stands(item) != "open":
        return "A fix starts from a bug whose issue is open on GitHub."
    if fix["under_way"]:
        return "A fix of this bug is under way."
    if record is None or record.confirmed_at is None:
        return "Give the bug a severity first: a fix starts from a confirmed bug."
    return None


def _bugs(db: Session, project: ProjectRecord) -> list[BacklogItem]:
    """Every bug of the project, whatever its plan knows of it."""
    return list(
        db.scalars(
            select(BacklogItem)
            .where(
                BacklogItem.project_id == project.id,
                BacklogItem.id.in_(bugs_with_optional_stories(db, project.id)),
                # An issue that is itself an Epic or a User Story is no bug, and a draft
                # that was deleted is gone.
                BacklogItem.workspace_review.is_distinct_from("planning"),
                BacklogItem.workspace_review.is_distinct_from(ARCHIVED_DRAFT),
            )
            .limit(LISTED)
        )
    )


def _records(db: Session, project: ProjectRecord) -> dict[UUID, BugRecord]:
    return {
        record.item_id: record
        for record in db.scalars(select(BugRecord).where(BugRecord.project_id == project.id))
    }


def _record(db: Session, project: ProjectRecord, item: UUID) -> BugRecord:
    """What was decided about the bug, made where nothing was yet."""
    record = db.scalar(
        select(BugRecord).where(BugRecord.project_id == project.id, BugRecord.item_id == item)
    )
    if record is None:
        record = BugRecord(project_id=project.id, item_id=item)
        db.add(record)
        db.flush()
    return record


def _opened(db: Session, project: ProjectRecord) -> dict[int, Any]:
    """When each issue was opened on GitHub, as the reading of the repository's history
    kept it: for the issues whose own record does not say."""
    found: dict[int, Any] = {}
    for key, evidence in db.execute(
        select(WorkspaceSource.key, WorkspaceSource.evidence).where(
            WorkspaceSource.project_id == project.id, WorkspaceSource.key.like("issue:%")
        )
    ):
        number = key.removeprefix("issue:")
        if number.isdigit() and (evidence or {}).get("created_at"):
            found[int(number)] = evidence["created_at"]
    return found


def _reported(item: BacklogItem, opened: dict[int, Any] | None = None) -> datetime:
    """When the bug was reported: the day its issue was opened on GitHub, and only where
    nothing says so the day it was first read here."""
    said = _when((item.facts or {}).get("github_created_at"))
    if said is None and item.issue_number is not None:
        said = _when((opened or {}).get(item.issue_number))
    return said or _when(item.created_at) or datetime.now(UTC)


def _brief(item: BacklogItem) -> dict[str, Any]:
    return {"id": str(item.id), "number": item.issue_number, "title": item.title}


def alike_by_words(title: str, text: str, bugs: list[BacklogItem]) -> list[tuple[float, UUID]]:
    """The bugs that have at least half of the given words, and two of them at the least,
    each with the share it has. Those that share the rarer words come first: a word that
    few bugs use says more than one that many use."""
    wanted = set(terms({"title": title, "body": text}))
    if not wanted or not bugs:
        return []
    theirs = {
        bug.id: set(terms({"title": bug.title, "body": bug.description or ""})) for bug in bugs
    }
    spread: Counter[str] = Counter(word for words in theirs.values() for word in words)

    def weight(word: str) -> float:
        return math.log1p((len(bugs) - spread[word] + 0.5) / (spread[word] + 0.5)) + 0.1

    found = []
    for key, words in theirs.items():
        shared = wanted & words
        share = len(shared) / len(wanted)
        if share >= SHARED and len(shared) >= min(2, len(wanted)):
            found.append((share, sum(weight(word) for word in shared), key))
    found.sort(key=lambda entry: (-entry[1], -entry[0], str(entry[2])))
    return [(share, key) for share, _weight, key in found]


def alike_by_meaning(
    db: Session, project: ProjectRecord, bugs: list[BacklogItem]
) -> dict[UUID, list[tuple[float, UUID]]]:
    """For each of the bugs, the others that lie closest to it in meaning, as the ranking
    service read them: possible duplicates, the closest first."""
    embedding = ranking.stored_model(db, RecordVector, project.id)
    if not embedding or len(bugs) < 2:
        return {}
    vectors = ranking.vectors(db, project.id, embedding)
    keys = [bug.id for bug in bugs if ("bug", bug.id) in vectors]
    if len(keys) < 2:
        return {}
    table = numpy.stack([vectors["bug", key] for key in keys])
    near = table @ table.T
    found: dict[UUID, list[tuple[float, UUID]]] = {}
    for place, key in enumerate(keys):
        close = [
            (float(near[place][other]), keys[other])
            for other in numpy.argsort(-near[place])
            if other != place and float(near[place][other]) >= ALIKE
        ][:SHOWN_ALIKE]
        if close:
            found[key] = close
    return found


def _row(
    item: BacklogItem,
    record: BugRecord | None,
    problems: dict[UUID, str],
    opened: dict[int, Any] | None,
    known: dict[UUID, BacklogItem],
    alike: list[tuple[float, UUID]],
    run: SandboxRun | None = None,
) -> dict[str, Any]:
    stands = _stands(item)
    updated = _when(item.issue_updated_at)
    closed = _when(item.github_closed_at)
    fix = fix_of(item, run)
    triage = triage_of(item, record, fix)
    other = known.get(record.duplicate_of) if record and record.duplicate_of else None
    return {
        "id": str(item.id),
        "number": item.issue_number,
        "url": item.issue_url,
        "title": item.title,
        "text": shown_text(item.description),
        "state": stands,
        "triage": triage,
        "says": STATES[triage],
        "severity": severity_of(item, record),
        "area": area_of(item, record),
        "seen_in": record.seen_in if record else None,
        "duplicate_of": _brief(other) if other is not None else None,
        "alike": [
            {**_brief(known[key]), "likeness": round(likeness, 3)}
            for likeness, key in alike
            if key in known
        ],
        "confirmed": bool(item.type_confirmed and item.execution_type == "bug"),
        "labels": _labels(item),
        "reported_at": _reported(item, opened).isoformat(),
        "updated_at": updated.isoformat() if updated else None,
        "closed_at": closed.isoformat() if closed else None,
        "problem": problems.get(item.id),
        # Where fixing it stands, and why a fix cannot be started now, when it cannot.
        "fix": {**fix, "refused": why_no_fix(item, record, fix)},
    }


def _engine(db: Session, project: ProjectRecord) -> dict[str, Any]:
    """The section of the engine the project's bugs are fixed in, the flow it has, and
    why no fix can be started, when none can."""
    section = db.scalar(
        select(ProjectSection).where(
            ProjectSection.project_id == project.id, ProjectSection.key == SECTION
        )
    )
    plan = (
        plan_for_item(db, section.flow_key, produced=False, version=section.flow_version)
        if section is not None and section.flow_key
        else None
    )
    found = document_in_force(db, plan[0]) if plan is not None else None
    why = None
    if not project.source_repository:
        why = "Connect a repository to the project first."
    elif section is None:
        why = "This project has no section Bugs in its engine."
    elif not section.flow_key:
        why = (
            "The section Bugs has no flow. Assign “Fix a bug” to it under Engine, Flow assignment."
        )
    elif found is None:
        why = (
            "The flow of the section Bugs cannot run here. Assign “Fix a bug” to it "
            "under Engine, Flow assignment."
        )
    return {
        "section": section.name if section is not None else None,
        "flow": {"key": found[0].key, "name": found[0].name} if found is not None else None,
        "refused": why,
    }


class BugSection:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        planning: PlanningRecords,
        publishing: WorkspacePublishing,
    ) -> None:
        self.sessions = sessions
        self.planning = planning
        self.publishing = publishing

    # ------------------------------------------------------------------ reading

    @staticmethod
    def _problems(db: Session, project: ProjectRecord) -> dict[UUID, str]:
        """What GitHub last refused, for the bugs a change of which is still to be made."""
        return {
            local: detail
            for local, detail in db.execute(
                select(WorkspaceAction.local_id, WorkspaceAction.detail).where(
                    WorkspaceAction.project_id == project.id,
                    WorkspaceAction.status.not_in(("done", "retained")),
                    WorkspaceAction.detail.is_not(None),
                )
            )
        }

    def overview(self, name: str) -> dict[str, Any]:
        """How many bugs the project has in each state and, of the open ones, of each
        severity; whether one can be reported; and the areas its bugs are in."""
        with self.sessions() as db:
            project = active_project(db, name)
            found = _bugs(db, project)
            records = _records(db, project)
            runs = _runs(db, project)
            fixable = 0
            counts = {"open": 0, "closed": 0, "creating": 0, "draft": 0}
            states: Counter[str] = Counter()
            severities: Counter[str] = Counter()
            areas: Counter[str] = Counter()
            for item in found:
                record = records.get(item.id)
                stands = _stands(item)
                counts[stands] += 1
                fix = fix_of(item, runs.get(item.id))
                states[triage_of(item, record, fix)] += 1
                fixable += why_no_fix(item, record, fix) is None
                area = area_of(item, record)
                if area:
                    areas[area] += 1
                if stands != "closed":
                    severities[severity_of(item, record) or "none"] += 1
            linked = int(
                db.scalar(
                    select(func.count(func.distinct(ProjectIssueStory.item_id))).where(
                        ProjectIssueStory.project_id == project.id,
                        ProjectIssueStory.item_id.in_([item.id for item in found]),
                    )
                )
                or 0
            )
            opened = _opened(db, project)
            oldest = min(
                (_reported(item, opened) for item in found if _stands(item) == "open"),
                default=None,
            )
            return {
                "repository": project.source_repository or None,
                "can_report": bool(project.source_repository),
                "counts": counts,
                "states": [
                    {"state": state, "says": says, "count": states[state]}
                    for state, says in STATES.items()
                ],
                "severities": [
                    {"severity": severity, "open": severities[severity]}
                    for severity in (*SEVERITIES, "none")
                ],
                "areas": [area for area, _count in areas.most_common(50)],
                "linked": linked,
                "oldest_open": oldest.isoformat() if oldest else None,
                "parts": [{"key": key, "heading": heading} for key, heading in PARTS],
                # Whether a fix can be started here, by which flow, and how many
                # confirmed bugs wait for one.
                "fixing": {**_engine(db, project), "ready": fixable},
            }

    def listing(
        self,
        name: str,
        state: str,
        search: str,
        sort: str,
        page: int,
        page_size: int,
        severity: str = "",
        area: str = "",
    ) -> dict[str, Any]:
        """The project's bugs: open, closed, in one state of triage, or all; of one
        severity or area; the newest first, the oldest, the last changed, or the most
        severe. An open bug comes with the open bugs that lie closest to it in meaning."""
        wanted = " ".join(search.split()).casefold()
        with self.sessions() as db:
            project = active_project(db, name)
            problems = self._problems(db, project)
            opened = _opened(db, project)
            records = _records(db, project)
            every = _bugs(db, project)
            known = {item.id: item for item in every}
            runs = _runs(db, project)
            rows = []
            for item in every:
                record = records.get(item.id)
                # What is being created, and a draft, stand with the open ones.
                group = "closed" if _stands(item) == "closed" else "open"
                if state in ("open", "closed"):
                    if group != state:
                        continue
                elif (
                    state in STATES
                    and triage_of(item, record, fix_of(item, runs.get(item.id))) != state
                ):
                    continue
                mine = severity_of(item, record)
                if severity and (mine or "none") != severity:
                    continue
                if area and (area_of(item, record) or "").casefold() != area.casefold():
                    continue
                if wanted and not (
                    wanted in item.title.casefold()
                    or wanted.lstrip("#") == str(item.issue_number or "")
                ):
                    continue
                rows.append(item)
            if sort == "updated":
                rows.sort(
                    key=lambda item: _when(item.issue_updated_at) or _reported(item, opened),
                    reverse=True,
                )
            elif sort == "severity":
                order = {key: place for place, key in enumerate(SEVERITIES)}
                rows.sort(
                    key=lambda item: (
                        order.get(severity_of(item, records.get(item.id)) or "", len(order)),
                        _reported(item, opened),
                    )
                )
            else:
                rows.sort(key=lambda item: _reported(item, opened), reverse=sort != "oldest")
            pages = max(1, (len(rows) + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            shown = rows[(page - 1) * page_size : page * page_size]
            alike = alike_by_meaning(
                db, project, [item for item in every if _stands(item) == "open"]
            )
            return {
                "items": [
                    _row(
                        item,
                        records.get(item.id),
                        problems,
                        opened,
                        known,
                        alike.get(item.id, []),
                        runs.get(item.id),
                    )
                    for item in shown
                ],
                "total": len(rows),
                "page": page,
                "pages": pages,
            }

    def similar(self, name: str, title: str, text: str) -> dict[str, Any]:
        """The open bugs that share the most words with a bug as it is being written."""
        with self.sessions() as db:
            project = active_project(db, name)
            open_bugs = [item for item in _bugs(db, project) if _stands(item) != "closed"]
            known = {item.id: item for item in open_bugs}
            return {
                "items": [
                    {**_brief(known[key]), "url": known[key].issue_url, "shared": round(share, 2)}
                    for share, key in alike_by_words(title, text, open_bugs)[:5]
                ]
            }

    # ---------------------------------------------------------------- reporting

    def _draft(
        self, name: str, title: str, parts: dict[str, str], severity: str | None
    ) -> tuple[UUID, UUID]:
        if severity is not None and severity not in SEVERITIES:
            raise ProjectInvalid("A severity is critical, high, medium or low.")
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            if not project.source_repository:
                raise ProjectInvalid("This project has no repository to report a bug in.")
            if not (parts.get("happens") or "").strip():
                raise ProjectInvalid("Say what happens.")
            cleaned = clean_title(title)
            same = next(
                (
                    item
                    for item in _bugs(db, project)
                    if _stands(item) != "closed" and item.title.casefold() == cleaned.casefold()
                ),
                None,
            )
            if same is not None:
                known = f"#{same.issue_number}" if same.issue_number else "being created"
                raise ProjectConflict(f"A bug with this title is open already: {known}.")
            item = BacklogItem(
                project_id=project.id,
                source="manual",
                state="proposed",
                # Reporting is the administrator's word for creating it.
                workspace_review="publishing",
                title=cleaned,
                description=clean_description(body_of(parts)),
                execution_type="bug",
                type_confirmed=True,
                classification_reason="Reported as a bug by the administrator.",
            )
            db.add(item)
            db.flush()
            seen = " ".join((parts.get("seen_in") or "").split())[:200] or None
            if severity or seen:
                record = _record(db, project, item.id)
                record.seen_in = seen
                if severity:
                    record.severity, record.confirmed_at = severity, datetime.now(UTC)
            action = enqueue(db, project.id, "create", item.id, project.source_repository)
            return item.id, action.id

    async def report(
        self,
        name: str,
        title: str,
        parts: dict[str, str],
        github: GitHubApp | None,
        severity: str | None = None,
    ) -> dict[str, Any]:
        """Report a bug: its issue is created on GitHub now, with the ``bug`` label and
        the severity it was given. Where GitHub cannot be reached the bug is kept and
        created as soon as it can."""
        key, action = await asyncio.to_thread(self._draft, name, title, parts, severity)
        result = await self.publishing.sync(name, action, github)
        return {"id": str(key), **result}

    # ----------------------------------------------------------------- changing

    def _published(self, name: str, key: UUID) -> tuple[str, int, str, str, str]:
        """The bug as it is held here: repository, number, title, text and state."""
        with self.sessions() as db:
            project = active_project(db, name)
            item = db.get(BacklogItem, key)
            if (
                item is None
                or item.project_id != project.id
                or item.id not in {row.id for row in _bugs(db, project)}
            ):
                raise ProjectNotFound(str(key))
            if item.issue_number is None:
                raise ProjectConflict("This bug is not on GitHub yet.")
            return (
                item.repository or project.source_repository,
                item.issue_number,
                item.title,
                item.description or "",
                item.issue_state or "open",
            )

    def _kept(self, name: str, key: UUID, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Keep the bug as GitHub has it now."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            item = db.get(BacklogItem, key)
            if item is None or item.project_id != project.id:
                raise ProjectNotFound(str(key))
            item.title = clean_title(str(snapshot.get("title") or item.title))
            item.description = clean_description(snapshot.get("body"))
            state = "closed" if snapshot.get("state") == "closed" else "open"
            closed = _when(snapshot.get("closed_at"))
            item.issue_state = state
            item.github_closed_at = closed if state == "closed" else None
            item.issue_updated_at = _when(snapshot.get("updated_at")) or datetime.now(UTC)
            item.facts = {**(item.facts or {}), "github_state_reason": snapshot.get("state_reason")}
            record = _records(db, project).get(item.id)
            if state == "closed":
                item.state, item.done_at = "done", closed or datetime.now(UTC)
            else:
                if item.state == "done":
                    item.state, item.done_at = "proposed", None
                if record is not None:
                    # Open again: it is no longer the duplicate of another.
                    record.duplicate_of = None
            item.updated_at = datetime.now(UTC)
            db.flush()
            return _row(item, record, {}, _opened(db, project), {}, [])

    async def edit(
        self, name: str, key: UUID, title: str, text: str, github: GitHubApp | None
    ) -> dict[str, Any]:
        """Change a bug's title and text on GitHub. A text that was changed there since it
        was read here is read again, and nothing is overwritten."""
        repository, number, _title, held, _state = await asyncio.to_thread(
            self._published, name, key
        )
        cleaned = clean_title(title)
        wanted = clean_description(text) or ""
        if github is None:
            raise ProjectConflict("Configure the GitHub App to change a bug on GitHub.")
        try:
            snapshot = await github.workspace_issue(repository, number)
            current = str(snapshot.get("body") or "")
            if not _same(current, held):
                await asyncio.to_thread(self._kept, name, key, snapshot)
                raise ProjectConflict(
                    "This bug was changed on GitHub since it was read here. It is read again "
                    "now: look at it and make your change again."
                )
            marks = "\n".join(MARKER.findall(current))
            token = await github.repository_token(repository, {"issues": "write"})
            try:
                changed = await github.update_workspace_issue(
                    repository,
                    number,
                    token=token,
                    title=cleaned,
                    body=wanted + ("\n\n" + marks if marks else ""),
                )
            finally:
                await github.revoke(token)
        except GitHubAppError as error:
            raise ProjectConflict(str(error)) from error
        return await asyncio.to_thread(self._kept, name, key, changed)

    # ------------------------------------------------------------------- triage

    def _decided(
        self, name: str, key: UUID, severity: str | None, area: str | None, seen_in: str | None
    ) -> tuple[str | None, int | None, str | None]:
        """Keep what was decided about the bug. Returns where its issue is and the
        severity it has now, for GitHub to be told."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            item = db.get(BacklogItem, key)
            if (
                item is None
                or item.project_id != project.id
                or item.id not in {row.id for row in _bugs(db, project)}
            ):
                raise ProjectNotFound(str(key))
            record = _record(db, project, item.id)
            if severity is not None:
                record.severity = severity or None
                record.confirmed_at = datetime.now(UTC) if severity else None
            if area is not None:
                record.area = " ".join(area.split())[:60] or None
            if seen_in is not None:
                record.seen_in = " ".join(seen_in.split())[:200] or None
            record.updated_at = datetime.now(UTC)
            return (
                (item.repository or project.source_repository) if item.issue_number else None,
                item.issue_number,
                record.severity,
            )

    def _labelled(self, name: str, key: UUID, labels: list[str]) -> None:
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            item = db.get(BacklogItem, key)
            if item is not None and item.project_id == project.id:
                item.issue_labels = labels

    async def triage(
        self,
        name: str,
        key: UUID,
        github: GitHubApp | None,
        *,
        severity: str | None = None,
        area: str | None = None,
        seen_in: str | None = None,
    ) -> dict[str, Any]:
        """Say how severe a bug is, which area it is in and where it was seen. Giving a
        severity confirms the bug; an empty severity takes the confirmation back. The
        severity is put on the issue as a label first; where GitHub does not take it,
        nothing is changed here either. ``None`` leaves a thing as it is."""
        if severity not in (None, "", *SEVERITIES):
            raise ProjectInvalid("A severity is critical, high, medium or low.")
        repository, number = await asyncio.to_thread(self._where, name, key)
        if severity is not None and repository and number:
            if github is None:
                raise ProjectConflict("Configure the GitHub App to label a bug on GitHub.")
            try:
                token = await github.repository_token(repository, {"issues": "write"})
                try:
                    labels = await github.label_workspace_issue(
                        repository,
                        number,
                        token=token,
                        add={severity_label(severity): SEVERITIES[severity]} if severity else {},
                        remove=[severity_label(other) for other in SEVERITIES if other != severity],
                    )
                finally:
                    await github.revoke(token)
            except GitHubAppError as error:
                raise ProjectConflict(str(error)) from error
            await asyncio.to_thread(self._labelled, name, key, labels)
        await asyncio.to_thread(self._decided, name, key, severity, area, seen_in)
        return {"id": str(key), "severity": severity, "area": area}

    def _where(self, name: str, key: UUID) -> tuple[str | None, int | None]:
        """The repository and number of the bug's issue, or neither while it has none."""
        with self.sessions() as db:
            project = active_project(db, name)
            item = db.get(BacklogItem, key)
            if (
                item is None
                or item.project_id != project.id
                or item.id not in {row.id for row in _bugs(db, project)}
            ):
                raise ProjectNotFound(str(key))
            if item.issue_number is None:
                return None, None
            return item.repository or project.source_repository, item.issue_number

    async def triage_several(
        self,
        name: str,
        keys: list[UUID],
        github: GitHubApp | None,
        *,
        severity: str | None = None,
        area: str | None = None,
    ) -> dict[str, Any]:
        """The same decision for several bugs. Each is done on its own: one that GitHub
        does not take is told and the others are done."""
        done, failed = 0, []
        for key in list(dict.fromkeys(keys))[:SEVERAL]:
            try:
                await self.triage(name, key, github, severity=severity, area=area)
                done += 1
            except (ProjectConflict, ProjectInvalid, ProjectNotFound) as error:
                failed.append({"id": str(key), "detail": str(error)})
        return {"done": done, "failed": failed}

    # ------------------------------------------------------------------ closing

    def close(
        self, name: str, key: UUID, reason: str, note: str, duplicate_of: int | None = None
    ) -> dict[str, Any]:
        """Close a bug as fixed, as not going to be fixed, or as the duplicate of another
        bug, given by its number. GitHub is told the reason, and the note, where one is
        given, is left on the issue as a comment; a duplicate's comment names the other
        bug."""
        if reason not in REASONS:
            raise ProjectInvalid(
                "A bug is closed as fixed, as not going to be fixed, or as a duplicate."
            )
        other: UUID | None = None
        with self.sessions() as db:
            project = active_project(db, name)
            found = {row.id: row for row in _bugs(db, project)}
            if key not in found:
                raise ProjectNotFound(str(key))
            if reason == "duplicate":
                twin = next(
                    (
                        row
                        for row in found.values()
                        if duplicate_of is not None and row.issue_number == duplicate_of
                    ),
                    None,
                )
                if twin is None or twin.id == key:
                    raise ProjectInvalid("Name the bug it duplicates by its number.")
                other = twin.id
        said = (clean_description(note) or "")[:4000]
        if other is not None:
            said = f"Duplicate of #{duplicate_of}." + (f"\n\n{said}" if said else "")
        impact = self.planning.lifecycle.preview(name, "issues", key, "close")
        mine = next((entry for entry in impact["items"] if entry["id"] == str(key)), None)
        if mine is None or mine["action"] != "close":
            raise ProjectConflict("This bug is closed already, or its closure is under way.")
        result = self.planning.lifecycle.confirm(
            name, "issues", key, "close", impact["token"], closure=(REASONS[reason], said)
        )
        if other is not None:
            with self.sessions.begin() as db:
                project = active_project(db, name, lock=True)
                _record(db, project, key).duplicate_of = other
        return result

    def close_several(self, name: str, keys: list[UUID], reason: str, note: str) -> dict[str, Any]:
        """Close several bugs for the same reason. Each is closed on its own."""
        if reason not in ("fixed", "wont_fix"):
            raise ProjectInvalid("Several bugs are closed as fixed or as not going to be fixed.")
        done, failed = 0, []
        for key in list(dict.fromkeys(keys))[:SEVERAL]:
            try:
                self.close(name, key, reason, note)
                done += 1
            except (ProjectConflict, ProjectInvalid, ProjectNotFound) as error:
                failed.append({"id": str(key), "detail": str(error)})
        return {"done": done, "failed": failed}

    async def reopen(self, name: str, key: UUID, github: GitHubApp | None) -> dict[str, Any]:
        """Open a closed bug again, on GitHub and here."""
        repository, number, _title, _held, state = await asyncio.to_thread(
            self._published, name, key
        )
        if state != "closed":
            raise ProjectConflict("This bug is open.")
        if github is None:
            raise ProjectConflict("Configure the GitHub App to open a bug again on GitHub.")
        try:
            token = await github.repository_token(repository, {"issues": "write"})
            try:
                snapshot = await github.reopen_workspace_issue(repository, number, token=token)
            finally:
                await github.revoke(token)
        except GitHubAppError as error:
            raise ProjectConflict(str(error)) from error
        return await asyncio.to_thread(self._kept, name, key, snapshot)

    # ------------------------------------------------------------------ fixing

    @staticmethod
    def _to_section(db: Session, project: ProjectRecord, item: BacklogItem) -> None:
        """Put the bug in the section Bugs of the engine, by the administrator's word:
        what starts for it is that section's flow."""
        section = db.scalar(
            select(ProjectSection).where(
                ProjectSection.project_id == project.id, ProjectSection.key == SECTION
            )
        )
        if section is None:  # ``_engine`` said so before
            raise ProjectConflict("This project has no section Bugs in its engine.")
        if item.section_id != section.id:
            item.section_id = section.id
            item.routed_by = "administrator"
            item.routing_reason = None

    def to_fix(self, name: str, key: UUID) -> UUID:
        """Make a bug ready for its fix to be started: it is open and confirmed, no fix
        of it is under way, and it stands in the section Bugs. Returns the item a run
        is started for. Nothing is started here."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            item = db.get(BacklogItem, key)
            if (
                item is None
                or item.project_id != project.id
                or item.id not in {row.id for row in _bugs(db, project)}
            ):
                raise ProjectNotFound(str(key))
            engine = _engine(db, project)
            if engine["refused"]:
                raise ProjectConflict(str(engine["refused"]))
            record = db.scalar(
                select(BugRecord).where(
                    BugRecord.project_id == project.id, BugRecord.item_id == item.id
                )
            )
            refused = why_no_fix(item, record, fix_of(item, _runs(db, project).get(item.id)))
            if refused:
                raise ProjectConflict(refused)
            self._to_section(db, project, item)
            return item.id

    def to_queue(self, name: str) -> list[UUID]:
        """The confirmed bugs a fix can be started for, the most severe first and of one
        severity the oldest first, each put in the section Bugs: what a queue of fixes
        is made of. A bug that is in a queue under way already is left there."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            engine = _engine(db, project)
            if engine["refused"]:
                raise ProjectConflict(str(engine["refused"]))
            records = _records(db, project)
            runs = _runs(db, project)
            opened = _opened(db, project)
            queued = set(
                db.scalars(
                    select(WorkQueueEntry.item_id)
                    .join(WorkQueue, WorkQueueEntry.queue_id == WorkQueue.id)
                    .where(
                        WorkQueue.project_id == project.id,
                        WorkQueue.state.in_(("draft", "running", "paused")),
                        WorkQueueEntry.state.in_(("waiting", "running")),
                    )
                )
            )
            order = {severity: place for place, severity in enumerate(SEVERITIES)}
            ready = [
                item
                for item in _bugs(db, project)
                if item.id not in queued
                and item.state in ("proposed", "accepted")
                and why_no_fix(item, records.get(item.id), fix_of(item, runs.get(item.id))) is None
            ]
            ready.sort(
                key=lambda item: (
                    order.get(severity_of(item, records.get(item.id)) or "", len(order)),
                    _reported(item, opened),
                )
            )
            if not ready:
                raise ProjectConflict(
                    "No confirmed bug waits for a fix: each is new, in progress, or in a "
                    "queue already."
                )
            for item in ready:
                self._to_section(db, project, item)
            return [item.id for item in ready[:QUEUED]]

    def not_a_bug(self, name: str, key: UUID) -> dict[str, Any]:
        """Say that the issue is ordinary work and no bug. Its ``bug`` label is taken off
        on GitHub; where the project has a plan it then waits for a User Story like any
        other issue."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            item = db.get(BacklogItem, key)
            if (
                item is None
                or item.project_id != project.id
                or item.id not in {row.id for row in _bugs(db, project)}
            ):
                raise ProjectNotFound(str(key))
            confirm_classification(db, project, item, "enhancement")
            return {"id": str(key), "bug": False}
