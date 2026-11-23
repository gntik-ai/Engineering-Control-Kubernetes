"""Who made each link between planning records and why, and which parent is the primary one.

Whether a User Story is under an Epic, or a technical Issue under a User Story, is said by
the link tables, and everything that reads or writes those keeps working as it does. What
those tables cannot say is kept here, one record a link: who made it, the reason and the
passages that justify it, and whether it is the child's primary parent.

The records are kept in step with the link tables, not written beside every write: a
link that has no record yet gets one, with what can be established of it at that moment,
and the record of a link that is gone is kept with the day it went. So a link made by any
part of the platform is recorded, also by a part written later, and a link's history is
never lost with the link.

Who made a link is established from facts that are stored, never guessed:

- ``declared``: the child's own text names the parent ("Story: #22", "Epic: #7");
- ``administrator``: the administrator chose the child's parents by hand;
- ``model``: a review matched the two, with the evidence it gave, or proposed the record
  with this parent and the administrator approved the proposal;
- ``unknown``: nothing stored explains it.

A child has one primary parent. An only parent is the primary one. Of several, the first
by how much is known of the link, then by how alike the two texts are, is suggested until
the administrator chooses.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final, Literal, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.workspace_issues import STORY_LINE, STORY_REF
from engineering_control.db.models import (
    BacklogItem,
    PlanningLink,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    StoryIssueCoverage,
    WorkspaceCandidate,
)

Child = Literal["stories", "issues"]
Pair = tuple[UUID, UUID]
ORIGINS: Final = ("declared", "administrator", "model", "unknown")
# A line of a Story's text that names its Epic.
EPIC_LINE: Final = re.compile(
    r"^\s*(?:[-*]\s*)?(?:parent\s+)?(?:parent|epics?)\s*:\s*(.*)$", re.I | re.M
)
WORD: Final = re.compile(r"[a-z][a-z0-9]{2,}")
COMMON: Final = frozenset(
    "the and for are its you can that with this from when then has also will only into than have "
    "should what which want user story epic issue".split()
)
CHUNK: Final = 500
# How much is known of a link, for choosing among several parents: the lower the more.
_RANK: Final = {"declared": 0, "administrator": 1, "model": 2, "unknown": 3}


@dataclass(frozen=True)
class _Spec:
    name: str
    table: Any
    child: Any
    parent: Any


_SPECS: Final = {
    "stories": _Spec(
        "story_epic", ProjectStoryEpic, ProjectStoryEpic.story_id, ProjectStoryEpic.epic_id
    ),
    "issues": _Spec(
        "issue_story", ProjectIssueStory, ProjectIssueStory.item_id, ProjectIssueStory.story_id
    ),
}


@dataclass(frozen=True)
class Note:
    """What is established of a link when its record is made."""

    origin: str
    confirmed: bool = False
    reason: str | None = None
    evidence: list[dict[str, str]] | None = None
    strength: str | None = None


def _chunks(values: list[UUID]) -> Iterable[list[UUID]]:
    for start in range(0, len(values), CHUNK):
        yield values[start : start + CHUNK]


def _numbers(db: Session, project: UUID, kind: str, records: set[UUID]) -> dict[UUID, int]:
    """The GitHub number each planning record was imported from, where it was."""
    found: dict[UUID, int] = {}
    for some in _chunks(sorted(records)):
        for local, number in db.execute(
            select(WorkspaceCandidate.local_id, WorkspaceCandidate.number).where(
                WorkspaceCandidate.project_id == project,
                WorkspaceCandidate.kind == kind,
                WorkspaceCandidate.decision == "approved",
                WorkspaceCandidate.number.is_not(None),
                WorkspaceCandidate.local_id.in_(some),
            )
        ):
            if local is not None and number is not None:
                found[local] = number
    return found


def _named(text: str, lines: re.Pattern[str], number: int | None, repository: str) -> str | None:
    """The line of ``text`` that names that number of this repository as a parent."""
    if number is None:
        return None
    for line in lines.finditer(text):
        for ref in STORY_REF.finditer(line.group(0)):
            if (ref[1] or ref[2] or repository).lower() == repository.lower() and int(
                ref[3]
            ) == number:
                return " ".join(line.group(0).split())[:300]
    return None


def _approved(db: Session, project: UUID, kind: str, children: set[UUID]) -> dict[UUID, Any]:
    found: dict[UUID, Any] = {}
    for some in _chunks(sorted(children)):
        for candidate in db.scalars(
            select(WorkspaceCandidate).where(
                WorkspaceCandidate.project_id == project,
                WorkspaceCandidate.kind == kind,
                WorkspaceCandidate.decision == "approved",
                WorkspaceCandidate.local_id.in_(some),
            )
        ):
            if candidate.local_id is not None:
                found[candidate.local_id] = candidate
    return found


def _established(
    db: Session, project: ProjectRecord, kind: Child, pairs: set[Pair]
) -> dict[Pair, Note]:
    """What the stored facts say of each new link."""
    children = {child for child, _parent in pairs}
    parents = {parent for _child, parent in pairs}
    repository = project.source_repository
    proposals = _approved(db, project.id, kind, children)
    notes: dict[Pair, Note] = {}
    if kind == "issues":
        numbers = _numbers(db, project.id, "stories", parents)
        items: dict[UUID, BacklogItem] = {}
        for some in _chunks(sorted(children)):
            for item in db.scalars(
                select(BacklogItem).where(
                    BacklogItem.project_id == project.id, BacklogItem.id.in_(some)
                )
            ):
                items[item.id] = item
        matched: dict[Pair, dict[str, Any]] = {}
        for some in _chunks(sorted(parents)):
            for checkpoint in db.scalars(
                select(StoryIssueCoverage).where(
                    StoryIssueCoverage.project_id == project.id,
                    StoryIssueCoverage.story_id.in_(some),
                )
            ):
                for match in checkpoint.matches or []:
                    try:
                        matched[UUID(str(match["id"])), checkpoint.story_id] = match
                    except (KeyError, ValueError, TypeError):
                        continue
        for child, parent in pairs:
            issue = items.get(child)
            line = (
                _named(issue.description or "", STORY_LINE, numbers.get(parent), repository)
                if issue is not None
                else None
            )
            proposal = proposals.get(child)
            if line:
                notes[child, parent] = Note("declared", evidence=[{"side": "issue", "text": line}])
            elif issue is not None and (issue.facts or {}).get("manual_story_links"):
                notes[child, parent] = Note("administrator", confirmed=True)
            elif (child, parent) in matched:
                match = matched[child, parent]
                said = " ".join(str(match.get("evidence") or "").split())[:600]
                notes[child, parent] = Note(
                    "model",
                    reason=said or None,
                    strength="full" if match.get("full") else "partial",
                )
            elif proposal is not None and str(parent) in (proposal.parents or []):
                notes[child, parent] = Note(
                    "model", confirmed=True, reason=proposal.review_reason or None
                )
        return notes
    numbers = _numbers(db, project.id, "epics", parents)
    texts: dict[UUID, str] = {}
    for some in _chunks(sorted(children)):
        for local, body in db.execute(
            select(ProjectUserStory.id, ProjectUserStory.description).where(
                ProjectUserStory.project_id == project.id, ProjectUserStory.id.in_(some)
            )
        ):
            texts[local] = body or ""
    for child, parent in pairs:
        proposal = proposals.get(child)
        # What the Story said on GitHub, where it came from there, and what it says now.
        said = "\n".join(
            part
            for part in ((proposal.description if proposal else None), texts.get(child))
            if part
        )
        line = _named(said, EPIC_LINE, numbers.get(parent), repository)
        if line:
            notes[child, parent] = Note("declared", evidence=[{"side": "story", "text": line}])
        elif proposal is not None and str(parent) in (proposal.parents or []):
            notes[child, parent] = Note(
                "model", confirmed=True, reason=proposal.review_reason or None
            )
    return notes


def _words(text: str) -> set[str]:
    return {word for word in WORD.findall(text.lower()) if word not in COMMON}


def _texts(db: Session, project: UUID, model: Any, ids: set[UUID]) -> dict[UUID, tuple[str, str]]:
    found: dict[UUID, tuple[str, str]] = {}
    for some in _chunks(sorted(ids)):
        for local, title, body in db.execute(
            select(model.id, model.title, model.description).where(
                model.project_id == project, model.id.in_(some)
            )
        ):
            found[local] = (title or "", (body or "")[:4000])
    return found


def likeness(title: str, body: str, parents: dict[UUID, str]) -> dict[UUID, float]:
    """How alike a record's text is to the text of each of its parents. A word of its
    title counts three times a word of its body, and a word every parent has tells them
    apart less than one that a single parent has."""
    in_title, in_body = _words(title), _words(body)
    words = {parent: _words(text) for parent, text in parents.items()}
    shared: dict[str, int] = defaultdict(int)
    for found in words.values():
        for word in found:
            shared[word] += 1
    return {
        parent: sum(3.0 / shared[word] for word in in_title & found)
        + sum(1.0 / shared[word] for word in (in_body - in_title) & found)
        for parent, found in words.items()
    }


def _order(link: PlanningLink, *, scores: dict[UUID, float]) -> tuple[int, int, float, str]:
    """Which of a child's links comes first as its primary parent."""
    return (
        _RANK.get(link.origin, 3),
        0 if link.strength == "full" else 1,
        -scores.get(link.parent_id, 0.0),
        str(link.parent_id),
    )


def _suggest(
    db: Session, project: UUID, kind: Child, groups: dict[UUID, list[PlanningLink]]
) -> None:
    """Choose a primary parent for each child that has several and none chosen: first by
    how much is known of the link, then by how alike the two texts are."""
    child_model = ProjectUserStory if kind == "stories" else BacklogItem
    parent_model = ProjectEpic if kind == "stories" else ProjectUserStory
    of_children = _texts(db, project, child_model, set(groups))
    of_parents = _texts(
        db, project, parent_model, {link.parent_id for links in groups.values() for link in links}
    )
    for child, links in groups.items():
        title, body = of_children.get(child, ("", ""))
        scores = likeness(
            title,
            body,
            {link.parent_id: " ".join(of_parents.get(link.parent_id, ("", ""))) for link in links},
        )
        best = min(links, key=partial(_order, scores=scores))
        best.is_primary, best.primary_by = True, "suggested"


def _primaries(db: Session, project: UUID, kind: Child, children: set[UUID] | None) -> None:
    """Every child with a parent has exactly one primary parent."""
    spec = _SPECS[kind]
    query = select(PlanningLink).where(
        PlanningLink.project_id == project,
        PlanningLink.kind == spec.name,
        PlanningLink.removed_at.is_(None),
    )
    rows: list[PlanningLink] = []
    if children is None:
        rows = list(db.scalars(query))
    else:
        for some in _chunks(sorted(children)):
            rows.extend(db.scalars(query.where(PlanningLink.child_id.in_(some))))
    groups: dict[UUID, list[PlanningLink]] = defaultdict(list)
    for row in rows:
        groups[row.child_id].append(row)
    undecided: dict[UUID, list[PlanningLink]] = {}
    for child, links in groups.items():
        chosen = [link for link in links if link.is_primary]
        if len(links) == 1:
            only = links[0]
            if only.primary_by != "administrator":
                only.primary_by = "only"
            only.is_primary = True
            continue
        if len(chosen) > 1:
            # One stays: the administrator's choice before any other.
            keep = min(chosen, key=lambda link: (link.primary_by != "administrator", str(link.id)))
            for link in chosen:
                if link is not keep:
                    link.is_primary, link.primary_by = False, None
            chosen = [keep]
        if chosen:
            # It was the only parent and is one of several now: nobody chose it among them.
            if chosen[0].primary_by == "only":
                chosen[0].primary_by = "suggested"
            continue
        undecided[child] = links
    if undecided:
        _suggest(db, project, kind, undecided)


def reconcile(
    db: Session,
    project: ProjectRecord,
    kind: Child,
    children: set[UUID] | None = None,
    *,
    made_by: str | None = None,
) -> int:
    """Bring the records in step with the links of those children, or of the whole project.
    A link without a record gets one; the record of a link that is gone is closed.
    ``made_by`` says who made the new links, when the caller knows; otherwise the stored
    facts are asked. Returns how many records were made or closed."""
    spec = _SPECS[kind]
    links = select(spec.child, spec.parent).where(spec.table.project_id == project.id)
    records = select(PlanningLink).where(
        PlanningLink.project_id == project.id,
        PlanningLink.kind == spec.name,
        PlanningLink.removed_at.is_(None),
    )
    there: set[Pair] = set()
    recorded: dict[Pair, PlanningLink] = {}
    if children is None:
        there = {(child, parent) for child, parent in db.execute(links)}
        recorded = {(row.child_id, row.parent_id): row for row in db.scalars(records)}
    else:
        for some in _chunks(sorted(children)):
            there.update((c, p) for c, p in db.execute(links.where(spec.child.in_(some))))
            recorded.update(
                ((row.child_id, row.parent_id), row)
                for row in db.scalars(records.where(PlanningLink.child_id.in_(some)))
            )
    new = there - recorded.keys()
    gone = [row for pair, row in recorded.items() if pair not in there]
    if not new and not gone:
        return 0
    now = datetime.now(UTC)
    for row in gone:
        row.removed_at, row.is_primary = now, False
    notes = _established(db, project, kind, new) if new and made_by is None else {}
    for child, parent in sorted(new, key=lambda pair: (str(pair[0]), str(pair[1]))):
        note = (
            Note(made_by, confirmed=made_by == "administrator")
            if made_by is not None
            else notes.get((child, parent), Note("unknown"))
        )
        db.add(
            PlanningLink(
                project_id=project.id,
                kind=spec.name,
                child_id=child,
                parent_id=parent,
                origin=note.origin,
                confirmed=note.confirmed,
                reason=note.reason,
                evidence=note.evidence,
                strength=note.strength,
            )
        )
    db.flush()
    _primaries(db, project.id, kind, {child for child, _ in new} | {row.child_id for row in gone})
    return len(new) + len(gone)


def _shown(row: PlanningLink) -> dict[str, Any]:
    made = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    gone = row.removed_at
    return {
        "parent": str(row.parent_id),
        "primary": row.is_primary,
        "primary_by": row.primary_by,
        "origin": row.origin,
        "confirmed": row.confirmed,
        "reason": row.reason,
        "evidence": row.evidence or [],
        "strength": row.strength,
        "recorded_at": made.isoformat(),
        "removed_at": (gone if gone.tzinfo else gone.replace(tzinfo=UTC)).isoformat()
        if gone
        else None,
    }


def explained(row: PlanningLink) -> bool:
    """Whether the record says why the two are linked: the text names the parent, the
    administrator chose it, or a reason is written."""
    return row.origin in ("declared", "administrator") or bool((row.reason or "").strip())


def numbers(db: Session, project: UUID) -> dict[str, int]:
    """For the project's health: how many of its links say why, and how many children
    have a primary parent that nobody chose."""
    rows = db.execute(
        select(
            PlanningLink.origin,
            PlanningLink.reason,
            PlanningLink.is_primary,
            PlanningLink.primary_by,
            PlanningLink.confirmed,
        ).where(PlanningLink.project_id == project, PlanningLink.removed_at.is_(None))
    ).all()
    with_reason = sum(
        1
        for origin, reason, _primary, _by, _confirmed in rows
        if origin in ("declared", "administrator") or (reason or "").strip()
    )
    return {
        "links_recorded": len(rows),
        "links_with_reason": with_reason,
        "links_without_reason": len(rows) - with_reason,
        "links_declared": sum(1 for origin, *_ in rows if origin == "declared"),
        "links_by_administrator": sum(1 for origin, *_ in rows if origin == "administrator"),
        "links_by_model": sum(1 for origin, *_ in rows if origin == "model"),
        "links_unknown": sum(1 for origin, *_ in rows if origin == "unknown"),
        "primaries_suggested": sum(
            1 for _o, _r, primary, by, _c in rows if primary and by == "suggested"
        ),
        "primaries_chosen": sum(
            1 for _o, _r, primary, by, _c in rows if primary and by == "administrator"
        ),
    }


def primary_stories(db: Session, project: UUID) -> dict[UUID, int]:
    """How many User Stories each Epic is the primary Epic of. A Story is counted once,
    under its primary Epic, however many Epics it is linked to."""
    return {
        epic: count
        for epic, count in db.execute(
            select(PlanningLink.parent_id, func.count())
            .join(ProjectUserStory, ProjectUserStory.id == PlanningLink.child_id)
            .where(
                PlanningLink.project_id == project,
                PlanningLink.kind == "story_epic",
                PlanningLink.removed_at.is_(None),
                PlanningLink.is_primary,
                ProjectUserStory.archived_at.is_(None),
            )
            .group_by(PlanningLink.parent_id)
        )
    }


class PlanningLinks:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def _in_step(
        self, name: str, kind: str, children: set[UUID] | None, made_by: str | None = None
    ) -> None:
        try:
            with self.sessions.begin() as db:
                project = active_project(db, name)
                reconcile(db, project, cast(Child, kind), children, made_by=made_by)
        except IntegrityError:
            # Another request recorded the same link at the same moment: it is recorded.
            return

    def reconcile_project(self, name: str) -> None:
        """Bring every record of the project in step with its links."""
        for kind in ("stories", "issues"):
            self._in_step(name, kind, None)

    def before_change(self, name: str, kind: str, child: UUID) -> None:
        """Record what is linked now, before the administrator changes it: what they add
        is then known to be theirs."""
        if kind in _SPECS:
            self._in_step(name, kind, {child})

    def after_change(self, name: str, kind: str, child: UUID) -> None:
        """The links the administrator just made are theirs."""
        if kind in _SPECS:
            self._in_step(name, kind, {child}, made_by="administrator")

    def noted(self, name: str, kind: str, child: UUID, parent: UUID, note: Note) -> None:
        """The link the administrator just approved is recorded with what is known of
        it: who proposed it, why, and the passages that show it."""
        if kind not in _SPECS:
            return
        self._in_step(name, kind, {child}, made_by="administrator")
        if note.origin == "administrator":
            return
        with self.sessions.begin() as db:
            project = active_project(db, name)
            row = db.scalar(
                select(PlanningLink).where(
                    PlanningLink.project_id == project.id,
                    PlanningLink.kind == _SPECS[kind].name,
                    PlanningLink.child_id == child,
                    PlanningLink.parent_id == parent,
                    PlanningLink.removed_at.is_(None),
                )
            )
            if row is not None:
                row.origin, row.confirmed = note.origin, note.confirmed
                row.reason, row.evidence, row.strength = note.reason, note.evidence, note.strength

    def annotate(self, name: str, kind: str, items: list[dict[str, Any]]) -> None:
        """Say of each listed record's parents who linked them and why, and which is the
        primary one. A proposal is no record yet and has none. Of each listed Epic, say
        how many User Stories it is the primary Epic of."""
        if kind == "epics":
            self._in_step(name, "stories", None)
            with self.sessions() as db:
                counts = primary_stories(db, active_project(db, name).id)
            for item in items:
                if not item.get("proposal"):
                    item["primary_story_count"] = counts.get(UUID(str(item["id"])), 0)
            return
        if kind not in _SPECS:
            return
        spec = _SPECS[kind]
        listed = {UUID(str(item["id"])): item for item in items if not item.get("proposal")}
        for item in items:
            item["parent_links"] = []
        if not listed:
            return
        self._in_step(name, kind, set(listed))
        with self.sessions() as db:
            project = active_project(db, name)
            for some in _chunks(sorted(listed)):
                for row in db.scalars(
                    select(PlanningLink)
                    .where(
                        PlanningLink.project_id == project.id,
                        PlanningLink.kind == spec.name,
                        PlanningLink.removed_at.is_(None),
                        PlanningLink.child_id.in_(some),
                    )
                    .order_by(
                        PlanningLink.is_primary.desc(), PlanningLink.created_at, PlanningLink.id
                    )
                ):
                    listed[row.child_id]["parent_links"].append(_shown(row))

    def history(self, name: str, kind: str, child: UUID) -> dict[str, Any]:
        """Every link the record has and had to a parent, the current ones first."""
        if kind not in _SPECS:
            raise ProjectInvalid("An Epic has no parent")
        spec = _SPECS[kind]
        self._in_step(name, kind, {child})
        parent_model = ProjectEpic if kind == "stories" else ProjectUserStory
        with self.sessions() as db:
            project = active_project(db, name)
            rows = db.scalars(
                select(PlanningLink)
                .where(
                    PlanningLink.project_id == project.id,
                    PlanningLink.kind == spec.name,
                    PlanningLink.child_id == child,
                )
                .order_by(
                    PlanningLink.removed_at.is_not(None),
                    PlanningLink.is_primary.desc(),
                    PlanningLink.created_at.desc(),
                )
            ).all()
            titles = {
                local: title
                for local, title in db.execute(
                    select(parent_model.id, parent_model.title).where(
                        parent_model.project_id == project.id,
                        parent_model.id.in_({row.parent_id for row in rows}),
                    )
                )
            }
            return {"items": [{**_shown(row), "title": titles.get(row.parent_id)} for row in rows]}

    def set_primary(
        self, name: str, kind: str, child: UUID, parent: UUID, why: Note | None = None
    ) -> dict[str, Any]:
        """The administrator's choice of the record's primary parent, among its parents.
        ``why`` is what a review said for it, when the choice approves a review's answer:
        its reason and passages are kept on the link."""
        if kind not in _SPECS:
            raise ProjectInvalid("An Epic has no parent")
        spec = _SPECS[kind]
        self._in_step(name, kind, {child})
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            rows = db.scalars(
                select(PlanningLink).where(
                    PlanningLink.project_id == project.id,
                    PlanningLink.kind == spec.name,
                    PlanningLink.child_id == child,
                    PlanningLink.removed_at.is_(None),
                )
            ).all()
            if not rows:
                raise ProjectNotFound(str(child))
            chosen = next((row for row in rows if row.parent_id == parent), None)
            if chosen is None:
                raise ProjectInvalid("Choose one of the record's own parents")
            for row in rows:
                row.is_primary, row.primary_by = (
                    row is chosen,
                    "administrator" if row is chosen else None,
                )
            chosen.confirmed = True
            if why is not None and (why.reason or why.evidence):
                chosen.reason, chosen.evidence = why.reason, why.evidence
            db.flush()
            return {
                "id": str(child),
                "parent": str(parent),
                "record": str(child),
                "parent_links": [
                    _shown(row)
                    for row in sorted(rows, key=lambda row: (not row.is_primary, row.created_at))
                ],
            }

    def with_links(self) -> list[str]:
        """The projects that have links, for whoever keeps their records in step."""
        with self.sessions() as db:
            linked = select(ProjectIssueStory.project_id).union(select(ProjectStoryEpic.project_id))
            return list(
                db.scalars(
                    select(ProjectRecord.name)
                    .where(ProjectRecord.archived_at.is_(None), ProjectRecord.id.in_(linked))
                    .order_by(ProjectRecord.name)
                )
            )
