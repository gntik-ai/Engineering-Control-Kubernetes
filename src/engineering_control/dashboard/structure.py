"""The structure of the plan: how each Epic holds together, and what is missing.

Three things are looked at, and each becomes something the administrator can approve,
edit or dismiss. Nothing is created without that word.

- **Epics.** For each Epic: how many User Stories have it as their primary Epic, how
  many are linked to it at all, how close its primary Stories lie to one another, and how
  many Stories are linked to it although every other Epic they are under lies closer. The
  last number is what makes an Epic the place where Stories go that fit nowhere.
- **Splits.** An Epic that is the primary Epic of more than a tenth of all Stories is
  proposed to be split: its primary Stories are grouped by meaning, and each group beyond
  the largest would become an Epic of its own.
- **Missing User Stories.** Issues that belong to no Story, and that the scorer gives no
  existing Story a real share for, are grouped: issues that lie as close to one of them
  as an issue typically lies to its Story. Each group of three or more with an open issue
  is proposed as a User Story, worded from its issues and placed under the Epic closest
  to it. The wording is provisional: it is the administrator's to change before approving.

Groups are found from vectors and are the same each time for the same plan. A bug is no
part of any of it.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import numpy
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard import ranking
from engineering_control.dashboard.delivery import LEAD, _typical
from engineering_control.dashboard.planning import PlanningRecords
from engineering_control.dashboard.planning_links import Note, PlanningLinks
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.review_graph import terms
from engineering_control.dashboard.signals import ISSUE_STORY, _plan, _truth
from engineering_control.db.models import (
    BacklogItem,
    DocFile,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    RecordVector,
    ScorerDecision,
    StructureDecision,
)

# An Epic that is the primary Epic of more than this share of the Stories is too broad,
# where it has at least this many; a part of a split has at least this many Stories.
BROAD_SHARE: Final = 0.10
BROAD_FROM: Final = 20
PART_MIN: Final = 5
# A missing Story is proposed from at least this many issues.
GROUP_MIN: Final = 3
LISTED: Final = 12
PREFIX: Final = re.compile(
    r"^\s*(?:\[[^\]]{1,30}\]|[A-Za-z][\w -]{0,24}(?:\([^)]{0,60}\))?\s*:)\s*"
)


def _id(kind: str, *members: Any) -> str:
    said = kind + "|" + "|".join(sorted(str(member) for member in members))
    return hashlib.sha256(said.encode()).hexdigest()[:32]


def plain_title(title: str) -> str:
    """An issue's title without the labels in front of it, as a Story could be called."""
    for _ in range(2):
        title = PREFIX.sub("", title, count=1)
    title = " ".join(title.split())
    return (title[:1].upper() + title[1:])[:200] if title else "User Story"


def groups(
    keys: list[UUID], vectors: dict[tuple[str, UUID], Any], kind: str, bound: float
) -> list[list[UUID]]:
    """Records that lie close together: around the record with the most neighbours, all
    that lie within the bound of it; then the same among what is left. The first of each
    group is the one the others were gathered around."""
    have = [key for key in sorted(keys, key=str) if (kind, key) in vectors]
    if len(have) < GROUP_MIN:
        return []
    table = numpy.stack([vectors[kind, key] for key in have])
    close = table @ table.T >= bound
    found: list[list[UUID]] = []
    left = set(range(len(have)))
    order = sorted(left, key=lambda place: (-int(close[place].sum()), place))
    for place in order:
        if place not in left:
            continue
        members = [other for other in sorted(left) if close[place, other]]
        if len(members) < GROUP_MIN:
            continue
        left -= set(members)
        found.append([have[place], *(have[other] for other in members if other != place)])
    return found


def parts(keys: list[UUID], vectors: dict[tuple[str, UUID], Any]) -> list[list[UUID]]:
    """An Epic's primary Stories in two to four groups by meaning, where that divides
    them better than leaving them together; else one group. The same each time."""
    have = [key for key in sorted(keys, key=str) if ("story", key) in vectors]
    if len(have) < 2 * PART_MIN:
        return [have]
    table = numpy.stack([vectors["story", key] for key in have])
    best: tuple[float, list[int]] | None = None
    for count in (2, 3, 4):
        centre = table.mean(axis=0)
        seeds = [int(numpy.argmax(table @ centre))]
        while len(seeds) < count:
            nearest = (table @ table[seeds].T).max(axis=1)
            seeds.append(int(numpy.argmin(nearest)))
        centres = table[seeds].copy()
        labels = numpy.zeros(len(have), dtype=int)
        for _ in range(25):
            labels = numpy.argmax(table @ centres.T, axis=1)
            for part in range(count):
                mine = table[labels == part]
                if len(mine):
                    made = mine.mean(axis=0)
                    centres[part] = made / (numpy.linalg.norm(made) or 1.0)
        sizes = Counter(int(label) for label in labels)
        if len(sizes) < count or min(sizes.values()) < PART_MIN:
            continue
        # How much closer each Story lies to its own group than to the next one.
        apart = 1.0 - table @ table.T
        marks = []
        for place, label in enumerate(labels):
            own = apart[place, labels == label]
            inside = float(own.sum() / max(1, len(own) - 1))
            outside = min(
                float(apart[place, labels == other].mean()) for other in sizes if other != label
            )
            marks.append((outside - inside) / max(outside, inside, 1e-9))
        score = float(numpy.mean(marks))
        if best is None or score > best[0]:
            best = (score, [int(label) for label in labels])
    if best is None or best[0] <= 0.0:
        return [have]
    split: dict[int, list[UUID]] = defaultdict(list)
    for key, label in zip(have, best[1], strict=True):
        split[label].append(key)
    return sorted(split.values(), key=lambda group: (-len(group), str(group[0])))


def survey(db: Session, project: ProjectRecord) -> dict[str, Any]:
    """The project's Epics as they hold together, and what is proposed: Epics to split
    and User Stories that are missing."""
    plan = _plan(db, project)
    embedding = ranking.stored_model(db, RecordVector, project.id)
    vectors = ranking.vectors(db, project.id, embedding) if embedding else {}
    known, _sources = _truth(db, project.id, plan)
    bound = _typical(plan, vectors, known[ISSUE_STORY])
    open_stories = set(
        db.scalars(
            select(ProjectUserStory.id).where(
                ProjectUserStory.project_id == project.id,
                ProjectUserStory.archived_at.is_(None),
                ProjectUserStory.closed_at.is_(None),
            )
        )
    )
    primary: dict[UUID, list[UUID]] = defaultdict(list)
    linked: dict[UUID, list[UUID]] = defaultdict(list)
    far: Counter[UUID] = Counter()
    for story, links in plan.story_links.items():
        if story not in open_stories:
            continue
        mine = vectors.get(("story", story))
        close = {
            parent: float(vectors["epic", parent] @ mine)
            for parent, *_rest in links
            if mine is not None and ("epic", parent) in vectors
        }
        for parent, _origin, is_primary, _by in links:
            linked[parent].append(story)
            if is_primary:
                primary[parent].append(story)
        # Linked to it although every other Epic the Story is under lies closer.
        if len(close) >= 3:
            last = min(close, key=lambda parent: (close[parent], str(parent)))
            if last not in {parent for parent, _o, is_primary, _by in links if is_primary}:
                far[last] += 1
    with_epic = sum(1 for story in plan.story_links if story in open_stories) or 1
    epics = []
    splits = []
    for key, (title, _body) in plan.epics.items():
        mine = [story for story in primary.get(key, []) if ("story", story) in vectors]
        together = None
        if len(mine) >= 2:
            table = numpy.stack([vectors["story", story] for story in mine])
            centre = table.mean(axis=0)
            centre = centre / (numpy.linalg.norm(centre) or 1.0)
            together = round(float((table @ centre).mean()), 3)
        share = len(primary.get(key, [])) / with_epic
        broad = share > BROAD_SHARE and len(primary.get(key, [])) >= BROAD_FROM
        epics.append(
            {
                "id": str(key),
                "title": title,
                "primary": len(primary.get(key, [])),
                "linked": len(linked.get(key, [])),
                "share": round(share, 3),
                "together": together,
                "far": far[key],
                "broad": broad,
            }
        )
        if broad:
            found = parts(primary[key], vectors)
            if len(found) > 1:
                words = [
                    Counter(
                        word
                        for story in group
                        for word in set(terms({"title": plan.stories[story][0], "body": ""}))
                    )
                    for group in found
                ]
                made = []
                for place, group in enumerate(found):
                    others: Counter[str] = Counter()
                    for other, counted in enumerate(words):
                        if other != place:
                            others.update(counted)
                    own = sorted(
                        words[place],
                        key=lambda word, place=place, others=others: (  # type: ignore[misc]
                            -(words[place][word] - others[word]),
                            word,
                        ),
                    )[:3]
                    made.append(
                        {
                            "title": title if place == 0 else f"{title}: {', '.join(own)}",
                            "stays": place == 0,
                            "stories": [
                                {"id": str(story), "title": plan.stories[story][0]}
                                for story in group
                            ],
                        }
                    )
                splits.append(
                    {
                        "id": _id("split", key, *(s for group in found[1:] for s in group)),
                        "epic": {"id": str(key), "title": title},
                        "parts": made,
                    }
                )
    # Issues without a Story for which no existing Story gets a real share of belief.
    placeable = set()
    for child, detail in db.execute(
        select(ScorerDecision.child_id, ScorerDecision.detail).where(
            ScorerDecision.project_id == project.id, ScorerDecision.kind == ISSUE_STORY
        )
    ):
        if any(float(share) >= LEAD for _story, share in (detail or {}).get("first") or []):
            placeable.add(child)
    unplaced = [key for key in plan.orphans if key not in placeable]
    states = dict(
        db.execute(
            select(BacklogItem.id, BacklogItem.issue_state).where(
                BacklogItem.project_id == project.id, BacklogItem.id.in_(unplaced)
            )
        )
        .tuples()
        .all()
    )
    epic_keys = [key for key in sorted(plan.epics, key=str) if ("epic", key) in vectors]
    epic_table = numpy.stack([vectors["epic", key] for key in epic_keys]) if epic_keys else None
    stories = []
    closed_groups = 0
    for group in groups(unplaced, vectors, "issue", bound):
        if all(states.get(key) == "closed" for key in group):
            # Work that was done and closed with no Story: nothing to plan from it.
            closed_groups += 1
            continue
        centre = numpy.stack([vectors["issue", key] for key in group]).mean(axis=0)
        near = []
        if epic_table is not None:
            for place in numpy.argsort(-(epic_table @ centre))[:3]:
                epic = epic_keys[int(place)]
                near.append({"id": str(epic), "title": plan.epics[epic][0]})
        listed = [
            {
                "id": str(key),
                "title": f"#{plan.issues[key][2]} {plan.issues[key][0]}",
                "state": "closed" if states.get(key) == "closed" else "open",
            }
            for key in group
        ]
        lines = "\n".join(f"- {entry['title']}" for entry in listed[:LISTED])
        # It is called after its oldest open issue: that is what is still to be done.
        named = min(
            (key for key in group if states.get(key) != "closed"),
            key=lambda key: (plan.issues[key][2] or 0, str(key)),
        )
        stories.append(
            {
                "id": _id("story", *group),
                "title": plain_title(plan.issues[named][0]),
                "description": (
                    f"Proposed from {len(group)} issues that belong to no User Story and lie "
                    f"close together:\n{lines}"
                ),
                "issues": listed,
                "open": sum(1 for entry in listed if entry["state"] == "open"),
                "epics": near,
            }
        )
    # Documents no open Story lies close to.
    story_keys = [key for key in sorted(open_stories, key=str) if ("story", key) in vectors]
    story_table = numpy.stack([vectors["story", key] for key in story_keys]) if story_keys else None
    uncovered = []
    documents = 0
    for key, path, title in db.execute(
        select(DocFile.id, DocFile.path, DocFile.title).where(
            DocFile.project_id == project.id, DocFile.removed_scan_id.is_(None)
        )
    ):
        mine = vectors.get(("doc", key))
        if mine is None:
            continue
        documents += 1
        if story_table is None or float((story_table @ mine).max()) < bound:
            uncovered.append({"path": path, "title": title})
    decided = {
        row.proposal: row.decision
        for row in db.scalars(
            select(StructureDecision).where(StructureDecision.project_id == project.id)
        )
    }
    return {
        "epics": sorted(epics, key=lambda entry: (-entry["linked"], entry["title"])),
        "splits": [entry for entry in splits if entry["id"] not in decided],
        "stories": [entry for entry in stories if entry["id"] not in decided],
        "dismissed": sum(1 for decision in decided.values() if decision == "dismissed"),
        "approved": sum(1 for decision in decided.values() if decision == "approved"),
        "closed_groups": closed_groups,
        "unplaced": len(unplaced),
        "documents": documents,
        "uncovered": sorted(uncovered, key=lambda entry: entry["path"])[:LISTED],
        "uncovered_count": len(uncovered),
        "close_from": round(bound, 3),
        "broad_share": BROAD_SHARE,
    }


class Structure:
    def __init__(
        self, sessions: sessionmaker[Session], planning: PlanningRecords, links: PlanningLinks
    ) -> None:
        self.sessions = sessions
        self.planning = planning
        self.links = links

    def overview(self, name: str) -> dict[str, Any]:
        self.links.reconcile_project(name)
        with self.sessions() as db:
            return survey(db, active_project(db, name))

    def _proposal(self, name: str, kind: str, proposal: str) -> dict[str, Any]:
        with self.sessions() as db:
            found = survey(db, active_project(db, name))
        entry = next((e for e in found[kind] if e["id"] == proposal), None)
        if entry is None:
            raise ProjectConflict(
                "This proposal is no longer made: the plan has changed. Look at the list again."
            )
        return dict(entry)

    def _decide(self, name: str, proposal: str, kind: str, decision: str, made: Any) -> None:
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            row = db.scalar(
                select(StructureDecision).where(
                    StructureDecision.project_id == project.id,
                    StructureDecision.proposal == proposal,
                )
            )
            if row is None:
                row = StructureDecision(project_id=project.id, proposal=proposal, kind=kind)
                db.add(row)
            row.decision, row.made, row.decided_at = decision, made, datetime.now(UTC)

    def dismiss(self, name: str, proposal: str) -> dict[str, Any]:
        """Set a proposal aside. It is not made again while it stays the same."""
        with self.sessions() as db:
            found = survey(db, active_project(db, name))
        kind = next(
            (k for k in ("stories", "splits") if any(e["id"] == proposal for e in found[k])), None
        )
        if kind is None:
            raise ProjectNotFound(proposal)
        self._decide(name, proposal, kind, "dismissed", None)
        return {"id": proposal, "decision": "dismissed"}

    def add_story(
        self, name: str, proposal: str, title: str, description: str | None, epic: UUID
    ) -> dict[str, Any]:
        """Make the User Story a proposal describes, in the administrator's words and
        under the Epic they chose, and put the proposal's issues under it. An issue that
        has a Story by now, or is a bug by now, is left as it is."""
        entry = self._proposal(name, "stories", proposal)
        made = self.planning.save(name, "stories", title, description, [epic])
        story = UUID(made["id"])
        self.links.after_change(name, "stories", story)
        placed, skipped = [], 0
        for issue in entry["issues"]:
            key = UUID(issue["id"])
            with self.sessions() as db:
                taken = db.scalar(
                    select(ProjectIssueStory.story_id)
                    .where(ProjectIssueStory.item_id == key)
                    .limit(1)
                )
            if taken is not None:
                skipped += 1
                continue
            try:
                self.links.before_change(name, "issues", key)
                self.planning.link_stories(name, key, [story])
                self.links.noted(
                    name,
                    "issues",
                    key,
                    story,
                    Note(
                        "scorer",
                        confirmed=True,
                        reason=(
                            "Put under this User Story when it was made from a group of "
                            "issues that belonged to no Story and lie close together."
                        ),
                    ),
                )
                placed.append(str(key))
            except (ProjectInvalid, ProjectNotFound, ProjectConflict):
                skipped += 1
        self._decide(name, proposal, "stories", "approved", {"story": str(story), "issues": placed})
        return {"id": proposal, "story_id": str(story), "linked": len(placed), "skipped": skipped}

    def split(self, name: str, proposal: str, titles: list[str]) -> dict[str, Any]:
        """Split an Epic as a proposal describes: each group beyond the largest becomes
        an Epic with the title given for it, and the primary Epic of its Stories. The
        Stories stay linked to the Epic they came from."""
        entry = self._proposal(name, "splits", proposal)
        new = [part for part in entry["parts"] if not part["stays"]]
        if len(titles) != len(new) or any(not title.strip() for title in titles):
            raise ProjectInvalid("Give each new Epic a title.")
        source = entry["epic"]["title"]
        created = []
        for part, title in zip(new, titles, strict=True):
            made = self.planning.save(
                name, "epics", title, f"Split off from the Epic “{source}”.", []
            )
            epic = UUID(made["id"])
            moved = []
            for story in part["stories"]:
                key = UUID(story["id"])
                with self.sessions.begin() as db:
                    project = active_project(db, name, lock=True)
                    alive = db.scalar(
                        select(ProjectUserStory.id).where(
                            ProjectUserStory.id == key,
                            ProjectUserStory.project_id == project.id,
                            ProjectUserStory.archived_at.is_(None),
                        )
                    )
                    if alive is None:
                        continue
                    db.add(ProjectStoryEpic(project_id=project.id, story_id=key, epic_id=epic))
                self.links.after_change(name, "stories", key)
                self.links.set_primary(
                    name,
                    "stories",
                    key,
                    epic,
                    why=Note("scorer", reason=f"Moved here when the Epic “{source}” was split."),
                )
                moved.append(str(key))
            created.append({"epic": str(epic), "title": title, "stories": moved})
        self._decide(name, proposal, "splits", "approved", {"epics": created})
        return {"id": proposal, "epics": created}
