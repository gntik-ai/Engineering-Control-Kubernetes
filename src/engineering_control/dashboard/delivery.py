"""Where each User Story stands, from evidence.

"User Stories without an issue" is one number for several different things: a Story that
was delivered and whose work was never linked, a Story nobody has started, a Story that
nothing in the project supports any more. Each open Story gets one state here, with what
shows it:

- ``delivered``: its issues are all closed as done, and something shows the whole Story
  was covered: a review that found an issue covering it in full, or the OpenSpec change of
  one of its issues with every task done or archived. Two signals, so that it can be
  offered for closing.
- ``closed_work``: its issues are all closed as done, and nothing yet shows that they
  covered the whole Story.
- ``in_progress``: an issue of it is open.
- ``dropped``: every issue of it was closed as not planned.
- ``leads``: no issue is linked, but issues that are not under any Story look like its
  work: the scorer gives this Story a real share of belief for them. They are leads to
  link, not evidence yet.
- ``planned``: no issue, linked or alike, but a document of the repository lies as close
  to it as a right link typically does.
- ``unsupported``: nothing was found in issues or documents.

Only what is linked decides between the first four; what merely looks alike is shown as
a lead and decides nothing. Bugs are no part of this: a bug is no Story's work. Nothing
here changes a Story. Closing the delivered ones is a batch to approve (``reorg.py``).
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Any, Final
from uuid import UUID

import numpy
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import ranking
from engineering_control.dashboard.planning_links import PlanningLinks
from engineering_control.dashboard.project_records import ProjectNotFound, active_project
from engineering_control.dashboard.signals import ISSUE_STORY, _Plan, _plan, _truth
from engineering_control.db.models import (
    BacklogItem,
    DocFile,
    PlanningLink,
    ProjectRecord,
    ProjectUserStory,
    RecordVector,
    ScorerDecision,
    StoryDelivery,
)

STATES: Final = {
    "delivered": "Delivered: its issues are closed and the whole Story is shown covered",
    "closed_work": "Work closed: not yet shown to cover the whole Story",
    "in_progress": "In progress: an issue of it is open",
    "dropped": "Dropped: its issues were closed as not planned",
    "leads": "No linked work, but issues without a User Story look like it",
    "planned": "No work yet: a document describes it",
    "unsupported": "Nothing supports it: no issue and no document",
}
# The share of belief from which an unlinked issue counts as a lead for a Story.
LEAD: Final = 0.2
# Where no right link is known to take the measure from: how close a document must lie.
CLOSE: Final = 0.7
LISTED: Final = 8


def _typical(
    plan: _Plan, vectors: dict[tuple[str, UUID], Any], known: dict[UUID, set[UUID]]
) -> float:
    """How close an issue typically lies to the Story it is known to belong to: the
    measure a document is held to before it counts as describing a Story."""
    found = [
        float(vectors["issue", issue] @ vectors["story", story])
        for issue, stories in known.items()
        for story in stories
        if ("issue", issue) in vectors and ("story", story) in vectors
    ]
    return statistics.median(found) if len(found) >= 10 else CLOSE


def compute(db: Session, project: ProjectRecord) -> list[tuple[UUID, str, dict[str, Any]]]:
    """Each open User Story of the project with its state and the evidence for it."""
    plan = _plan(db, project)
    open_stories = set(
        db.scalars(
            select(ProjectUserStory.id).where(
                ProjectUserStory.project_id == project.id,
                ProjectUserStory.archived_at.is_(None),
                ProjectUserStory.closed_at.is_(None),
            )
        )
    )
    issues = {
        key: (number, title, state or "open", (facts or {}).get("github_state_reason"))
        for key, number, title, state, facts in db.execute(
            select(
                BacklogItem.id,
                BacklogItem.issue_number,
                BacklogItem.title,
                BacklogItem.issue_state,
                BacklogItem.facts,
            ).where(BacklogItem.project_id == project.id)
        )
    }
    strengths = {
        (child, parent): strength
        for child, parent, strength in db.execute(
            select(PlanningLink.child_id, PlanningLink.parent_id, PlanningLink.strength).where(
                PlanningLink.project_id == project.id,
                PlanningLink.kind == ISSUE_STORY,
                PlanningLink.removed_at.is_(None),
            )
        )
    }
    # The Story's own issues. A bug is no part of the plan and is not among them.
    under: dict[UUID, list[UUID]] = defaultdict(list)
    for issue, links in plan.issue_links.items():
        for story, _origin, _primary in links:
            under[story].append(issue)
    # OpenSpec changes, by the number of the issue they are named after or that names them.
    changes: dict[str, dict[str, Any]] = {}
    docs: dict[UUID, tuple[str, str | None, str | None]] = {}
    for key, path, title, facts in db.execute(
        select(DocFile.id, DocFile.path, DocFile.title, DocFile.facts).where(
            DocFile.project_id == project.id, DocFile.removed_scan_id.is_(None)
        )
    ):
        spec = (facts or {}).get("openspec") or {}
        docs[key] = (path, title, spec.get("change"))
        if not spec.get("change"):
            continue
        entry = changes.setdefault(
            spec["change"],
            {"change": spec["change"], "archived": False, "issue": None, "done": 0, "tasks": 0},
        )
        entry["archived"] = entry["archived"] or bool(spec.get("archived"))
        entry["issue"] = entry["issue"] or spec.get("issue")
        tasks = (facts or {}).get("tasks") or {}
        entry["done"] += int(tasks.get("done") or 0)
        entry["tasks"] += int(tasks.get("total") or 0)
    of_issue: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    by_number = {number: key for key, (number, *_rest) in issues.items() if number}
    for entry in changes.values():
        named = {
            key
            for key, (_number, title, *_rest) in issues.items()
            if entry["change"].lower() in (title or "").lower()
        }
        if entry["issue"] in by_number:
            named.add(by_number[entry["issue"]])
        for key in named:
            of_issue[key].append(entry)
    # Issues under no Story that the scorer would put here, were it surer.
    leads: dict[UUID, list[tuple[float, UUID]]] = defaultdict(list)
    for child, detail in db.execute(
        select(ScorerDecision.child_id, ScorerDecision.detail).where(
            ScorerDecision.project_id == project.id,
            ScorerDecision.kind == ISSUE_STORY,
            ScorerDecision.outcome.in_(("place", "unsure", "several", "none")),
        )
    ):
        if child in plan.issue_links or child not in plan.issues:
            continue
        for story, share in (detail or {}).get("first") or []:
            if float(share) >= LEAD:
                leads[UUID(str(story))].append((float(share), child))
    # Documents that lie as close to a Story as a right link typically does.
    embedding = ranking.stored_model(db, RecordVector, project.id)
    vectors = ranking.vectors(db, project.id, embedding) if embedding else {}
    known, _sources = _truth(db, project.id, plan)
    bound = _typical(plan, vectors, known[ISSUE_STORY])
    doc_keys = [key for key in sorted(docs, key=str) if ("doc", key) in vectors]
    table = numpy.stack([vectors["doc", key] for key in doc_keys]) if doc_keys else None

    def shown(issue: UUID, story: UUID | None = None, share: float | None = None) -> dict[str, Any]:
        number, title, state, reason = issues.get(issue, (None, "", "open", None))
        return {
            "id": str(issue),
            "number": number,
            "title": title,
            "state": "closed" if state == "closed" else "open",
            "not_planned": state == "closed" and reason == "not_planned",
            "strength": strengths.get((issue, story)) if story else None,
            "share": round(share, 3) if share is not None else None,
        }

    found = []
    for story in sorted(open_stories & set(plan.stories), key=str):
        mine = sorted(
            under.get(story, []), key=lambda key: (issues.get(key, (0,))[0] or 0, str(key))
        )
        states = [issues.get(key, (None, "", "open", None)) for key in mine]
        open_count = sum(1 for _n, _t, state, _r in states if state != "closed")
        not_planned = sum(
            1 for _n, _t, state, reason in states if state == "closed" and reason == "not_planned"
        )
        done = len(mine) - open_count - not_planned
        specs = [entry for key in mine for entry in of_issue.get(key, [])]
        finished_specs = [
            entry
            for entry in specs
            if entry["archived"] or (entry["tasks"] and entry["done"] >= entry["tasks"])
        ]
        covered = any(strengths.get((key, story)) == "full" for key in mine)
        evidence: dict[str, Any] = {
            "issues": {"open": open_count, "done": done, "not_planned": not_planned},
            "linked": [shown(key, story) for key in mine[:LISTED]],
            "openspec": [
                {
                    "change": entry["change"],
                    "archived": entry["archived"],
                    "done": entry["done"],
                    "tasks": entry["tasks"],
                }
                for entry in specs[:LISTED]
            ],
            "leads": [],
            "documents": [],
        }
        if open_count:
            state = "in_progress"
        elif mine and not done:
            state = "dropped"
        elif mine and (covered or finished_specs):
            state = "delivered"
            evidence["shown_by"] = (
                "A review found an issue that covers the whole Story."
                if covered
                else f"The OpenSpec change {finished_specs[0]['change']} has every task done."
            )
        elif mine:
            state = "closed_work"
        else:
            alike = sorted(leads.get(story, []), key=lambda entry: (-entry[0], str(entry[1])))
            evidence["leads"] = [shown(issue, share=share) for share, issue in alike[:LISTED]]
            mine_vector = vectors.get(("story", story))
            if table is not None and mine_vector is not None:
                close = table @ mine_vector
                for place in numpy.argsort(-close)[:3]:
                    if float(close[place]) >= bound:
                        path, title, change = docs[doc_keys[int(place)]]
                        evidence["documents"].append(
                            {
                                "path": path,
                                "title": title,
                                "change": change,
                                "likeness": round(float(close[place]), 3),
                            }
                        )
            state = "leads" if alike else "planned" if evidence["documents"] else "unsupported"
            evidence["looked_at"] = {
                "issues_without_story": len(plan.orphans),
                "documents": len(doc_keys),
                "close_from": round(bound, 3),
            }
        found.append((story, state, evidence))
    return found


class Delivery:
    def __init__(self, sessions: sessionmaker[Session], links: PlanningLinks) -> None:
        self.sessions = sessions
        self.links = links

    def refresh(self, name: str) -> int:
        """Work out where each open Story of the project stands, and keep it. Only this
        module's own table is written. Returns how many Stories were looked at."""
        try:
            self.links.reconcile_project(name)
            with self.sessions() as db:
                project = active_project(db, name)
                pid = project.id
                found = compute(db, project)
        except ProjectNotFound:
            return 0
        with self.sessions.begin() as db:
            db.execute(delete(StoryDelivery).where(StoryDelivery.project_id == pid))
            db.add_all(
                StoryDelivery(project_id=pid, story_id=story, state=state, evidence=evidence)
                for story, state, evidence in found
            )
        return len(found)

    def refresh_all(self) -> int:
        with self.sessions() as db:
            names = list(
                db.scalars(
                    select(ProjectRecord.name)
                    .where(ProjectRecord.archived_at.is_(None))
                    .order_by(ProjectRecord.name)
                )
            )
        return sum(self.refresh(name) for name in names)

    def overview(self, name: str) -> dict[str, Any]:
        """How many open Stories stand in each state, and when that was worked out."""
        with self.sessions() as db:
            project = active_project(db, name)
            counts: Counter[str] = Counter()
            for state, count in db.execute(
                select(StoryDelivery.state, func.count())
                .where(StoryDelivery.project_id == project.id)
                .group_by(StoryDelivery.state)
            ):
                counts[state] = count
            at = db.scalar(
                select(func.max(StoryDelivery.updated_at)).where(
                    StoryDelivery.project_id == project.id
                )
            )
            return {
                "states": [
                    {"state": state, "label": label, "count": counts[state]}
                    for state, label in STATES.items()
                ],
                "total": sum(counts.values()),
                "computed_at": at.isoformat() if at else None,
            }

    def listing(self, name: str, state: str, page: int, page_size: int) -> dict[str, Any]:
        """The open Stories in one state, or in any, each with its evidence."""
        with self.sessions() as db:
            project = active_project(db, name)
            scope = [StoryDelivery.project_id == project.id]
            if state:
                scope.append(StoryDelivery.state == state)
            total = int(
                db.scalar(select(func.count()).select_from(StoryDelivery).where(*scope)) or 0
            )
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            rows = db.execute(
                select(StoryDelivery, ProjectUserStory.title)
                .join(ProjectUserStory, ProjectUserStory.id == StoryDelivery.story_id)
                .where(*scope)
                .order_by(ProjectUserStory.title, StoryDelivery.story_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "items": [
                    {
                        "id": str(row.story_id),
                        "title": title,
                        "state": row.state,
                        "label": STATES.get(row.state, row.state),
                        **(row.evidence or {}),
                    }
                    for row, title in rows
                ],
                "total": total,
                "page": page,
                "pages": pages,
            }


def numbers(db: Session, project: UUID) -> dict[str, int]:
    """For Planning health: Stories that nothing supports, delivered ones still open,
    and those with work that only looks like theirs."""
    counts = dict(
        db.execute(
            select(StoryDelivery.state, func.count())
            .where(StoryDelivery.project_id == project)
            .group_by(StoryDelivery.state)
        )
        .tuples()
        .all()
    )
    return {
        "stories_unsupported": int(counts.get("unsupported", 0)),
        "stories_delivered_open": int(counts.get("delivered", 0)),
        "stories_with_leads": int(counts.get("leads", 0)),
    }
