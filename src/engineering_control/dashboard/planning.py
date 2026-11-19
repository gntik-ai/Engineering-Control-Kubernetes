"""Project-scoped planning records and their parent relationships."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import (
    Select,
    String,
    Subquery,
    Uuid,
    delete,
    false,
    func,
    literal,
    null,
    select,
    true,
    union_all,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from engineering_control.dashboard.backlog_items import clean_description, clean_title
from engineering_control.dashboard.connection_records import connected_records, finding_links
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
    WorkspaceCandidate,
    WorkspaceImport,
)

Kind = Literal["epics", "stories", "issues"]


def replace_parents(
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
    row.updated_at = datetime.now(UTC)
    row.title = clean_title(title)
    row.description = clean_description(description)
    session.add(row)
    session.flush()
    if kind != "epics":
        replace_parents(session, kind, project.id, row.id, parents)
    return row.id


def _visible_issues(project: UUID) -> ColumnElement[bool]:
    return BacklogItem.workspace_review.in_(
        ("pending", "publishing", "approved")
    ) | BacklogItem.id.in_(
        select(ProjectIssueStory.item_id).where(ProjectIssueStory.project_id == project)
    )


def _parent_links(
    kind: Kind,
) -> tuple[
    InstrumentedAttribute[UUID],
    InstrumentedAttribute[UUID],
    type[ProjectStoryEpic] | type[ProjectIssueStory],
]:
    if kind == "stories":
        return ProjectStoryEpic.story_id, ProjectStoryEpic.epic_id, ProjectStoryEpic
    return ProjectIssueStory.item_id, ProjectIssueStory.story_id, ProjectIssueStory


def _parent_options(project: UUID, kind: str) -> Select[tuple[UUID, str]]:
    parent = ProjectEpic if kind == "stories" else ProjectUserStory
    return (
        select(parent.id, parent.title)
        .where(parent.project_id == project, parent.archived_at.is_(None))
        .order_by(parent.created_at, parent.id)
    )


def coverage_problem(session: Session, project: UUID, stage: str) -> str | None:
    if stage == "epics":
        if not session.scalar(
            select(ProjectEpic.id)
            .where(ProjectEpic.project_id == project, ProjectEpic.archived_at.is_(None))
            .limit(1)
        ):
            return "Approve or add an Epic before generating User Stories"
        return None
    parent, links, foreign_key = (
        (ProjectEpic, ProjectStoryEpic, ProjectStoryEpic.epic_id)
        if stage == "stories"
        else (ProjectUserStory, ProjectIssueStory, ProjectIssueStory.story_id)
    )
    missing = session.scalar(
        select(func.count())
        .select_from(parent)
        .where(
            parent.project_id == project,
            parent.archived_at.is_(None),
            parent.id.not_in(select(foreign_key).where(links.project_id == project)),
        )
    )
    if missing:
        label = "Epic" if stage == "stories" else "User Story"
        return (
            f"{missing} {label} records still need coverage. Add proposals or adjust their links."
        )
    return None


def _table_rows(project: UUID, kind: Kind) -> Subquery:
    proposals = (
        select(
            WorkspaceCandidate.id,
            WorkspaceCandidate.title,
            WorkspaceCandidate.description,
            WorkspaceCandidate.created_at,
            true().label("proposal"),
            literal("proposal").label("review"),
            WorkspaceCandidate.parents,
            WorkspaceCandidate.number,
            null().label("github_state"),
        )
        .outerjoin(WorkspaceImport, WorkspaceImport.project_id == WorkspaceCandidate.project_id)
        .where(
            WorkspaceCandidate.project_id == project,
            WorkspaceCandidate.kind == kind,
            WorkspaceCandidate.decision == "pending",
            (WorkspaceImport.stage == kind) | (WorkspaceCandidate.origin == "connection"),
        )
    )
    saved: Select[Any]
    if kind == "issues":
        saved = select(
            BacklogItem.id,
            BacklogItem.title,
            BacklogItem.description,
            BacklogItem.created_at,
            false(),
            BacklogItem.workspace_review,
            null(),
            BacklogItem.issue_number,
            BacklogItem.issue_state,
        ).where(BacklogItem.project_id == project, _visible_issues(project))
    else:
        model = ProjectEpic if kind == "epics" else ProjectUserStory
        saved = select(
            model.id,
            model.title,
            model.description,
            model.created_at,
            false(),
            literal("approved"),
            null(),
            null(),
            null(),
        ).where(model.project_id == project, model.archived_at.is_(None))
    return union_all(proposals, saved).subquery()


def _search_matches(
    session: Session, rows: Subquery, project: UUID, kind: Kind, search: str
) -> ColumnElement[bool]:
    if not search:
        return true()
    match = (
        rows.c.title.icontains(search, autoescape=True)
        | rows.c.description.icontains(search, autoescape=True)
        | (sql_cast(rows.c.number, String) == search.removeprefix("#"))
    )
    if kind == "epics":
        return match

    parent = ProjectEpic if kind == "stories" else ProjectUserStory
    parents = select(parent.id).where(
        parent.project_id == project,
        parent.archived_at.is_(None),
        parent.title.icontains(search, autoescape=True),
    )
    child, parent_id, link = _parent_links(kind)
    saved = select(child).where(link.project_id == project, parent_id.in_(parents))

    # Saved records have link rows. Pending import proposals still carry a JSON array
    # of UUID strings. Match both against active parents before paging the results.
    if session.get_bind().dialect.name == "postgresql":
        entries = func.json_array_elements_text(rows.c.parents).table_valued("value")
        value: ColumnElement[Any] = sql_cast(entries.c.value, Uuid)
    else:
        entries = func.json_each(rows.c.parents).table_valued("value")
        # SQLAlchemy stores SQLite UUID columns as 32 hex digits without hyphens.
        value = func.replace(entries.c.value, "-", "")
    proposed = select(1).select_from(entries).where(value.in_(parents)).correlate(rows).exists()
    return match | (~rows.c.proposal & rows.c.id.in_(saved)) | (rows.c.proposal & proposed)


class PlanningRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @contextmanager
    def _snapshot(self) -> Iterator[Session]:
        # Counts, version and rows must describe the same instant when an approval
        # moves a candidate into the saved records while a page is being fetched.
        with self.sessions() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            elif session.get_bind().dialect.name == "sqlite":
                # sqlite3's legacy transaction mode does not start a transaction on SELECT.
                session.connection().exec_driver_sql("BEGIN")
            yield session

    def page(
        self,
        name: str,
        kind: Kind,
        page: int,
        page_size: int,
        search: str = "",
        epic: UUID | None = None,
        record: UUID | None = None,
        connected: str = "",
    ) -> dict[str, Any]:
        with self._snapshot() as session:
            project = active_project(session, name)
            rows = _table_rows(project.id, kind)
            epic = epic if kind == "epics" else None
            focus = record or epic
            search = "" if focus else search.strip()
            matches = _search_matches(session, rows, project.id, kind, search)
            if search:
                matches = matches | (
                    ~rows.c.proposal
                    & rows.c.id.in_(connected_records(project.id, kind, search, exact=False))
                )
            if connected and not focus:
                matches = (
                    matches
                    & ~rows.c.proposal
                    & rows.c.id.in_(connected_records(project.id, kind, connected, exact=True))
                )
            ordering = (rows.c.proposal.desc(), rows.c.created_at, rows.c.id)
            # Approval and stage progression always refer to the entire table.
            total, pending, proposed = session.execute(
                select(
                    func.count().filter(matches),
                    func.count().filter(rows.c.review.in_(("proposal", "pending"))),
                    func.count().filter(rows.c.proposal),
                ).select_from(rows)
            ).one()
            pages = max(1, (total + page_size - 1) // page_size)
            requested_page = page
            page = min(page, pages)
            if focus:
                ranked = select(
                    rows.c.id,
                    rows.c.proposal,
                    func.row_number().over(order_by=ordering).label("position"),
                ).subquery()
                position = session.scalar(
                    select(ranked.c.position).where(ranked.c.id == focus, ~ranked.c.proposal)
                )
                if position is not None:
                    page = (position - 1) // page_size + 1

            items = [
                dict(record)
                for record in session.execute(
                    select(rows)
                    .where(matches)
                    .order_by(*ordering)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                ).mappings()
            ]
            linked: dict[UUID, list[str]] = defaultdict(list)
            parents: dict[str, str] = {}
            has_parents = False
            if kind != "epics":
                options = _parent_options(project.id, kind)
                has_parents = session.execute(options.limit(1)).first() is not None
                child, parent, link = _parent_links(kind)
                saved_ids = [item["id"] for item in items if not item["proposal"]]
                if saved_ids:
                    for child_id, parent_id in session.execute(
                        select(child, parent)
                        .where(link.project_id == project.id, child.in_(saved_ids))
                        .order_by(parent)
                    ):
                        linked[child_id].append(str(parent_id))
                ids = {
                    UUID(uid) for item in items for uid in (item["parents"] or linked[item["id"]])
                }
                if ids:
                    parent_model = ProjectEpic if kind == "stories" else ProjectUserStory
                    parents = {
                        str(uid): title
                        for uid, title in session.execute(options.where(parent_model.id.in_(ids)))
                    }
            external: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
            for uid, badge in finding_links(
                session, project.id, kind, [i["id"] for i in items if not i["proposal"]]
            ):
                external[uid].append(badge)
            for item in items:
                key = item["id"]
                item["connections"] = external[key]
                rels = item.pop("parents")
                item.pop("created_at")
                if kind != "epics":
                    item["epic_ids" if kind == "stories" else "story_ids"] = rels or linked[key]
                item["id"] = str(key)
                item["url"] = (
                    f"https://github.com/{project.source_repository}/issues/{item['number']}"
                    if item["number"]
                    else None
                )
            review = session.execute(
                select(
                    WorkspaceImport.version,
                    WorkspaceImport.stage,
                    WorkspaceImport.lease,
                    WorkspaceImport.lease_until,
                    WorkspaceImport.progress["review_mode"].as_string().label("mode"),
                ).where(WorkspaceImport.project_id == project.id)
            ).one_or_none()
            current = review is not None and review.stage == kind
            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "requested_page": requested_page,
                "search": search,
                "epic": str(epic) if epic else None,
                "record": str(record) if record else None,
                "connected": connected,
                "pages": pages,
                "pending": pending,
                "parents": parents,
                "has_parents": has_parents,
                "version": review.version if review else None,
                "busy": bool(
                    current
                    and review
                    and review.lease
                    and review.lease_until
                    and review.lease_until.replace(tzinfo=UTC) > datetime.now(UTC)
                ),
                "can_continue": bool(
                    current
                    and review
                    and review.mode in ("gaps", "review")
                    and not proposed
                    and not coverage_problem(session, project.id, kind)
                ),
            }

    def counts(self, name: str) -> dict[str, int]:
        with self._snapshot() as session:
            project = active_project(session, name)
            kinds: tuple[Kind, ...] = ("epics", "stories", "issues")
            counts = select(
                *(
                    select(func.count())
                    .select_from(_table_rows(project.id, kind))
                    .scalar_subquery()
                    .label(kind)
                    for kind in kinds
                )
            )
            return dict(session.execute(counts).mappings().one())

    def pending(self, name: str, kind: Kind) -> dict[str, Any]:
        # Open the bulk confirmation with just the IDs currently awaiting approval.
        with self._snapshot() as session:
            project = active_project(session, name)
            rows = _table_rows(project.id, kind)
            ids = session.scalars(
                select(rows.c.id)
                .where(rows.c.review.in_(("proposal", "pending")))
                .order_by(rows.c.proposal.desc(), rows.c.created_at, rows.c.id)
            )
            return {
                "ids": [str(key) for key in ids],
                "version": session.scalar(
                    select(WorkspaceImport.version).where(WorkspaceImport.project_id == project.id)
                ),
            }

    def parents(self, name: str, kind: Kind) -> list[dict[str, str]]:
        with self.sessions() as session:
            project = active_project(session, name)
            if kind == "epics":
                return []
            return [
                {"id": str(uid), "title": title}
                for uid, title in session.execute(_parent_options(project.id, kind))
            ]

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
                    _visible_issues(project.id),
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
