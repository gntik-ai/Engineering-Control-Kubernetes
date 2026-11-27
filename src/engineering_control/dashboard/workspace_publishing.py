"""Publish only recorded Superadmin approvals and reconcile uncertain GitHub responses."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.planning_queue import queue_review
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.workspace_issues import planning_kind
from engineering_control.db.models import (
    BacklogItem,
    BugRecord,
    PlanningLifecycleReceipt,
    ProjectEpic,
    ProjectRecord,
    ProjectUserStory,
    WorkspaceAction,
    WorkspaceCandidate,
    WorkspaceSource,
)
from engineering_control.github_app import GitHubApp, GitHubAppError

LOGGER = logging.getLogger(__name__)
CLOSE_ISSUE = "close_issue"
NOT_AN_ISSUE = (
    "This record calls itself an Epic or a User Story. Those are kept here and are not "
    "written to GitHub: only Issues are."
)


def action_row(action: WorkspaceAction) -> dict[str, Any]:
    return {
        "id": str(action.id),
        "operation": action.operation,
        "number": action.number,
        "status": action.status,
        "detail": action.detail,
    }


def enqueue(
    session: Session,
    project: UUID,
    operation: str,
    record: UUID,
    repository: str,
    number: int | None = None,
) -> WorkspaceAction:
    row = session.scalar(
        select(WorkspaceAction).where(
            WorkspaceAction.project_id == project,
            WorkspaceAction.operation == operation,
            WorkspaceAction.local_id == record,
        )
    )
    if row is None:
        row = WorkspaceAction(
            project_id=project,
            operation=operation,
            local_id=record,
            repository=repository,
            number=number,
        )
        session.add(row)
        session.flush()
    return row


def confirm_classification(
    db: Session, project: ProjectRecord, item: BacklogItem, category: str
) -> None:
    if category not in ("bug", "enhancement"):
        raise ProjectInvalid("Choose Bug or Enhancement for this Issue")
    if category != item.execution_type:
        item.classification_reason = "Classification selected by the administrator."
    if category != item.execution_type or not item.type_confirmed:
        item.execution_type, item.type_confirmed = category, True
        schedule_classification(db, project, item)
        if (
            item.issue_number
            and (item.repository or project.source_repository).casefold()
            == project.source_repository.casefold()
        ):
            mirror = db.scalar(
                select(WorkspaceCandidate).where(
                    WorkspaceCandidate.project_id == project.id,
                    WorkspaceCandidate.number == item.issue_number,
                    WorkspaceCandidate.kind == "issues",
                    WorkspaceCandidate.decision == "pending",
                )
            )
            if mirror:
                mirror.execution_type = category


def schedule_classification(db: Session, project: ProjectRecord, item: BacklogItem) -> None:
    if not item.type_confirmed or not item.issue_number:
        return
    repo = item.repository or project.source_repository
    action = enqueue(db, project.id, "label", item.id, repo, item.issue_number)
    # A worker holding a lease will compare its target to the latest decision at commit.
    action.status, action.detail = "pending", None


def approve_issue(session: Session, project: ProjectRecord, key: UUID) -> UUID | None:
    item = session.scalar(
        select(BacklogItem).where(BacklogItem.project_id == project.id, BacklogItem.id == key)
    )
    if item is None:
        raise ProjectNotFound(str(key))
    if item.workspace_review == "approved":
        return None
    if item.workspace_review not in ("pending", "publishing"):
        raise ProjectConflict("This issue has no pending workspace approval")
    queue_review(session, project.id, "issues", item.id, item.title)
    if item.issue_number:
        item.workspace_review = "publishing"
        return enqueue(
            session, project.id, "link", key, project.source_repository, item.issue_number
        ).id
    item.workspace_review = "publishing"
    return enqueue(session, project.id, "create", key, project.source_repository).id


def _confirmed_issue(db: Session, action: WorkspaceAction, project: ProjectRecord) -> bool:
    receipt = (
        db.get(PlanningLifecycleReceipt, action.confirmation_token)
        if action.confirmation_token
        else None
    )
    if not receipt:
        return False
    for entry in receipt.impact["items"]:
        if (
            entry["kind"] != "issues"
            or entry["action"] != "close"
            or entry["project"] != project.name
            or entry["id"] != str(action.local_id)
            or entry["repository"].casefold() != action.repository.casefold()
        ):
            continue
        if entry["github"] == "close":
            return bool(entry["number"] == action.number)
        if entry["github"] == "after_publication":
            # A draft had no GitHub number at confirmation. Its own completed publication
            # is the authority for the number now attached to it.
            return bool(
                db.scalar(
                    select(WorkspaceAction.id).where(
                        WorkspaceAction.project_id == action.project_id,
                        WorkspaceAction.operation == "create",
                        WorkspaceAction.local_id == action.local_id,
                        WorkspaceAction.repository == action.repository,
                        WorkspaceAction.number == action.number,
                        WorkspaceAction.status == "done",
                    )
                )
            )
    return False


class WorkspacePublishing:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def approve(self, name: str, key: UUID) -> UUID | None:
        with self.sessions.begin() as session:
            project = active_project(session, name, lock=True)
            return approve_issue(session, project, key)

    def _claim(self, name: str, key: UUID) -> tuple[WorkspaceAction, str, str]:
        with self.sessions.begin() as session:
            project = active_project(session, name, lock=True)
            action = session.get(WorkspaceAction, key)
            if action is None or action.project_id != project.id:
                raise ProjectNotFound(str(key))
            if action.status in ("done", "retained"):
                session.expunge(action)
                return action, "", ""
            now = datetime.now(UTC)
            if action.lease_until and action.lease_until.replace(tzinfo=UTC) > now:
                raise ProjectConflict("Synchronization is already running")
            if action.operation == CLOSE_ISSUE:
                item = session.get(BacklogItem, action.local_id)
                if action.status == "waiting":
                    raise ProjectConflict("Waiting for the approved issue publication to finish.")
                if (
                    not item
                    or item.project_id != project.id
                    or item.workspace_review == "planning"
                    or not action.number
                    or item.issue_number != action.number
                    or (item.repository or project.source_repository).casefold()
                    != action.repository.casefold()
                    or not _confirmed_issue(session, action, project)
                ):
                    action.status, action.detail = (
                        "blocked",
                        "The Issue no longer matches its confirmed closure.",
                    )
                    session.flush()
                    session.expunge(action)
                    return action, "", ""
                action.status, action.detail = "pending", None
            title = body = ""
            if action.operation == "create":
                item = session.get(BacklogItem, action.local_id)
                if item is None or item.workspace_review != "publishing":
                    raise ProjectConflict("This issue no longer has a recorded approval")
                if planning_kind({"title": item.title, "body": item.description}):
                    # Only Issues are on GitHub. What calls itself an Epic or a User
                    # Story is a record here and is never written there.
                    action.status, action.detail = "blocked", NOT_AN_ISSUE
                    session.flush()
                    session.expunge(action)
                    return action, "", ""
                title, body = item.title, item.description or ""
            if action.operation == "label":
                item = session.get(BacklogItem, action.local_id)
                if (
                    not item
                    or item.project_id != project.id
                    or not item.type_confirmed
                    or item.issue_number != action.number
                    or (item.repository or project.source_repository) != action.repository
                    or item.execution_type not in ("bug", "enhancement")
                ):
                    raise ProjectConflict("The Issue no longer matches this classification")
                title = item.execution_type
            action.lease, action.lease_until = uuid4(), now + timedelta(minutes=5)
            session.flush()
            session.expunge(action)
            return action, title, body

    def _first_labels(self, action: WorkspaceAction) -> list[str]:
        """The labels a new issue is created with: bug or enhancement, where the
        administrator said which it is, and for a bug the severity it was given."""
        with self.sessions() as db:
            item = db.get(BacklogItem, action.local_id)
            if not (item and item.type_confirmed and item.execution_type in ("bug", "enhancement")):
                return []
            labels = [item.execution_type]
            severity = db.scalar(
                select(BugRecord.severity).where(
                    BugRecord.project_id == item.project_id, BugRecord.item_id == item.id
                )
            )
            if item.execution_type == "bug" and severity:
                labels.append(f"severity: {severity}")
            return labels

    def _approved_planning_kind(self, action: WorkspaceAction) -> str | None:
        with self.sessions() as db:
            candidate = db.scalar(
                select(WorkspaceCandidate)
                .join(ProjectRecord, ProjectRecord.id == WorkspaceCandidate.project_id)
                .where(
                    ProjectRecord.id == action.project_id,
                    ProjectRecord.source_repository == action.repository,
                    WorkspaceCandidate.local_id == action.local_id,
                    WorkspaceCandidate.number == action.number,
                    WorkspaceCandidate.decision == "approved",
                    WorkspaceCandidate.kind.in_(("epics", "stories")),
                )
            )
            if candidate is None:
                return None
            source = db.get(WorkspaceSource, (action.project_id, candidate.identity))
            if not source or planning_kind(source.evidence) != candidate.kind:
                return None
            model = ProjectEpic if candidate.kind == "epics" else ProjectUserStory
            active = db.scalar(
                select(model.id).where(
                    model.project_id == action.project_id,
                    model.id == action.local_id,
                    model.archived_at.is_(None),
                )
            )
            return candidate.kind if active is not None else None

    def _update(
        self,
        name: str,
        claimed: WorkspaceAction,
        *,
        sent: bool = False,
        retained: bool = False,
        number: int | None = None,
        snapshot: dict[str, Any] | None = None,
        error: str | None = None,
        blocked: str | None = None,
        category: str | None = None,
        labels: list[str] | None = None,
    ) -> str | None:
        with self.sessions.begin() as session:
            project = active_project(session, name, lock=True)
            action = session.get(WorkspaceAction, claimed.id)
            if action is None or action.project_id != project.id or action.lease != claimed.lease:
                raise ProjectConflict("Synchronization was resumed in another session")
            if sent:
                action.status = "uncertain"
                return None
            action.lease = action.lease_until = None
            if blocked:
                action.status, action.detail = "blocked", blocked
                return None
            if retained:
                action.status = "retained"
                action.detail = (
                    "GitHub source retained because its planning type could not be verified."
                )
                return None
            action.detail = error[:350] if error else None
            if error:
                return None
            if action.operation == CLOSE_ISSUE:
                item = session.get(BacklogItem, action.local_id)
                if (
                    not item
                    or (item.project_id, item.issue_number) != (project.id, number)
                    or (item.repository or project.source_repository).casefold()
                    != action.repository.casefold()
                ):
                    action.status, action.detail = (
                        "blocked",
                        "The local Issue changed during synchronization.",
                    )
                    return action.detail
                closed = (snapshot or {}).get("closed_at")
                updated = (snapshot or {}).get("updated_at")
                item.issue_state = "closed"
                item.github_closed_at = (
                    datetime.fromisoformat(closed.replace("Z", "+00:00"))
                    if closed
                    else datetime.now(UTC)
                )
                item.state, item.done_at = "done", item.github_closed_at
                # Why it was closed, as GitHub has it now: known here at once, not only
                # after the repository is read again.
                item.facts = {
                    **(item.facts or {}),
                    "github_state_reason": (snapshot or {}).get("state_reason"),
                }
                if updated:
                    item.issue_updated_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                source = session.get(WorkspaceSource, (project.id, f"issue:{number}"))
                if source and action.repository.casefold() == project.source_repository.casefold():
                    source.evidence = {
                        **source.evidence,
                        "state": "closed",
                        "closed_at": closed,
                        "updated_at": updated,
                    }
            if action.operation == "label":
                item = session.get(BacklogItem, action.local_id)
                if item and item.execution_type != category:
                    action.status, action.detail = "pending", None
                    return None
                if item and labels is not None:
                    item.issue_labels = labels
            if action.operation in ("create", "link"):
                item = session.get(BacklogItem, action.local_id)
                if item is None or number is None:
                    raise ProjectConflict("The published issue could not be reconciled")
                item.repository = action.repository
                item.source = "github"
                item.issue_number = action.number = number
                item.issue_url = f"https://github.com/{action.repository}/issues/{number}"
                state = (snapshot or {}).get("state", "open")
                if state == "closed":
                    closed = (snapshot or {}).get("closed_at")
                    item.github_closed_at = (
                        datetime.fromisoformat(closed.replace("Z", "+00:00")) if closed else None
                    )
                    if item.state in ("proposed", "accepted", "done"):
                        item.state = "done"
                        item.done_at = item.github_closed_at
                elif item.issue_state == "closed":
                    item.github_closed_at = None
                    if item.state == "done":
                        item.state = "proposed"
                item.issue_state = state
                if snapshot:
                    item.facts = {
                        **(item.facts or {}),
                        "github_state_reason": snapshot.get("state_reason"),
                    }
                item.workspace_review = "approved"
                waiting = session.scalar(
                    select(WorkspaceAction).where(
                        WorkspaceAction.project_id == project.id,
                        WorkspaceAction.operation == CLOSE_ISSUE,
                        WorkspaceAction.local_id == item.id,
                        WorkspaceAction.status == "waiting",
                    )
                )
                if waiting:
                    waiting.number, waiting.status = number, "pending"
                    item.state = "done"
                schedule_classification(session, project, item)
            action.status = "done"
        return None

    async def sync(self, name: str, key: UUID, github: GitHubApp | None) -> dict[str, Any]:
        action, title, body = await asyncio.to_thread(self._claim, name, key)
        if action.status in ("done", "retained"):
            return {"project": name, "status": action.status}
        if action.status == "blocked":
            return {"project": name, "status": "needs_attention", "detail": action.detail}
        number = action.number
        snapshot = None
        labels = None
        try:
            if github is None:
                raise GitHubAppError("Configure the GitHub App to synchronize this approval")
            marker = f"<!-- engineering-control:workspace-issue:{action.id} -->"
            if action.operation == "link" and number:
                snapshot = await github.workspace_issue(action.repository, number)
            elif action.operation == "create" and action.status == "uncertain":
                number = await github.workspace_published(action.repository, marker)
                if number is None:
                    raise GitHubAppError(
                        "The creation outcome is uncertain. Recheck GitHub before publishing again."
                    )
                snapshot = await github.workspace_issue(action.repository, number)
            elif action.operation == CLOSE_ISSUE and number:
                snapshot = await github.workspace_issue(action.repository, number)
                if planning_kind(snapshot):
                    reason = (
                        "GitHub now identifies this record as an Epic or Story. Review its type."
                    )
                    await asyncio.to_thread(self._update, name, action, blocked=reason)
                    return {"project": name, "status": "needs_attention", "detail": reason}
                if snapshot["state"] != "closed":
                    token = await github.repository_token(action.repository, {"issues": "write"})
                    try:
                        if action.note:
                            # A closure the clean-up asked for says why on the issue,
                            # once, before it closes it.
                            await github.comment_workspace_issue(
                                action.repository,
                                number,
                                action.note,
                                f"<!-- engineering-control:closure:{action.id} -->",
                                token=token,
                            )
                        if action.state_reason:
                            await github.close_workspace_issue(
                                action.repository,
                                number,
                                token=token,
                                reason=action.state_reason,
                            )
                        else:
                            await github.close_workspace_issue(
                                action.repository, number, token=token
                            )
                    finally:
                        await github.revoke(token)
                    snapshot = await github.workspace_issue(action.repository, number)
                    if snapshot["state"] != "closed":
                        raise GitHubAppError(
                            "GitHub has not confirmed the technical Issue closure."
                        )
            elif action.operation == "close" and number:
                expected = await asyncio.to_thread(self._approved_planning_kind, action)
                current = (
                    await github.workspace_issue(action.repository, number) if expected else {}
                )
                if expected is None or planning_kind(current) != expected:
                    await asyncio.to_thread(self._update, name, action, retained=True)
                    return {"project": name, "status": "retained", "number": number}
                if current["state"] != "closed":
                    token = await github.repository_token(action.repository, {"issues": "write"})
                    try:
                        await github.close_workspace_issue(action.repository, number, token=token)
                        LOGGER.info(
                            "Closed GitHub planning issue project=%s issue=%d kind=%s action=%s",
                            name,
                            number,
                            expected,
                            key,
                        )
                    finally:
                        await github.revoke(token)
            else:
                token = await github.repository_token(action.repository, {"issues": "write"})
                try:
                    if action.operation == "label" and number:
                        labels = await github.classify_workspace_issue(
                            action.repository, number, title, token=token
                        )
                    elif action.operation == "create":
                        first = await asyncio.to_thread(self._first_labels, action)
                        await asyncio.to_thread(self._update, name, action, sent=True)
                        text = body.rstrip() + "\n\n" + marker
                        number = await (
                            # What you called a bug or an enhancement is one from the
                            # moment it is created, and a bug has its severity then too.
                            github.create_workspace_issue(
                                action.repository, title, text, token=token, labels=first
                            )
                            if first
                            else github.create_workspace_issue(
                                action.repository, title, text, token=token
                            )
                        )
                    else:
                        raise GitHubAppError("Invalid GitHub approval record")
                finally:
                    await github.revoke(token)
        except GitHubAppError as error:
            await asyncio.to_thread(self._update, name, action, error=str(error))
            return {"project": name, "status": "needs_attention", "detail": str(error)}
        problem = await asyncio.to_thread(
            self._update,
            name,
            action,
            number=number,
            snapshot=snapshot,
            category=title,
            labels=labels,
        )
        if problem:
            return {"project": name, "status": "needs_attention", "detail": problem}
        return {"project": name, "status": "done", "number": number}

    def due(self) -> list[tuple[str, UUID]]:
        now = datetime.now(UTC)
        with self.sessions() as session:
            return [
                (name, key)
                for name, key in session.execute(
                    select(ProjectRecord.name, WorkspaceAction.id)
                    .join(WorkspaceAction, WorkspaceAction.project_id == ProjectRecord.id)
                    .where(
                        ProjectRecord.archived_at.is_(None),
                        (WorkspaceAction.status == "pending")
                        | (
                            (WorkspaceAction.status == "uncertain")
                            & WorkspaceAction.detail.is_(None)
                        ),
                        WorkspaceAction.lease_until.is_(None) | (WorkspaceAction.lease_until < now),
                        WorkspaceAction.detail.is_(None)
                        | (WorkspaceAction.updated_at < now - timedelta(minutes=15)),
                    )
                    .order_by(WorkspaceAction.created_at)
                    .limit(20)
                )
            ]

    def outstanding(self, name: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            project = active_project(session, name)
            return [
                action_row(a)
                for a in session.scalars(
                    select(WorkspaceAction)
                    .where(
                        WorkspaceAction.project_id == project.id,
                        WorkspaceAction.status.not_in(("done", "retained")),
                    )
                    .order_by(WorkspaceAction.created_at)
                )
            ]
