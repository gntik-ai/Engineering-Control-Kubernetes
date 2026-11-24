"""Bugs stand apart from the plan.

A bug reports that something built is wrong. It is not a piece of a User Story's work, so
it belongs to no User Story and, through none, to no Epic: it is flagged as a bug, listed
on its own and handled on its own. Nothing the platform does links a bug to a User Story,
and no review looks for one.

A bug can still carry a link from before it was known as a bug. Those are listed here, and
taken off by the administrator's word, one at a time or all at once. An issue wrongly
taken for a bug is put right by saying it is an enhancement; it keeps its User Stories.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.planning import PlanningRecords, _visible_issues
from engineering_control.dashboard.planning_links import PlanningLinks
from engineering_control.dashboard.project_records import active_project
from engineering_control.db.models import (
    BacklogItem,
    PlanningLink,
    ProjectIssueStory,
    ProjectRecord,
    ProjectUserStory,
)

# How many linked bugs one reading lists. All of them are counted, and unlinked at once.
LISTED = 200


def _linked(db: Session, project: ProjectRecord) -> dict[UUID, list[UUID]]:
    """The bugs that are linked to a User Story, each with those Stories."""
    found: dict[UUID, list[UUID]] = defaultdict(list)
    for item, story in db.execute(
        select(ProjectIssueStory.item_id, ProjectIssueStory.story_id)
        .join(BacklogItem, BacklogItem.id == ProjectIssueStory.item_id)
        .where(
            ProjectIssueStory.project_id == project.id,
            BacklogItem.project_id == project.id,
            _visible_issues(project.id),
            BacklogItem.workspace_review.is_distinct_from("planning"),
            BacklogItem.id.in_(bugs_with_optional_stories(db, project.id)),
        )
        .order_by(ProjectIssueStory.item_id, ProjectIssueStory.story_id)
    ):
        found[item].append(story)
    return dict(found)


class Bugs:
    def __init__(
        self, sessions: sessionmaker[Session], planning: PlanningRecords, links: PlanningLinks
    ) -> None:
        self.sessions = sessions
        self.planning = planning
        self.links = links

    def linked(self, name: str) -> dict[str, Any]:
        """The bugs still linked to a User Story, with who linked each and why."""
        self.links.reconcile_project(name)
        with self.sessions() as db:
            project = active_project(db, name)
            linked = _linked(db, project)
            shown = sorted(linked, key=str)[:LISTED]
            items = {
                row.id: row
                for row in db.scalars(select(BacklogItem).where(BacklogItem.id.in_(shown)))
            }
            stories = {
                key: title
                for key, title in db.execute(
                    select(ProjectUserStory.id, ProjectUserStory.title).where(
                        ProjectUserStory.project_id == project.id,
                        ProjectUserStory.id.in_({s for key in shown for s in linked[key]}),
                    )
                )
            }
            origins = {
                (child, parent): origin
                for child, parent, origin in db.execute(
                    select(
                        PlanningLink.child_id, PlanningLink.parent_id, PlanningLink.origin
                    ).where(
                        PlanningLink.project_id == project.id,
                        PlanningLink.kind == "issue_story",
                        PlanningLink.removed_at.is_(None),
                        PlanningLink.child_id.in_(shown),
                    )
                )
            }
            rows = sorted(
                (items[key] for key in shown if key in items),
                key=lambda row: (row.issue_state == "closed", row.issue_number or 0, str(row.id)),
            )
            return {
                "bugs": len(linked),
                "links": sum(len(parents) for parents in linked.values()),
                "listed": len(rows),
                "items": [
                    {
                        "id": str(row.id),
                        "number": row.issue_number,
                        "title": row.title,
                        "state": "closed" if row.issue_state == "closed" else "open",
                        "confirmed": bool(row.type_confirmed and row.execution_type == "bug"),
                        "stories": [
                            {
                                "id": str(story),
                                "title": stories.get(story, ""),
                                "origin": origins.get((row.id, story), "unknown"),
                            }
                            for story in linked[row.id]
                        ],
                    }
                    for row in rows
                ],
            }

    def unlink(self, name: str, items: list[UUID] | None = None) -> dict[str, Any]:
        """Take those bugs, or every bug, off the User Stories they are linked to. The
        issue's type is not touched, and nothing is written on GitHub."""
        with self.sessions() as db:
            linked = _linked(db, active_project(db, name))
        chosen = [key for key in sorted(linked, key=str) if items is None or key in set(items)]
        links = 0
        for key in chosen:
            self.links.before_change(name, "issues", key)
            self.planning.link_stories(name, key, [])
            self.links.after_change(name, "issues", key)
            links += len(linked[key])
        return {"unlinked": len(chosen), "links": links}
