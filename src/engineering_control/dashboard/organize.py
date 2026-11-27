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
    OrganizeLine,
    OrganizeRun,
    PlanningLink,
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
            count(number, kind, item.issue_state or "open", item.workspace_review == "planning")
    for number, evidence in bodies.items():
        if number in seen:
            continue
        kind = planning_kind(evidence)
        if kind:
            count(number, kind, str(evidence.get("state") or "open"), False)
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
    several_chosen = sum(
        1
        for row in stories
        if UUID(str(row["id"])) in chosen
        and len([key for key in row.get("epic_ids") or [] if str(key) in open_epics]) > 1
    )
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
    known_bugs = db.scalar(
        select(func.count()).select_from(bugs_with_optional_stories(db, project.id).subquery())
    )
    return {
        "project": project.name,
        "epics_stories": {
            "epics": len(epics),
            "stories": len(stories),
            # Settled: under exactly one open Epic.
            "stories_under_one_epic": one,
            "stories_under_several_epics": several,
            "stories_under_several_epics_chosen": several_chosen,
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
            "bugs_in_all": int(known_bugs or 0),
            "stories": len(stories),
            "stories_with_open_issues": len(has_open_issue),
            "stories_with_closed_issues_only": closed_issues_only,
            "stories_with_no_issue": sum(no_issue.values()),
            # Of those with no Issue at all: where the evidence says each stands.
            "stories_with_no_issue_by_standing": dict(sorted(no_issue.items())),
        },
        "github": _on_github(db, project),
        "documentation": doc_parts.survey(db, project),
    }


def ground(sessions: sessionmaker[Session], name: str, *, fresh: bool = False) -> dict[str, Any]:
    """What a first run would meet in the project: how its Epics, User Stories and Issues
    stand to each other, which of its Epics and Stories are issues on GitHub, and how its
    documentation cuts into parts. Counted from the records; no model is asked."""
    kept = _KEPT.get(name)
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
    _KEPT[name] = (now, counted)
    return counted


def _shown_line(line: OrganizeLine, now: datetime) -> dict[str, Any]:
    return {
        "id": str(line.id),
        "run": str(line.run_id),
        "list": line.listed,
        "case": line.case,
        "subject": {
            "kind": line.subject_kind,
            "id": str(line.subject_id) if line.subject_id else None,
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
        "can_undo": line.listed != "needs_you"
        and line.state == "applied"
        and now - _aware(line.created_at) < UNDO,
    }


def _shown_run(run: OrganizeRun, lists: dict[str, int]) -> dict[str, Any]:
    counts = run.counts or {}
    reached = list(STAGES).index(run.stage) if run.stage in STAGES else 0
    return {
        "id": str(run.id),
        "kind": run.kind,
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


class Organize:
    """The runs of Organize and Coverage, and what each found."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def ground(self, name: str, *, fresh: bool = False) -> dict[str, Any]:
        return ground(self.sessions, name, fresh=fresh)

    # -- a run, from the word that proposes it to its end ---------------------------

    def propose(
        self,
        name: str,
        kind: str,
        *,
        estimate: dict[str, Any] | None = None,
        dollar_cap: float | None = None,
        auto: bool = False,
    ) -> UUID:
        """A run that says what it would ask and cost, and waits for the word. One run
        of a kind at a time in a project: a second is refused while the first is live."""
        if kind not in KINDS:
            raise ProjectInvalid("Choose what to organize")
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            live = db.scalar(
                select(OrganizeRun.id).where(
                    OrganizeRun.project_id == project.id,
                    OrganizeRun.kind == kind,
                    OrganizeRun.state.in_(LIVE),
                )
            )
            if live is not None:
                raise ProjectConflict("A run of this kind is under way in this project")
            run = OrganizeRun(
                project_id=project.id,
                kind=kind,
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
            shown = [_shown_run(run, lists.get(run.id, {})) for run in runs]
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
                "items": [_shown_line(row, now) for row in rows],
            }
