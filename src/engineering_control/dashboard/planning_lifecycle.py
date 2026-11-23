"""Preview and commit the same explicit set of planning changes before contacting GitHub."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Integer, case, delete, func, literal, select, text, union_all, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, aliased, sessionmaker

from engineering_control.dashboard.connection_records import RECORD_MODELS, digest
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.workspace_publishing import CLOSE_ISSUE, enqueue
from engineering_control.db.models import (
    BacklogItem,
    ConnectionFinding,
    PlanningLifecycleReceipt,
    PlanningReview,
    ProjectConnection,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
    WorkspaceAction,
)

Kind = Literal["epics", "stories", "issues"]
Operation = Literal["close", "delete"]
Record = ProjectEpic | ProjectUserStory | BacklogItem
ARCHIVED_DRAFT = "deleted_draft"
CLOSED_DRAFT = "closed_draft"


def _relations(parent_kind: Kind) -> Any:
    """Verified local and inter-project parent/child links, in one direction."""
    link: type[ProjectStoryEpic] | type[ProjectIssueStory]
    parent_model: type[ProjectEpic] | type[ProjectUserStory]
    child_model: type[ProjectUserStory] | type[BacklogItem]
    if parent_kind == "epics":
        link, parent, child = ProjectStoryEpic, ProjectStoryEpic.epic_id, ProjectStoryEpic.story_id
        parent_model, child_model = ProjectEpic, ProjectUserStory
        child_kind = "stories"
    else:
        link, parent, child = (
            ProjectIssueStory,
            ProjectIssueStory.story_id,
            ProjectIssueStory.item_id,
        )
        parent_model, child_model = ProjectUserStory, BacklogItem
        child_kind = "issues"
    local = select(
        parent.label("parent"),
        child.label("child"),
        literal(None, type_=Integer).label("child_number"),
        link.project_id.label("owner"),
        link.project_id.label("peer"),
    )
    f, c = ConnectionFinding, ProjectConnection
    forward = f.source_kind == parent_kind
    remote = (
        select(
            case((forward, f.source_id), else_=f.target_id),
            case((forward, f.target_id), else_=f.source_id),
            case((forward, f.target_number), else_=f.source_number),
            case(
                (forward, f.source_project),
                (f.source_project == c.first_id, c.second_id),
                else_=c.first_id,
            ),
            case(
                (~forward, f.source_project),
                (f.source_project == c.first_id, c.second_id),
                else_=c.first_id,
            ),
        )
        .join_from(f, c, c.id == f.connection_id)
        .where(
            c.archived_at.is_(None),
            f.status == "approved",
            f.action.in_(("link", "create")),
            f.source_project.in_((c.first_id, c.second_id)),
            (forward & (f.target_kind == child_kind))
            | ((f.source_kind == child_kind) & (f.target_kind == parent_kind)),
        )
    )
    edges = union_all(local, remote).subquery()
    owner, peer = aliased(ProjectRecord), aliased(ProjectRecord)
    real_child = (
        (
            BacklogItem.workspace_review.is_distinct_from("planning")
            & BacklogItem.workspace_review.is_distinct_from(ARCHIVED_DRAFT)
        )
        if child_kind == "issues"
        else ProjectUserStory.archived_at.is_(None)
    )
    child_match = child_model.id == edges.c.child
    if child_kind == "issues":
        child_match = child_match | (
            edges.c.child.is_(None)
            & (BacklogItem.issue_number == edges.c.child_number)
            & (func.lower(BacklogItem.repository) == func.lower(peer.source_repository))
        )
    return (
        select(parent_model.id.label("parent"), child_model.id.label("child"))
        .select_from(edges)
        .join(peer, peer.id == edges.c.peer)
        .join(
            parent_model,
            (parent_model.id == edges.c.parent) & (parent_model.project_id == edges.c.owner),
        )
        .join(
            child_model,
            child_match & (child_model.project_id == edges.c.peer),
        )
        .join(owner, owner.id == edges.c.owner)
        .where(
            owner.archived_at.is_(None),
            peer.archived_at.is_(None),
            parent_model.archived_at.is_(None),
            real_child,
        )
        .subquery()
    )


def _children(db: Session, parent_kind: Kind, parents: set[UUID]) -> dict[UUID, set[UUID]]:
    edges = _relations(parent_kind)
    touched = select(edges.c.child).where(edges.c.parent.in_(parents))
    result: dict[UUID, set[UUID]] = defaultdict(set)
    for child, parent in db.execute(
        select(edges.c.child, edges.c.parent).where(edges.c.child.in_(touched))
    ):
        result[child].add(parent)
    return result


def story_has_epic(db: Session, key: UUID) -> bool:
    edges = _relations("epics")
    return bool(db.scalar(select(edges.c.parent).where(edges.c.child == key).limit(1)))


class Impact:
    def __init__(
        self, db: Session, project: ProjectRecord, kind: Kind, key: UUID, operation: Operation
    ):
        self.db, self.kind, self.key, self.operation = db, kind, key, operation
        self.closures: set[UUID] = set()
        self.records: dict[tuple[str, UUID], Record] = {}
        self.rows: list[dict[str, Any]] = []
        self.revisions: list[tuple[str, str, str]] = []
        roots = self._records(kind, {key})
        if not roots or roots[0][0].project_id != project.id:
            raise ProjectNotFound(str(key))
        root, owner = roots[0]
        if isinstance(root, BacklogItem):
            local_draft = not root.issue_number and root.workspace_review in (
                "pending",
                CLOSED_DRAFT,
            )
            if (
                not local_draft
                and not root.issue_number
                and root.workspace_review not in ("approved", "publishing")
            ):
                edges = _relations("stories")
                if not db.scalar(select(edges.c.parent).where(edges.c.child == key).limit(1)):
                    raise ProjectNotFound(str(key))
            if operation == "delete" and not local_draft:
                raise ProjectInvalid(
                    "Only local drafts can be deleted. Close published or publishing Issues."
                )
        self._add(kind, root, owner, operation, "Selected record", set())
        if kind == "epics":
            stories = self._descendants("epics", "stories", {key})
        else:
            stories = {key} if kind == "stories" else set()
        if stories:
            self._descendants("stories", "issues", stories)
        self.snapshot = {
            "project": project.name,
            "kind": kind,
            "id": str(key),
            "operation": operation,
            "items": self.rows,
        }
        self.token = digest([self.snapshot, self.revisions])

    def _records(self, kind: Kind, keys: set[UUID]) -> list[Any]:
        model = RECORD_MODELS[kind]
        if kind == "issues" and keys:
            self.closures.update(
                self.db.scalars(
                    select(WorkspaceAction.local_id).where(
                        WorkspaceAction.local_id.in_(keys),
                        WorkspaceAction.operation == CLOSE_ISSUE,
                        WorkspaceAction.status != "done",
                    )
                )
            )
        available = (
            BacklogItem.workspace_review.is_distinct_from(ARCHIVED_DRAFT)
            & BacklogItem.workspace_review.is_distinct_from("planning")
            if kind == "issues"
            else (ProjectEpic if kind == "epics" else ProjectUserStory).archived_at.is_(None)
        )
        return list(
            self.db.execute(
                select(model, ProjectRecord)
                .join(ProjectRecord, ProjectRecord.id == model.project_id)
                .where(model.id.in_(keys), ProjectRecord.archived_at.is_(None), available)
                .order_by(ProjectRecord.name, model.title, model.id)
            )
        )

    def _descendants(self, parent: Kind, kind: Kind, parents: set[UUID]) -> set[UUID]:
        linked = _children(self.db, parent, parents)
        exclusive = set()
        for row, project in self._records(kind, set(linked)):
            own = linked[row.id]
            if len(own) == 1:
                self._add(
                    kind,
                    row,
                    project,
                    "close",
                    "Only linked Epic" if parent == "epics" else "Only linked User Story",
                    own,
                )
                exclusive.add(row.id)
            else:
                label = "Epics" if parent == "epics" else "User Stories"
                self._add(kind, row, project, "keep", f"Shared by {len(own)} {label}", own)
        return exclusive

    def _add(
        self,
        kind: Kind,
        row: Record,
        project: ProjectRecord,
        action: str,
        reason: str,
        parents: set[UUID],
    ) -> None:
        item: dict[str, Any] = dict(
            project=project.name,
            id=str(row.id),
            kind=kind,
            title=row.title,
            action=action,
            reason=reason,
            parents=sorted(str(p) for p in parents),
        )
        if isinstance(row, BacklogItem):
            number = row.issue_number
            item.update(
                repository=(row.repository or project.source_repository),
                number=number,
                state=(row.issue_state or "open")
                if number
                else "closed locally"
                if row.workspace_review == CLOSED_DRAFT
                else "draft",
                github=None,
            )
            if action == "close":
                if row.issue_state == "closed" or row.workspace_review == CLOSED_DRAFT:
                    item["action"] = "already_closed"
                elif row.id in self.closures:
                    item["action"] = "sync_pending"
                elif row.workspace_review == "publishing" and not number:
                    item["github"] = "after_publication"
                elif number:
                    item["github"] = "close"
        else:
            item["state"] = "closed" if row.closed_at else "open"
            if action == "close" and row.closed_at:
                item["action"] = "already_closed"
        self.rows.append(item)
        self.records[kind, row.id] = row
        self.revisions.append((kind, str(row.id), row.updated_at.isoformat()))

    def apply(self, now: datetime) -> None:
        for entry in self.rows:
            action = entry["action"]
            if action not in ("close", "delete"):
                continue
            row = self.records[entry["kind"], UUID(entry["id"])]
            if isinstance(row, BacklogItem):
                if action == "delete":
                    row.workspace_review, row.state = ARCHIVED_DRAFT, "skipped"
                else:
                    row.state = "done"
                    if not entry["github"]:
                        row.workspace_review = CLOSED_DRAFT
                    else:
                        a = enqueue(
                            self.db,
                            row.project_id,
                            CLOSE_ISSUE,
                            row.id,
                            entry["repository"],
                            row.issue_number,
                        )
                        if a.status not in ("pending", "waiting") or a.number != row.issue_number:
                            a.repository, a.number = entry["repository"], row.issue_number
                            a.detail, a.lease, a.lease_until = None, None, None
                        a.confirmation_token = self.token
                        a.status = "waiting" if row.workspace_review == "publishing" else "pending"
            elif action == "close":
                row.closed_at = now
            else:
                row.archived_at = now
            row.updated_at = now
            if action == "delete":
                self.db.execute(
                    update(PlanningReview)
                    .where(
                        PlanningReview.project_id == row.project_id,
                        PlanningReview.kind == entry["kind"],
                        PlanningReview.record_id == row.id,
                    )
                    .values(status="dismissed", lease=None)
                )
        if self.operation == "delete":
            # The root is archived. Its children retain their own records and their other parents.
            if self.kind == "epics":
                self.db.execute(
                    delete(ProjectStoryEpic).where(ProjectStoryEpic.epic_id == self.key)
                )
            elif self.kind == "stories":
                self.db.execute(
                    delete(ProjectStoryEpic).where(ProjectStoryEpic.story_id == self.key)
                )
                self.db.execute(
                    delete(ProjectIssueStory).where(ProjectIssueStory.story_id == self.key)
                )
            else:
                self.db.execute(
                    delete(ProjectIssueStory).where(ProjectIssueStory.item_id == self.key)
                )
            self.db.execute(
                update(ConnectionFinding)
                .where(
                    ConnectionFinding.status == "approved",
                    (
                        (ConnectionFinding.source_kind == self.kind)
                        & (ConnectionFinding.source_id == self.key)
                    )
                    | (
                        (ConnectionFinding.target_kind == self.kind)
                        & (ConnectionFinding.target_id == self.key)
                    ),
                )
                .values(status="dismissed")
            )


class PlanningLifecycle:
    def __init__(self, sessions: sessionmaker[Session]):
        self.sessions = sessions

    def preview(self, name: str, kind: Kind, key: UUID, operation: Operation) -> dict[str, Any]:
        with self.sessions.begin() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            impact = Impact(db, active_project(db, name), kind, key, operation)
            return {**impact.snapshot, "token": impact.token}

    def confirm(
        self, name: str, kind: Kind, key: UUID, operation: Operation, token: str | None
    ) -> dict[str, Any]:
        if not token:
            raise ProjectConflict("Preview the affected records and confirm this change.")
        try:
            with self.sessions.begin() as db:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                project = active_project(db, name, lock=True)
                previous = db.get(PlanningLifecycleReceipt, token)
                if previous and (
                    previous.project_id,
                    previous.kind,
                    previous.record_id,
                    previous.operation,
                ) == (project.id, kind, key, operation):
                    return {
                        "id": str(key),
                        "project": name,
                        "state": "closed" if operation == "close" else "deleted",
                    }
                impact = Impact(db, project, kind, key, operation)
                if impact.token != token:
                    raise ProjectConflict(
                        "The affected records changed. Review the updated list and confirm again."
                    )
                now = datetime.now(UTC)
                impact.apply(now)
                db.add(
                    PlanningLifecycleReceipt(
                        token=token,
                        kind=kind,
                        record_id=key,
                        operation=operation,
                        project_id=project.id,
                        impact=impact.snapshot,
                        confirmed_at=now,
                    )
                )
                return {
                    "id": str(key),
                    "project": name,
                    "state": "closed" if operation == "close" else "deleted",
                }
        except OperationalError as exc:
            if getattr(exc.orig, "sqlstate", None) in ("40001", "40P01"):
                raise ProjectConflict(
                    "Concurrent change. Review the updated list and confirm again."
                ) from exc
            raise
