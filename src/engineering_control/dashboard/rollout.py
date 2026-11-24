"""The reorganization, across every project and as records arrive.

Nothing here decides anything. It answers three questions that only make sense once the
pieces run by themselves:

- **Where does each project stand?** One row a project: whether it has a plan at all, how
  much the ranking service has read, whether the scorer is measured on it, what waits for
  approval, and the next step, said in a sentence.
- **What arrived, and where is it now?** The issues that came in lately, each with where
  the cascade put it: a bug, under a Story, in a batch to approve, a lead for a Story, or
  waiting with its closest Story named.
- **What moved overnight?** Once a day the scorer, the Stories' states and the health of
  every project are worked out again whatever changed, and the day's numbers are compared
  with the day before (``planning_drift``).

A project that was never through Discovery has no Epics and no User Stories: there is
nothing to reorganize, and its row says so.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.planning import _visible_issues
from engineering_control.dashboard.planning_drift import drift
from engineering_control.dashboard.planning_links import PlanningLinks
from engineering_control.dashboard.project_records import active_project
from engineering_control.dashboard.signals import BUCKETS, ISSUE_STORY
from engineering_control.db.models import (
    BacklogItem,
    ChoiceReview,
    DocFile,
    GoldPair,
    PlanningLink,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectUserStory,
    RecordVector,
    ScorerDecision,
    ScorerReport,
    StoryDelivery,
    WorkspaceSource,
)

ARRIVED_DAYS: Final = 14
LISTED: Final = 50


def _some(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def _count(db: Session, model: Any, *where: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def _row(db: Session, project: ProjectRecord) -> dict[str, Any]:
    pid = project.id
    epics = _count(
        db, ProjectEpic, ProjectEpic.project_id == pid, ProjectEpic.archived_at.is_(None)
    )
    stories = _count(
        db,
        ProjectUserStory,
        ProjectUserStory.project_id == pid,
        ProjectUserStory.archived_at.is_(None),
        ProjectUserStory.closed_at.is_(None),
    )
    issues = _count(
        db, BacklogItem, BacklogItem.project_id == pid, BacklogItem.issue_number.is_not(None)
    )
    documents = _count(db, DocFile, DocFile.project_id == pid, DocFile.removed_scan_id.is_(None))
    vectors = _count(db, RecordVector, RecordVector.project_id == pid)
    judged = _count(db, GoldPair, GoldPair.project_id == pid, GoldPair.label.is_not(None))
    drawn = _count(db, GoldPair, GoldPair.project_id == pid)
    report = db.scalar(select(ScorerReport).where(ScorerReport.project_id == pid))
    kinds = (report.report or {}).get("kinds", []) if report is not None else []
    would: Counter[str] = Counter()
    for entry in kinds:
        would.update({key: int(value) for key, value in (entry.get("would") or {}).items()})
    measured = {entry["kind"]: bool(entry.get("measured_here")) for entry in kinds}
    states = dict(
        db.execute(
            select(StoryDelivery.state, func.count())
            .where(StoryDelivery.project_id == pid)
            .group_by(StoryDelivery.state)
        )
        .tuples()
        .all()
    )
    unsupported = int(states.get("unsupported", 0))
    answered = _count(
        db,
        ChoiceReview,
        ChoiceReview.project_id == pid,
        ChoiceReview.kind == "story_needed",
        ChoiceReview.state == "answered",
    )
    moved = drift(db, pid)
    numbers = (moved or {}).get("numbers", {})
    wanted = sum(count for _kind, count in BUCKETS.values())
    planned = bool(epics and stories)
    steps: list[dict[str, str]] = []
    if not planned:
        steps.append(
            {
                "view": "discovery",
                "says": (
                    "It has no Epics and no User Stories: bring it through Discovery first. "
                    "Until then there is nothing to reorganize."
                ),
            }
        )
    else:
        if not vectors:
            steps.append(
                {"view": "reorganize", "says": "The ranking service has not read its records yet."}
            )
        if numbers.get("changes_ready"):
            steps.append(
                {
                    "view": "reorganize",
                    "says": _some(numbers["changes_ready"], "change waits", "changes wait")
                    + " for your approval.",
                }
            )
        if numbers.get("structure_proposals"):
            steps.append(
                {
                    "view": "reorganize",
                    "says": _some(
                        numbers["structure_proposals"],
                        "proposal for the structure waits",
                        "proposals for the structure wait",
                    )
                    + " for your word.",
                }
            )
        if unsupported > answered:
            steps.append(
                {
                    "view": "reorganize",
                    "says": f"Ask a model about the {unsupported - answered:,} User Stories that "
                    "nothing supports and that have no answer yet.",
                }
            )
        if vectors and not all(measured.get(kind) for kind in (ISSUE_STORY, "story_epic")):
            steps.append(
                {
                    "view": "reorganize",
                    "says": (
                        f"Judge the pairs ({judged:,} of {max(drawn, wanted):,} judged): the "
                        "scorer is not yet measured on this project's own links."
                    ),
                }
            )
        if numbers.get("bugs_linked"):
            steps.append(
                {
                    "view": "bugs",
                    "says": _some(numbers["bugs_linked"], "bug still carries", "bugs still carry")
                    + " a link to a User Story.",
                }
            )
        if numbers.get("orphans_open"):
            steps.append(
                {
                    "view": "related-work",
                    "says": _some(numbers["orphans_open"], "open issue has", "open issues have")
                    + " no User Story.",
                }
            )
    return {
        "project": project.name,
        "display_name": project.display_name,
        "planned": planned,
        "epics": epics,
        "stories": stories,
        "issues": issues,
        "documents": documents,
        "vectors": vectors,
        "pairs": {"judged": judged, "drawn": drawn, "wanted": wanted},
        "scorer": {
            "computed_at": report.computed_at.isoformat()
            if report is not None and report.computed_at
            else None,
            "measured_here": measured,
            "sure": sum(would[key] for key in ("place", "primary", "confirmed", "unsupported")),
            "abstains": sum(would[key] for key in ("unsure", "several", "none")),
        },
        "states": {key: int(value) for key, value in states.items()},
        "drift": moved,
        "steps": steps,
    }


class Rollout:
    def __init__(self, sessions: sessionmaker[Session], links: PlanningLinks) -> None:
        self.sessions = sessions
        self.links = links

    def overview(self) -> dict[str, Any]:
        """Every project with where it stands and what its next step is."""
        with self.sessions() as db:
            projects = db.scalars(
                select(ProjectRecord)
                .where(ProjectRecord.archived_at.is_(None))
                .order_by(ProjectRecord.name)
            ).all()
            return {"projects": [_row(db, project) for project in projects]}

    def status(self, name: str) -> dict[str, Any]:
        """One project's row: for its own Reorganize page."""
        with self.sessions() as db:
            return _row(db, active_project(db, name))

    def arrivals(self, name: str, days: int = ARRIVED_DAYS) -> dict[str, Any]:
        """The issues that arrived in the last days, the newest first, each with where
        the cascade put it."""
        since = datetime.now(UTC) - timedelta(days=days)
        # A link made since the records were last brought in step has no record yet.
        self.links.reconcile_project(name)
        with self.sessions() as db:
            project = active_project(db, name)
            pid = project.id
            # An issue arrived when it was opened on GitHub, not when the platform first
            # read it: a project's whole history is read on one day. Only issues of the
            # plan are counted; one that is itself an Epic or a Story is not.
            opened: dict[UUID, datetime] = {}
            rows = []
            # What Discovery read of each issue says when it was opened, for the issues
            # that have not been read again since.
            read_before = {
                key: (evidence or {}).get("created_at")
                for key, evidence in db.execute(
                    select(WorkspaceSource.key, WorkspaceSource.evidence).where(
                        WorkspaceSource.project_id == pid, WorkspaceSource.key.like("issue:%")
                    )
                )
            }
            for row in db.scalars(
                select(BacklogItem).where(
                    BacklogItem.project_id == pid,
                    BacklogItem.issue_number.is_not(None),
                    _visible_issues(pid),
                    BacklogItem.workspace_review.is_distinct_from("planning"),
                )
            ):
                said = (row.facts or {}).get("github_created_at") or read_before.get(
                    f"issue:{row.issue_number}"
                )
                if not said:
                    continue
                at = datetime.fromisoformat(str(said).replace("Z", "+00:00"))
                at = at if at.tzinfo else at.replace(tzinfo=UTC)
                if at >= since:
                    opened[row.id] = at
                    rows.append(row)
            rows.sort(key=lambda row: (-opened[row.id].timestamp(), str(row.id)))
            keys = [row.id for row in rows]
            bugs = set(
                db.scalars(bugs_with_optional_stories(db, pid).where(BacklogItem.id.in_(keys)))
            )
            titles = dict(
                db.execute(
                    select(ProjectUserStory.id, ProjectUserStory.title).where(
                        ProjectUserStory.project_id == pid
                    )
                )
                .tuples()
                .all()
            )
            linked: dict[UUID, list[tuple[UUID, str]]] = {}
            origins = {
                (child, parent): origin
                for child, parent, origin in db.execute(
                    select(
                        PlanningLink.child_id, PlanningLink.parent_id, PlanningLink.origin
                    ).where(
                        PlanningLink.project_id == pid,
                        PlanningLink.kind == ISSUE_STORY,
                        PlanningLink.removed_at.is_(None),
                        PlanningLink.child_id.in_(keys),
                    )
                )
            }
            for item, parent in db.execute(
                select(ProjectIssueStory.item_id, ProjectIssueStory.story_id).where(
                    ProjectIssueStory.project_id == pid, ProjectIssueStory.item_id.in_(keys)
                )
            ):
                linked.setdefault(item, []).append((parent, origins.get((item, parent), "unknown")))
            decided = {
                row.child_id: row
                for row in db.scalars(
                    select(ScorerDecision).where(
                        ScorerDecision.project_id == pid,
                        ScorerDecision.kind == ISSUE_STORY,
                        ScorerDecision.child_id.in_(keys),
                    )
                )
            }
            read = set(
                db.scalars(
                    select(RecordVector.record_id).where(
                        RecordVector.project_id == pid,
                        RecordVector.kind == "issue",
                        RecordVector.record_id.in_(keys),
                    )
                )
            )
            counts: Counter[str] = Counter()
            items: list[dict[str, Any]] = []
            for row in rows:
                story: dict[str, Any] | None = None
                share: float | None = None
                if row.id in bugs:
                    stands = "bug"
                elif row.id in linked:
                    stands = "linked"
                    key, origin = linked[row.id][0]
                    story = {"id": str(key), "title": titles.get(key, ""), "origin": origin}
                elif row.id in decided:
                    decision = decided[row.id]
                    stands = "batch" if decision.outcome == "place" else "waiting"
                    if decision.parent_id:
                        story = {
                            "id": str(decision.parent_id),
                            "title": titles.get(decision.parent_id, ""),
                        }
                        share = decision.confidence
                elif row.id in read:
                    stands = "unscored"
                else:
                    stands = "unread"
                counts[stands] += 1
                if len(items) < LISTED:
                    items.append(
                        {
                            "id": str(row.id),
                            "title": f"#{row.issue_number} {row.title}",
                            "state": "closed" if row.issue_state == "closed" else "open",
                            "arrived_at": opened[row.id].isoformat(),
                            "stands": stands,
                            "story": story,
                            "share": share,
                        }
                    )
            return {
                "days": days,
                "total": len(rows),
                "counts": dict(counts),
                "items": items,
            }
