"""Publish only recorded Superadmin approvals and reconcile uncertain GitHub responses."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectNotFound,
    active_project,
)
from engineering_control.db.models import BacklogItem, ProjectRecord, WorkspaceAction
from engineering_control.github_app import GitHubApp, GitHubAppError


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
    if item.issue_number:
        item.workspace_review = "publishing"
        return enqueue(
            session, project.id, "link", key, project.source_repository, item.issue_number
        ).id
    item.workspace_review = "publishing"
    return enqueue(session, project.id, "create", key, project.source_repository).id


class WorkspacePublishing:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def approve(self, name: str, key: UUID) -> UUID | None:
        with self.sessions.begin() as session:
            project = active_project(session, name, lock=True)
            return approve_issue(session, project, key)

    def _claim(self, name: str, key: UUID) -> tuple[WorkspaceAction, str, str] | None:
        with self.sessions.begin() as session:
            project = active_project(session, name, lock=True)
            action = session.get(WorkspaceAction, key)
            if action is None or action.project_id != project.id:
                raise ProjectNotFound(str(key))
            if action.status == "done":
                return None
            now = datetime.now(UTC)
            if action.lease_until and action.lease_until.replace(tzinfo=UTC) > now:
                raise ProjectConflict("Synchronization is already running")
            title = body = ""
            if action.operation == "create":
                item = session.get(BacklogItem, action.local_id)
                if item is None or item.workspace_review != "publishing":
                    raise ProjectConflict("This issue no longer has a recorded approval")
                title, body = item.title, item.description or ""
            action.lease, action.lease_until = uuid4(), now + timedelta(minutes=5)
            session.flush()
            session.expunge(action)
            return action, title, body

    def _update(
        self,
        name: str,
        claimed: WorkspaceAction,
        *,
        sent: bool = False,
        number: int | None = None,
        snapshot: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self.sessions.begin() as session:
            project = active_project(session, name, lock=True)
            action = session.get(WorkspaceAction, claimed.id)
            if action is None or action.project_id != project.id or action.lease != claimed.lease:
                raise ProjectConflict("Synchronization was resumed in another session")
            if sent:
                action.status = "uncertain"
                return
            action.lease = action.lease_until = None
            action.detail = error[:350] if error else None
            if error:
                return
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
            action.status = "done"

    async def sync(self, name: str, key: UUID, github: GitHubApp | None) -> dict[str, Any]:
        work = await asyncio.to_thread(self._claim, name, key)
        if work is None:
            return {"project": name, "status": "done"}
        action, title, body = work
        number = action.number
        snapshot = None
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
            else:
                token = await github.repository_token(action.repository, {"issues": "write"})
                try:
                    if action.operation == "close" and number:
                        current = await github.workspace_issue(action.repository, number)
                        if current["state"] != "closed":
                            await github.close_workspace_issue(
                                action.repository, number, token=token
                            )
                    elif action.operation == "create":
                        await asyncio.to_thread(self._update, name, action, sent=True)
                        number = await github.create_workspace_issue(
                            action.repository, title, body.rstrip() + "\n\n" + marker, token=token
                        )
                    else:
                        raise GitHubAppError("Invalid GitHub approval record")
                finally:
                    await github.revoke(token)
        except GitHubAppError as error:
            await asyncio.to_thread(self._update, name, action, error=str(error))
            return {"project": name, "status": "needs_attention", "detail": str(error)}
        await asyncio.to_thread(self._update, name, action, number=number, snapshot=snapshot)
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
                        WorkspaceAction.project_id == project.id, WorkspaceAction.status != "done"
                    )
                    .order_by(WorkspaceAction.created_at)
                )
            ]
