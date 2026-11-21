"""Planning identities and original GitHub evidence shared by connection review and navigation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session

from engineering_control.db.models import (
    BacklogItem,
    ConnectionFinding,
    ProjectConnection,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    WorkspaceCandidate,
    WorkspaceImport,
    WorkspaceSource,
)

KINDS = ("epics", "stories", "issues")
RECORD_MODELS: dict[str, type[ProjectEpic] | type[ProjectUserStory] | type[BacklogItem]] = {
    "epics": ProjectEpic,
    "stories": ProjectUserStory,
    "issues": BacklogItem,
}
REFERENCE = re.compile(
    r"(?<![\w./-])(?:https?://github\.com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)#)([1-9]\d{0,9})(?![\w/])",
    re.I,
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def counterpart(connection: ProjectConnection, project: UUID) -> UUID:
    return connection.second_id if project == connection.first_id else connection.first_id


@dataclass
class Item:
    project: UUID
    kind: str
    id: UUID | None
    number: int | None
    title: str
    body: str
    approved: bool = False
    parents: list[str] = field(default_factory=list)
    original: str = ""

    @property
    def key(self) -> str:
        return str(self.id) if self.id else f"github-{self.number}"

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "kind": self.kind,
            "title": self.title,
            "description": self.body
            if len(self.body) <= 1200
            else self.body[:800] + " … " + self.body[-390:],
            "revision": digest([self.body, self.original]),
            "parents": sorted(self.parents),
            "number": self.number,
        }


@dataclass
class Catalog:
    project: ProjectRecord
    local: dict[UUID, Item] = field(default_factory=dict)
    numbers: dict[int, Item] = field(default_factory=dict)

    def find(self, key: UUID | None, number: int | None) -> Item | None:
        if key and key in self.local:
            return self.local[key]
        return self.numbers.get(number) if number else None

    def items(self) -> list[Item]:
        return [*self.local.values(), *(item for item in self.numbers.values() if not item.id)]


def catalog(session: Session, project: ProjectRecord) -> Catalog:
    result = Catalog(project)
    for kind, model in (("epics", ProjectEpic), ("stories", ProjectUserStory)):
        for key, title, body in session.execute(
            select(model.id, model.title, model.description).where(
                model.project_id == project.id, model.archived_at.is_(None)
            )
        ):
            result.local[key] = Item(project.id, kind, key, None, title, body or "", True)
    for key, title, body, number, repo, review in session.execute(
        select(
            BacklogItem.id,
            BacklogItem.title,
            BacklogItem.description,
            BacklogItem.issue_number,
            BacklogItem.repository,
            BacklogItem.workspace_review,
        ).where(BacklogItem.project_id == project.id)
    ):
        if review == "planning" or (
            not number and review not in ("approved", "publishing", "pending")
        ):
            continue
        if number and (repo or "").lower() != project.source_repository.lower():
            continue
        visible = review in ("pending", "publishing", "approved")
        item = Item(
            project.id,
            "issues",
            key if visible else None,
            number,
            title,
            body or "",
            review in ("approved", "publishing"),
        )
        if visible:
            result.local[key] = item
        if number:
            result.numbers[number] = item
    for key, number, kind in session.execute(
        select(
            WorkspaceCandidate.local_id, WorkspaceCandidate.number, WorkspaceCandidate.kind
        ).where(
            WorkspaceCandidate.project_id == project.id,
            WorkspaceCandidate.decision == "approved",
            WorkspaceCandidate.number.is_not(None),
        )
    ):
        # A reclassified technical issue owns its GitHub number. Keep the historic
        # local planning record and its existing relationships independently.
        if kind in ("epics", "stories") and number in result.numbers:
            continue
        if key in result.local and number:
            result.local[key].number = number
            result.numbers[number] = result.local[key]
    for link, child, parent in (
        (ProjectStoryEpic, ProjectStoryEpic.story_id, ProjectStoryEpic.epic_id),
        (ProjectIssueStory, ProjectIssueStory.item_id, ProjectIssueStory.story_id),
    ):
        for key, parent_key in session.execute(
            select(child, parent).where(link.project_id == project.id)
        ):
            if key in result.local:
                result.local[key].parents.append(str(parent_key))
    for (evidence,) in session.execute(
        select(WorkspaceSource.evidence).where(
            WorkspaceSource.project_id == project.id, WorkspaceSource.kind == "issue"
        )
    ):
        number = evidence.get("number")
        if type(number) is not int or number <= 0:
            continue
        body = str(evidence.get("body") or "")
        if number in result.numbers:
            result.numbers[number].original = body
        else:
            result.numbers[number] = Item(
                project.id, "issues", None, number, str(evidence.get("title") or f"#{number}"), body
            )
    return result


def watermark(session: Session, project: ProjectRecord) -> str:
    # Import progress covers source insertions. Counts also detect hard removals.
    marks: list[Any] = [project.source_repository, project.description, project.archived_at]
    marks.append(
        tuple(
            session.execute(
                select(WorkspaceImport.stage, WorkspaceImport.version).where(
                    WorkspaceImport.project_id == project.id
                )
            ).one_or_none()
            or ()
        )
    )
    for table in (ProjectEpic, ProjectUserStory, BacklogItem, WorkspaceCandidate):
        marks.append(
            tuple(
                session.execute(
                    select(func.count(), func.max(table.updated_at)).where(
                        table.project_id == project.id
                    )
                ).one()
            )
        )
    return digest(marks)


def link_identity(source: Item, target: Item | None, number: int | None = None) -> str:
    def endpoint(item: Item) -> str:
        return f"#{item.number}" if item.number else item.key

    return digest(
        [
            "link",
            source.project,
            endpoint(source),
            endpoint(target) if target else f"#{number}" if number else "project",
        ]
    )


def connected_records(
    project: UUID, kind: str, value: str, *, exact: bool
) -> Select[tuple[UUID | None]]:
    f, c, p = ConnectionFinding, ProjectConnection, ProjectRecord
    outgoing = f.source_project == project
    peer = case((c.first_id == project, c.second_id), else_=c.first_id)
    match = (
        p.name == value
        if exact
        else (
            p.name.icontains(value, autoescape=True)
            | p.display_name.icontains(value, autoescape=True)
        )
    )
    return (
        select(case((outgoing, f.source_id), else_=f.target_id))
        .join_from(f, c, c.id == f.connection_id)
        .join(p, p.id == peer)
        .where(
            (c.first_id == project) | (c.second_id == project),
            c.archived_at.is_(None),
            p.archived_at.is_(None),
            (f.action == "link") | ((f.action == "create") & f.target_id.is_not(None)),
            f.status.in_(("approved", "pending")),
            case((outgoing, f.source_kind), else_=f.target_kind) == kind,
            match,
        )
    )


def connected_story_issues(
    project: UUID, stories: Select[tuple[UUID]]
) -> Select[tuple[UUID | None, UUID]]:
    """Approved issue links to these local Stories on either side of a live connection."""
    f, c = ConnectionFinding, ProjectConnection
    outgoing = f.source_project == project
    peer = case((c.first_id == project, c.second_id), else_=c.first_id)
    story_id = case((outgoing, f.source_id), else_=f.target_id)
    issue_id = case((outgoing, f.target_id), else_=f.source_id)
    return (
        select(story_id.label("story_id"), BacklogItem.id.label("issue_id"))
        .join_from(f, c, c.id == f.connection_id)
        .join(ProjectRecord, ProjectRecord.id == peer)
        .join(BacklogItem, (BacklogItem.id == issue_id) & (BacklogItem.project_id == peer))
        .where(
            (c.first_id == project) | (c.second_id == project),
            c.archived_at.is_(None),
            ProjectRecord.archived_at.is_(None),
            BacklogItem.workspace_review.is_distinct_from("planning"),
            f.status == "approved",
            f.action.in_(("link", "create")),
            (
                outgoing
                & (f.source_kind == "stories")
                & (f.target_kind == "issues")
                & f.source_id.in_(stories)
            )
            | (
                (f.source_project == peer)
                & (f.target_kind == "stories")
                & (f.source_kind == "issues")
                & f.target_id.in_(stories)
            ),
        )
    )


def finding_links(
    session: Session, project: UUID, kind: str, keys: list[UUID]
) -> list[tuple[UUID, dict[str, Any]]]:
    """Return visible endpoint badges for records on either side of each active connection."""
    if not keys:
        return []
    pairs = list(
        session.execute(
            select(ConnectionFinding, ProjectConnection)
            .join(ProjectConnection, ProjectConnection.id == ConnectionFinding.connection_id)
            .where(
                ProjectConnection.archived_at.is_(None),
                (ProjectConnection.first_id == project) | (ProjectConnection.second_id == project),
                (ConnectionFinding.source_id.in_(keys) | ConnectionFinding.target_id.in_(keys)),
                (ConnectionFinding.action == "link")
                | (
                    (ConnectionFinding.action == "create")
                    & ConnectionFinding.target_id.is_not(None)
                ),
                ConnectionFinding.status.in_(("pending", "approved")),
            )
        )
    )
    if not pairs:
        return []
    peers = {
        p.id: p
        for p in session.scalars(
            select(ProjectRecord).where(
                ProjectRecord.id.in_({counterpart(c, project) for _, c in pairs}),
                ProjectRecord.archived_at.is_(None),
            )
        )
    }
    links = []
    for f, c in pairs:
        peer = peers.get(counterpart(c, project))
        if not peer:
            continue
        outgoing = project == f.source_project
        local_key, local_kind = (
            (f.source_id, f.source_kind) if outgoing else (f.target_id, f.target_kind)
        )
        if not local_key or local_kind != kind:
            continue
        target, target_kind = (
            (f.target_id, f.target_kind) if outgoing else (f.source_id, f.source_kind)
        )
        links.append(
            (
                local_key,
                {
                    "connection": str(c.id),
                    "project": peer.name,
                    "display_name": peer.display_name,
                    "record": str(target) if target else None,
                    "kind": target_kind,
                    "status": f.status,
                    "title": f.evidence.get("target_title" if outgoing else "source_title") or None,
                },
            )
        )
    return links


def explicit_references(source: Item, repository: str) -> dict[int, str]:
    found: dict[int, str] = {}
    for line in (source.body + "\n" + source.original).splitlines():
        for match in REFERENCE.finditer(line):
            if (match[1] or match[2]).casefold() == repository.casefold():
                number = int(match[3])
                excerpt = line[max(0, match.start() - 120) : match.end() + 120].strip()
                found.setdefault(number, excerpt)
    return found
