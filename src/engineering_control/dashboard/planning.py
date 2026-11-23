"""Project-scoped planning records and their parent relationships."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    Select,
    String,
    Subquery,
    Uuid,
    case,
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
from engineering_control.dashboard.connection_records import (
    connected_records,
    connected_story_issues,
    finding_links,
)
from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.planning_lifecycle import (
    ARCHIVED_DRAFT,
    CLOSED_DRAFT,
    PlanningLifecycle,
    story_has_epic,
)
from engineering_control.dashboard.planning_queue import (
    queue_review,
    record_changed,
    review_results,
)
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.workspace_publishing import CLOSE_ISSUE, confirm_classification
from engineering_control.db.models import (
    BacklogItem,
    PlanningReview,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    WorkspaceAction,
    WorkspaceCandidate,
    WorkspaceImport,
    WorkspaceSource,
)

Kind = Literal["epics", "stories", "issues"]
PlanningState = Literal["open", "closed"]
IssueView = Literal["open", "closed", "draft"]


def replace_parents(
    session: Session,
    kind: Kind,
    project: UUID,
    key: UUID,
    parents: list[UUID],
    *,
    allow_empty: bool = False,
) -> None:
    parent_model = ProjectEpic if kind == "stories" else ProjectUserStory
    expected = set(parents)
    if (not expected and not (kind == "issues" and allow_empty)) or len(expected) != len(parents):
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
    *,
    review_scope: str = "full",
    new_id: UUID | None = None,
    execution_type: str | None = None,
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
    if new_id is not None:
        row.id = new_id
    row.updated_at = datetime.now(UTC)
    row.title = clean_title(title)
    row.description = clean_description(description)
    session.add(row)
    session.flush()
    if isinstance(row, BacklogItem) and execution_type:
        confirm_classification(session, project, row, execution_type)
    if kind != "epics":
        optional = (
            kind == "issues"
            and not parents
            and session.scalar(
                bugs_with_optional_stories(session, project.id).where(BacklogItem.id == row.id)
            )
            is not None
        )
        replace_parents(session, kind, project.id, row.id, parents, allow_empty=optional)
    if key is None:
        queue_review(session, project.id, kind, row.id, row.title, review_scope)
    else:
        record_changed(session, project.id, kind, row.id, row.title)
    return row.id


def _visible_issues(project: UUID) -> ColumnElement[bool]:
    return BacklogItem.workspace_review.is_distinct_from(ARCHIVED_DRAFT) & (
        BacklogItem.workspace_review.in_(("pending", "publishing", "approved", CLOSED_DRAFT))
        | BacklogItem.id.in_(
            select(ProjectIssueStory.item_id).where(ProjectIssueStory.project_id == project)
        )
        | BacklogItem.id.in_(
            select(WorkspaceAction.local_id).where(
                WorkspaceAction.project_id == project,
                WorkspaceAction.operation == CLOSE_ISSUE,
                WorkspaceAction.confirmation_token.is_not(None),
            )
        )
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
            .where(
                ProjectEpic.project_id == project,
                ProjectEpic.archived_at.is_(None),
                ProjectEpic.closed_at.is_(None),
            )
            .limit(1)
        ):
            return "Approve, add or reopen an Epic before generating User Stories"
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
            parent.closed_at.is_(None),
            parent.id.not_in(select(foreign_key).where(links.project_id == project)),
        )
    )
    if missing:
        label = "Epic" if stage == "stories" else "User Story"
        return (
            f"{missing} {label} records still need coverage. Add proposals or adjust their links."
        )
    return None


def _approvable(rows: Subquery, kind: Kind) -> ColumnElement[bool]:
    pending = rows.c.review.in_(("proposal", "pending"))
    if kind == "epics":
        return pending
    eligible = ~rows.c.proposal | rows.c.parents[0].as_string().is_not(None)
    if kind == "issues":
        eligible = eligible | rows.c.number.is_not(None) | (rows.c.proposal_type == "bug")
    return cast(ColumnElement[bool], pending & eligible)


def _table_rows(project: UUID, kind: Kind) -> Subquery:
    awaiting_stage = (
        (WorkspaceImport.stage != kind)
        & (WorkspaceCandidate.origin == "import")
        & (
            WorkspaceImport.progress["coverage_run"]["state"]
            .as_string()
            .in_(("running", "waiting", "failed"))
        )
    )
    proposals = (
        select(
            WorkspaceCandidate.id,
            WorkspaceCandidate.title,
            WorkspaceCandidate.description,
            WorkspaceCandidate.created_at,
            true().label("proposal"),
            case((awaiting_stage, "awaiting_stage"), else_="proposal").label("review"),
            WorkspaceCandidate.parents,
            WorkspaceCandidate.execution_type.label("proposal_type"),
            WorkspaceCandidate.number,
            WorkspaceSource.evidence["state"].as_string().label("github_state"),
            literal("open").label("local_state")
            if kind != "issues"
            else null().label("local_state"),
        )
        .outerjoin(
            WorkspaceSource,
            (WorkspaceSource.project_id == WorkspaceCandidate.project_id)
            & (WorkspaceSource.key == WorkspaceCandidate.identity),
        )
        .outerjoin(WorkspaceImport, WorkspaceImport.project_id == WorkspaceCandidate.project_id)
        .where(
            WorkspaceCandidate.project_id == project,
            WorkspaceCandidate.kind == kind,
            WorkspaceCandidate.decision == "pending",
            (WorkspaceImport.stage == kind)
            | (WorkspaceCandidate.origin != "import")
            | awaiting_stage,
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
            null(),
            BacklogItem.issue_number,
            BacklogItem.issue_state,
            null(),
        ).where(
            BacklogItem.project_id == project,
            _visible_issues(project),
            BacklogItem.workspace_review.is_distinct_from("planning"),
            BacklogItem.issue_number.is_(None)
            | BacklogItem.issue_number.not_in(
                proposals.with_only_columns(WorkspaceCandidate.number).where(
                    WorkspaceCandidate.number.is_not(None)
                )
            ),
        )
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
            null(),
            case((model.closed_at.is_(None), "open"), else_="closed"),
        ).where(model.project_id == project, model.archived_at.is_(None))
    return union_all(proposals, saved).subquery()


def _state_bucket(rows: Subquery, kind: Kind) -> ColumnElement[str]:
    if kind != "issues":
        return rows.c.local_state
    return case(
        (rows.c.number.is_(None), "draft"), else_=func.coalesce(rows.c.github_state, "open")
    )


def _child_counts(
    session: Session, project: UUID, kind: Kind, keys: list[UUID]
) -> dict[UUID, tuple[int, int]]:
    """Count established relationships for the displayed parents across all child states."""
    if kind == "issues" or not keys:
        return {}
    story = ProjectUserStory
    member = select(story.id.label("story_id")).where(
        story.project_id == project, story.archived_at.is_(None)
    )
    if kind == "epics":
        link = ProjectStoryEpic
        member = (
            member.add_columns(link.epic_id.label("parent_id"))
            .join(link, (link.story_id == story.id) & (link.project_id == project))
            .where(link.epic_id.in_(keys))
        )
    else:
        member = member.add_columns(story.id.label("parent_id")).where(story.id.in_(keys))
    members = member.cte("member_stories")
    story_ids = select(members.c.story_id)
    local_issues = (
        select(ProjectIssueStory.story_id, ProjectIssueStory.item_id.label("issue_id"))
        .join(BacklogItem, BacklogItem.id == ProjectIssueStory.item_id)
        .where(
            ProjectIssueStory.project_id == project,
            ProjectIssueStory.story_id.in_(story_ids),
            BacklogItem.workspace_review.is_distinct_from("planning"),
        )
    )
    issues = union_all(local_issues, connected_story_issues(project, story_ids)).subquery()
    totals = (
        select(
            members.c.parent_id,
            func.count(members.c.story_id.distinct()),
            func.count(issues.c.issue_id.distinct()),
        )
        .outerjoin(issues, issues.c.story_id == members.c.story_id)
        .group_by(members.c.parent_id)
    )
    return {key: (stories, issues_count) for key, stories, issues_count in session.execute(totals)}


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
        self.lifecycle = PlanningLifecycle(sessions)

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
        issue_state: IssueView | None = None,
        state: PlanningState | None = None,
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
            scope: ColumnElement[bool] = true()
            view = issue_state if kind == "issues" else state
            bucket = _state_bucket(rows, kind)
            state_counts = dict(
                session.execute(select(bucket, func.count()).select_from(rows).group_by(bucket))
                .tuples()
                .all()
            )
            if (
                kind == "issues"
                and record
                and not session.scalar(select(rows.c.id).where(rows.c.id == record))
            ):
                # During its import stage a reviewable GitHub candidate can represent
                # the same numbered source as a saved technical Issue in this table.
                number = session.scalar(
                    select(BacklogItem.issue_number).where(
                        BacklogItem.project_id == project.id, BacklogItem.id == record
                    )
                )
                if number is not None:
                    focus = (
                        session.scalar(
                            select(rows.c.id).where(rows.c.number == number, rows.c.proposal)
                        )
                        or record
                    )
            if focus and view:
                view = session.scalar(select(bucket).where(rows.c.id == focus)) or view
            if view:
                scope = bucket == view
                matches = matches & scope
            ordering = (rows.c.proposal.desc(), rows.c.created_at, rows.c.id)
            # Stage progression covers every view. Bulk approval covers the selected state.
            total, pending, proposed = session.execute(
                select(
                    func.count().filter(matches),
                    func.count().filter(scope & _approvable(rows, kind)),
                    func.count().filter(rows.c.proposal),
                ).select_from(rows)
            ).one()
            pages = max(1, (total + page_size - 1) // page_size)
            requested_page = page
            page = min(page, pages)
            if focus:
                ranked = (
                    select(
                        rows.c.id,
                        rows.c.proposal,
                        func.row_number().over(order_by=ordering).label("position"),
                    )
                    .where(scope)
                    .subquery()
                )
                position = session.scalar(select(ranked.c.position).where(ranked.c.id == focus))
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
            mirror_ids = (
                {
                    number: key
                    for number, key in session.execute(
                        select(BacklogItem.issue_number, BacklogItem.id).where(
                            BacklogItem.project_id == project.id,
                            func.lower(BacklogItem.repository) == project.source_repository.lower(),
                            BacklogItem.workspace_review.is_distinct_from("planning"),
                            BacklogItem.issue_number.in_(
                                [i["number"] for i in items if i["proposal"] and i["number"]]
                            ),
                        )
                    )
                }
                if kind == "issues"
                else {}
            )
            connection_keys = [i["id"] for i in items if not i["proposal"]] + list(
                mirror_ids.values()
            )
            closing = (
                set(
                    session.scalars(
                        select(WorkspaceAction.local_id).where(
                            WorkspaceAction.project_id == project.id,
                            WorkspaceAction.local_id.in_(connection_keys),
                            WorkspaceAction.operation == CLOSE_ISSUE,
                            WorkspaceAction.status != "done",
                        )
                    )
                )
                if kind == "issues"
                else set()
            )
            linked: dict[UUID, list[str]] = defaultdict(list)
            parents: dict[str, str] = {}
            has_parents = False
            if kind != "epics":
                options = _parent_options(project.id, kind)
                has_parents = session.execute(options.limit(1)).first() is not None
                child, parent, link = _parent_links(kind)
                if connection_keys:
                    for child_id, parent_id in session.execute(
                        select(child, parent)
                        .where(link.project_id == project.id, child.in_(connection_keys))
                        .order_by(parent)
                    ):
                        linked[child_id].append(str(parent_id))
                for item in items:
                    key = (
                        mirror_ids.get(item["number"], item["id"])
                        if item["proposal"]
                        else item["id"]
                    )
                    item["parents"] = list(dict.fromkeys([*(item["parents"] or []), *linked[key]]))
                ids = {UUID(uid) for item in items for uid in item["parents"]}
                if ids:
                    parent_model = ProjectEpic if kind == "stories" else ProjectUserStory
                    parents = {
                        str(uid): title
                        for uid, title in session.execute(options.where(parent_model.id.in_(ids)))
                    }
            external: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
            for uid, badge in finding_links(session, project.id, kind, connection_keys):
                external[uid].append(badge)
            classifications = (
                {
                    uid: (value, confirmed, optional)
                    for uid, value, confirmed, optional in session.execute(
                        select(
                            BacklogItem.id,
                            BacklogItem.execution_type,
                            BacklogItem.type_confirmed,
                            BacklogItem.id.in_(bugs_with_optional_stories(session, project.id)),
                        ).where(
                            BacklogItem.project_id == project.id,
                            BacklogItem.id.in_(connection_keys),
                        )
                    )
                }
                if kind == "issues"
                else {}
            )
            row_ids = [item["id"] for item in items]
            child_counts = _child_counts(session, project.id, kind, row_ids)
            reviews = review_results(
                session,
                project.id,
                session.scalars(
                    select(PlanningReview).where(
                        PlanningReview.project_id == project.id,
                        PlanningReview.kind == kind,
                        PlanningReview.record_id.in_(row_ids + list(mirror_ids.values())),
                    )
                ).all(),
            )
            reasons = dict(
                session.execute(
                    select(WorkspaceCandidate.id, WorkspaceCandidate.review_reason).where(
                        WorkspaceCandidate.project_id == project.id,
                        WorkspaceCandidate.id.in_(row_ids),
                        WorkspaceCandidate.review_reason.is_not(None),
                    )
                )
                .tuples()
                .all()
            )
            for item in items:
                key = item["id"]
                if kind != "issues":
                    stories_count, item["issue_count"] = child_counts.get(key, (0, 0))
                    if kind == "epics":
                        item["story_count"] = stories_count
                mirror = mirror_ids.get(item["number"], key) if item["proposal"] else key
                item["lifecycle_id"] = (
                    str(mirror)
                    if not item["proposal"] or (kind == "issues" and item["number"] in mirror_ids)
                    else None
                )
                item["closing"] = mirror in closing
                item["automatic_review"] = reviews.get(mirror)
                item["review_reason"] = reasons.get(key)
                item["connections"] = external[mirror]
                rels = item.pop("parents")
                proposal_type = item.pop("proposal_type")
                item.pop("created_at")
                if kind != "epics":
                    ids_for_row = [v for v in rels if v in parents]
                    item["epic_ids" if kind == "stories" else "story_ids"] = ids_for_row
                    if kind == "issues":
                        category, confirmed, optional = classifications.get(
                            mirror, (None, False, False)
                        )
                        if proposal_type:
                            category, confirmed, optional = (
                                proposal_type,
                                True,
                                proposal_type == "bug",
                            )
                        item["execution_type"], item["type_confirmed"] = category, confirmed
                        item["story_optional"] = optional
                        unlinked = not ids_for_row and not any(
                            link["kind"] == "stories"
                            and link["record"]
                            and link["status"] == "approved"
                            for link in item["connections"]
                        )
                        item["orphan"] = unlinked and not optional
                        item["standalone_bug"] = unlinked and optional
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
                    WorkspaceImport.progress["coverage_run"]["state"].as_string().label("coverage"),
                ).where(WorkspaceImport.project_id == project.id)
            ).one_or_none()
            current = review is not None and review.stage == kind
            return {
                "items": items,
                "state": view,
                "state_counts": state_counts,
                "issue_state": view if kind == "issues" else None,
                "issue_counts": state_counts if kind == "issues" else {},
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
                    and (
                        review.coverage == "running"
                        or (
                            review.lease
                            and review.lease_until
                            and review.lease_until.replace(tzinfo=UTC) > datetime.now(UTC)
                        )
                    )
                ),
                "can_continue": bool(
                    current
                    and review
                    and review.coverage != "running"
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

    def pending(
        self,
        name: str,
        kind: Kind,
        issue_state: IssueView | None = None,
        state: PlanningState | None = None,
    ) -> dict[str, Any]:
        with self._snapshot() as session:
            project = active_project(session, name)
            rows = _table_rows(project.id, kind)
            query = (
                select(rows.c.id, rows.c.number, rows.c.github_state)
                .where(_approvable(rows, kind))
                .order_by(rows.c.proposal.desc(), rows.c.created_at, rows.c.id)
            )
            view = issue_state if kind == "issues" else state
            if view:
                query = query.where(_state_bucket(rows, kind) == view)
            pending = session.execute(query).all()
            result = {
                "ids": [str(r.id) for r in pending],
                "version": session.scalar(
                    select(WorkspaceImport.version).where(WorkspaceImport.project_id == project.id)
                ),
            }
            if kind in ("epics", "stories"):
                closing = [r.number for r in pending if r.number and r.github_state == "open"]
                if closing:
                    result["close_numbers"] = closing
            return result

    def link_stories(
        self, name: str, key: UUID, stories: list[UUID], execution_type: str | None = None
    ) -> dict[str, str]:
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            item = db.scalar(
                select(BacklogItem).where(
                    BacklogItem.id == key,
                    BacklogItem.project_id == project.id,
                    _visible_issues(project.id),
                    BacklogItem.workspace_review.is_distinct_from("planning"),
                )
            )
            if item is None:
                raise ProjectNotFound(str(key))
            if execution_type:
                confirm_classification(db, project, item, execution_type)
            optional = (
                db.scalar(bugs_with_optional_stories(db, project.id).where(BacklogItem.id == key))
                is not None
            )
            # A reclassified standalone bug can return to the list of Issues needing a Story.
            empty_before = execution_type == "enhancement" and not db.scalar(
                select(ProjectIssueStory.story_id).where(ProjectIssueStory.item_id == key).limit(1)
            )
            replace_parents(
                db, "issues", project.id, key, stories, allow_empty=optional or empty_before
            )
            record_changed(db, project.id, "issues", key, item.title)
            item.facts = {**(item.facts or {}), "manual_story_links": bool(stories) or optional}
            mirror = db.scalar(
                select(WorkspaceCandidate).where(
                    WorkspaceCandidate.project_id == project.id,
                    WorkspaceCandidate.kind == "issues",
                    WorkspaceCandidate.number.is_not(None),
                    WorkspaceCandidate.number == item.issue_number,
                    WorkspaceCandidate.decision == "pending",
                )
            )
            if mirror:
                mirror.parents = sorted({str(story) for story in stories})
            item.updated_at = datetime.now(UTC)
            return {"id": str(key)}

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
                    {
                        "id": str(e.id),
                        "title": e.title,
                        "description": e.description,
                        "local_state": "closed" if e.closed_at else "open",
                    }
                    for e in epics
                ],
                "stories": [
                    {
                        "id": str(s.id),
                        "title": s.title,
                        "description": s.description,
                        "local_state": "closed" if s.closed_at else "open",
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
        execution_type: str | None = None,
    ) -> dict[str, str]:
        with self.sessions.begin() as session:
            project = active_project(session, name, lock=True)
            if key is None and kind != "epics" and not parents:
                identifier = uuid4()
                cleaned = clean_title(title)
                session.add(
                    WorkspaceCandidate(
                        id=identifier,
                        project_id=project.id,
                        kind=kind,
                        title=cleaned,
                        description=clean_description(description),
                        parents=[],
                        identity=f"intake:{identifier}",
                        origin="intake",
                        execution_type=execution_type,
                    )
                )
                queue_review(session, project.id, kind, identifier, cleaned, "intake")
            else:
                identifier = save_record(
                    session,
                    project,
                    kind,
                    title,
                    description,
                    parents,
                    key,
                    execution_type=execution_type,
                )
            return {"project": name, "id": str(identifier)}

    def set_state(
        self,
        name: str,
        kind: Kind,
        key: UUID,
        state: PlanningState,
        confirmation: str | None = None,
    ) -> dict[str, Any]:
        if state == "closed":
            return self.lifecycle.confirm(name, kind, key, "close", confirmation)
        if kind == "issues":
            raise ProjectInvalid("Reopen published Issues in GitHub.")
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
            record = cast(ProjectEpic | ProjectUserStory, row)
            if record.closed_at is not None:
                if kind == "stories" and not story_has_epic(session, key):
                    raise ProjectConflict("Link the User Story to an Epic before reopening it.")
                record.closed_at = None
                record.updated_at = datetime.now(UTC)
            return {"id": str(key), "state": state}

    def remove(
        self, name: str, kind: Kind, key: UUID, confirmation: str | None = None
    ) -> dict[str, Any]:
        return self.lifecycle.confirm(name, kind, key, "delete", confirmation)
