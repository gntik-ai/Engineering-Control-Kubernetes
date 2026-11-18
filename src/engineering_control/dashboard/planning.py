"""Project-scoped planning records and their parent relationships."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import clean_description, clean_title
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import (
    BacklogItem,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
)

Kind = Literal["epics", "stories", "issues"]


def _replace_parents(
    session: Session, kind: Kind, project: UUID, key: UUID, parents: list[UUID]
) -> None:
    parent_model = ProjectEpic if kind == "stories" else ProjectUserStory
    expected = set(parents)
    if not expected or len(expected) != len(parents):
        raise ProjectInvalid("Choose one or more distinct parents")
    valid = set(
        session.scalars(
            select(parent_model.id).where(
                parent_model.project_id == project,
                parent_model.archived_at.is_(None),
                parent_model.id.in_(expected),
            )
        )
    )
    if expected != valid:
        raise ProjectInvalid("Every parent must be active and belong to this project")
    if kind == "stories":
        session.execute(
            delete(ProjectStoryEpic).where(
                ProjectStoryEpic.project_id == project, ProjectStoryEpic.story_id == key
            )
        )
        session.add_all(
            ProjectStoryEpic(project_id=project, story_id=key, epic_id=p) for p in parents
        )
    else:
        session.execute(
            delete(ProjectIssueStory).where(
                ProjectIssueStory.project_id == project, ProjectIssueStory.item_id == key
            )
        )
        session.add_all(
            ProjectIssueStory(project_id=project, item_id=key, story_id=p) for p in parents
        )


def save_record(
    session: Session,
    project: ProjectRecord,
    kind: Kind,
    title: str,
    description: str | None,
    parents: list[UUID],
    key: UUID | None = None,
) -> UUID:
    row: ProjectEpic | ProjectUserStory | BacklogItem
    if kind == "issues":
        if key is None:
            row = BacklogItem(
                project_id=project.id, source="manual", state="proposed", workspace_review="pending"
            )
        else:
            issue = session.scalar(
                select(BacklogItem).where(
                    BacklogItem.project_id == project.id, BacklogItem.id == key
                )
            )
            if issue is None:
                raise ProjectNotFound(str(key))
            if issue.workspace_review != "pending":
                raise ProjectConflict("Only a pending proposal can be edited")
            row = issue
    else:
        model = ProjectEpic if kind == "epics" else ProjectUserStory
        if key is None:
            row = model(project_id=project.id)
        else:
            saved = session.scalar(
                select(model).where(
                    model.id == key, model.project_id == project.id, model.archived_at.is_(None)
                )
            )
            if saved is None:
                raise ProjectNotFound(str(key))
            row = cast(ProjectEpic | ProjectUserStory, saved)
    row.title = clean_title(title)
    row.description = clean_description(description)
    session.add(row)
    session.flush()
    if kind != "epics":
        _replace_parents(session, kind, project.id, row.id, parents)
    return row.id


class PlanningRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def listing(self, name: str) -> dict[str, Any]:
        with self.sessions() as session:
            project = active_project(session, name)
            epic_ids: dict[UUID, list[str]] = defaultdict(list)
            for story, epic in session.execute(
                select(ProjectStoryEpic.story_id, ProjectStoryEpic.epic_id)
                .where(ProjectStoryEpic.project_id == project.id)
                .order_by(ProjectStoryEpic.epic_id)
            ):
                epic_ids[story].append(str(epic))
            story_ids: dict[UUID, list[str]] = defaultdict(list)
            for item, story in session.execute(
                select(ProjectIssueStory.item_id, ProjectIssueStory.story_id)
                .where(ProjectIssueStory.project_id == project.id)
                .order_by(ProjectIssueStory.story_id)
            ):
                story_ids[item].append(str(story))
            epics = session.scalars(
                select(ProjectEpic)
                .where(ProjectEpic.project_id == project.id, ProjectEpic.archived_at.is_(None))
                .order_by(ProjectEpic.created_at, ProjectEpic.id)
            )
            stories = session.scalars(
                select(ProjectUserStory)
                .where(
                    ProjectUserStory.project_id == project.id,
                    ProjectUserStory.archived_at.is_(None),
                )
                .order_by(ProjectUserStory.created_at, ProjectUserStory.id)
            )
            issues = session.scalars(
                select(BacklogItem)
                .where(
                    BacklogItem.project_id == project.id,
                    (BacklogItem.workspace_review.in_(("pending", "publishing", "approved")))
                    | BacklogItem.id.in_(
                        select(ProjectIssueStory.item_id).where(
                            ProjectIssueStory.project_id == project.id
                        )
                    ),
                )
                .order_by(BacklogItem.created_at, BacklogItem.id)
            )
            return {
                "epics": [
                    {"id": str(e.id), "title": e.title, "description": e.description} for e in epics
                ],
                "stories": [
                    {
                        "id": str(s.id),
                        "title": s.title,
                        "description": s.description,
                        "epic_ids": epic_ids[s.id],
                    }
                    for s in stories
                ],
                "issues": [
                    {
                        "id": str(i.id),
                        "title": i.title,
                        "description": i.description,
                        "story_ids": story_ids[i.id],
                        "review": i.workspace_review,
                        "number": i.issue_number,
                        "github_state": i.issue_state,
                        "url": f"https://github.com/{project.source_repository}/issues/{i.issue_number}"
                        if i.issue_number
                        else None,
                    }
                    for i in issues
                ],
            }

    def save(
        self,
        name: str,
        kind: Kind,
        title: str,
        description: str | None,
        parents: list[UUID],
        key: UUID | None = None,
    ) -> dict[str, str]:
        with self.sessions.begin() as session:
            project = active_project(session, name, lock=True)
            identifier = save_record(session, project, kind, title, description, parents, key)
            return {"project": name, "id": str(identifier)}

    def remove(self, name: str, kind: Literal["epics", "stories"], key: UUID) -> dict[str, str]:
        with self.sessions.begin() as session:
            project = active_project(session, name, lock=True)
            model = ProjectEpic if kind == "epics" else ProjectUserStory
            row = session.scalar(
                select(model).where(
                    model.project_id == project.id, model.id == key, model.archived_at.is_(None)
                )
            )
            if row is None:
                raise ProjectNotFound(str(key))
            links: type[ProjectStoryEpic] | type[ProjectIssueStory]
            if kind == "epics":
                links, parent, child = (
                    ProjectStoryEpic,
                    ProjectStoryEpic.epic_id,
                    ProjectStoryEpic.story_id,
                )
            else:
                links, parent, child = (
                    ProjectIssueStory,
                    ProjectIssueStory.story_id,
                    ProjectIssueStory.item_id,
                )
            other_parents = select(child).where(links.project_id == project.id, parent != key)
            orphan = session.scalar(
                select(child)
                .where(links.project_id == project.id, parent == key, child.not_in(other_parents))
                .limit(1)
            )
            if orphan:
                raise ProjectConflict(
                    "Reassign the linked story or issue before removing its last parent"
                )
            session.execute(delete(links).where(links.project_id == project.id, parent == key))
            if kind == "stories":
                session.execute(
                    delete(ProjectStoryEpic).where(
                        ProjectStoryEpic.project_id == project.id, ProjectStoryEpic.story_id == key
                    )
                )
            cast(ProjectEpic | ProjectUserStory, row).archived_at = datetime.now(UTC)
            return {"project": name, "id": str(key)}
