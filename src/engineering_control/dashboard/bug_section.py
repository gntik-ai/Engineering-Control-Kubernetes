"""The Bugs section: where a project's bugs are reported and managed on their own.

A bug stands apart from the plan (``bugs.py``, ``issue_types.py``): it belongs to no User
Story and to no Epic. This is its home. It is read from the project's issues and not from
the plan, so a project that was never through Discovery has it too.

- **Reporting.** One step: the administrator writes what happens, what was expected and
  how to reproduce it, and the issue is created on GitHub with the ``bug`` label in the
  same request. There is no proposal, no review and no approval between: the
  administrator wrote it, and it is not a planning record. A bug opened directly on
  GitHub arrives with the next reading of the repository.
- **Managing.** A bug's title and text can be changed; it is closed with a reason, fixed
  or not going to be fixed, which GitHub is told, with a comment where one is given; and
  it can be opened again. "Not a bug" says the issue is ordinary work: its ``bug`` label is
  taken off, and it then waits for a User Story like any other issue.

GitHub stays where a bug lives. Every change here is written there first and kept here as
GitHub answered it. A text that was changed on GitHub since it was read here is read
again instead of being overwritten.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import clean_description, clean_title
from engineering_control.dashboard.issue_types import bugs_with_optional_stories
from engineering_control.dashboard.planning import PlanningRecords
from engineering_control.dashboard.planning_lifecycle import ARCHIVED_DRAFT, CLOSED_DRAFT
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    ProjectNotFound,
    active_project,
)
from engineering_control.dashboard.workspace_publishing import (
    WorkspacePublishing,
    confirm_classification,
    enqueue,
)
from engineering_control.db.models import (
    BacklogItem,
    ProjectIssueStory,
    ProjectRecord,
    WorkspaceAction,
)
from engineering_control.github_app import GitHubApp, GitHubAppError

# What a report is made of, in the order and under the headings its issue carries.
PARTS: Final = (
    ("happens", "What happens"),
    ("expected", "What was expected"),
    ("steps", "How to reproduce"),
    ("seen_in", "Where it was seen"),
)
# How closing a bug is said here, and what GitHub is told.
REASONS: Final = {"fixed": "completed", "wont_fix": "not_planned"}
# What the platform leaves in an issue's text to know it again; never shown or edited.
MARKER: Final = re.compile(r"<!--\s*engineering-control:[^>]*-->")
LISTED: Final = 5000


def body_of(parts: dict[str, str]) -> str:
    """The text of a reported bug: each part that was filled in, under its heading."""
    said = [
        f"### {heading}\n\n{parts[key].strip()}"
        for key, heading in PARTS
        if (parts.get(key) or "").strip()
    ]
    return "\n\n".join(said)


def shown_text(text: str | None) -> str:
    """An issue's text without the platform's own marks."""
    return MARKER.sub("", text or "").strip()


def _same(one: str | None, other: str | None) -> bool:
    return shown_text(one).split() == shown_text(other).split()


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _labels(item: BacklogItem) -> list[str]:
    found = []
    for entry in item.issue_labels if isinstance(item.issue_labels, list) else []:
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(name, str) and name.strip():
            found.append(name.strip())
    return found


def _stands(item: BacklogItem) -> str:
    """Where a bug stands: open, closed, being created on GitHub, or a draft that was
    never published."""
    if item.issue_number is None:
        if item.workspace_review == "publishing":
            return "creating"
        return "closed" if item.workspace_review == CLOSED_DRAFT else "draft"
    return "closed" if item.issue_state == "closed" else "open"


def _bugs(db: Session, project: ProjectRecord) -> list[BacklogItem]:
    """Every bug of the project, whatever its plan knows of it."""
    return list(
        db.scalars(
            select(BacklogItem)
            .where(
                BacklogItem.project_id == project.id,
                BacklogItem.id.in_(bugs_with_optional_stories(db, project.id)),
                # An issue that is itself an Epic or a User Story is no bug, and a draft
                # that was deleted is gone.
                BacklogItem.workspace_review.is_distinct_from("planning"),
                BacklogItem.workspace_review.is_distinct_from(ARCHIVED_DRAFT),
            )
            .limit(LISTED)
        )
    )


def _reported(item: BacklogItem) -> datetime:
    said = _when((item.facts or {}).get("github_created_at")) or _when(item.created_at)
    return said or datetime.now(UTC)


def _row(item: BacklogItem, problems: dict[UUID, str]) -> dict[str, Any]:
    stands = _stands(item)
    reason = (item.facts or {}).get("github_state_reason") if stands == "closed" else None
    updated = _when(item.issue_updated_at)
    closed = _when(item.github_closed_at)
    return {
        "id": str(item.id),
        "number": item.issue_number,
        "url": item.issue_url,
        "title": item.title,
        "text": shown_text(item.description),
        "state": stands,
        "reason": reason if reason in ("completed", "not_planned") else None,
        "confirmed": bool(item.type_confirmed and item.execution_type == "bug"),
        "labels": _labels(item),
        "reported_at": _reported(item).isoformat(),
        "updated_at": updated.isoformat() if updated else None,
        "closed_at": closed.isoformat() if closed else None,
        "problem": problems.get(item.id),
    }


class BugSection:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        planning: PlanningRecords,
        publishing: WorkspacePublishing,
    ) -> None:
        self.sessions = sessions
        self.planning = planning
        self.publishing = publishing

    # ------------------------------------------------------------------ reading

    @staticmethod
    def _problems(db: Session, project: ProjectRecord) -> dict[UUID, str]:
        """What GitHub last refused, for the bugs a change of which is still to be made."""
        return {
            local: detail
            for local, detail in db.execute(
                select(WorkspaceAction.local_id, WorkspaceAction.detail).where(
                    WorkspaceAction.project_id == project.id,
                    WorkspaceAction.status.not_in(("done", "retained")),
                    WorkspaceAction.detail.is_not(None),
                )
            )
        }

    def overview(self, name: str) -> dict[str, Any]:
        """How many bugs the project has open and closed, whether one can be reported,
        and how many still carry a link to a User Story from before."""
        with self.sessions() as db:
            project = active_project(db, name)
            found = _bugs(db, project)
            counts = {"open": 0, "closed": 0, "creating": 0, "draft": 0}
            for item in found:
                counts[_stands(item)] += 1
            linked = int(
                db.scalar(
                    select(func.count(func.distinct(ProjectIssueStory.item_id))).where(
                        ProjectIssueStory.project_id == project.id,
                        ProjectIssueStory.item_id.in_([item.id for item in found]),
                    )
                )
                or 0
            )
            oldest = min(
                (_reported(item) for item in found if _stands(item) == "open"), default=None
            )
            return {
                "repository": project.source_repository or None,
                "can_report": bool(project.source_repository),
                "counts": counts,
                "linked": linked,
                "oldest_open": oldest.isoformat() if oldest else None,
                "parts": [{"key": key, "heading": heading} for key, heading in PARTS],
            }

    def listing(
        self, name: str, state: str, search: str, sort: str, page: int, page_size: int
    ) -> dict[str, Any]:
        """The project's bugs in one state, or all of them: the newest first, or the
        oldest, or the last changed."""
        wanted = " ".join(search.split()).casefold()
        with self.sessions() as db:
            project = active_project(db, name)
            problems = self._problems(db, project)
            rows = []
            for item in _bugs(db, project):
                stands = _stands(item)
                # What is being created, and a draft, stand with the open ones.
                group = "closed" if stands == "closed" else "open"
                if state and group != state:
                    continue
                if wanted and not (
                    wanted in item.title.casefold()
                    or wanted.lstrip("#") == str(item.issue_number or "")
                ):
                    continue
                rows.append(item)
            if sort == "updated":
                rows.sort(
                    key=lambda item: _when(item.issue_updated_at) or _reported(item), reverse=True
                )
            else:
                rows.sort(key=_reported, reverse=sort != "oldest")
            pages = max(1, (len(rows) + page_size - 1) // page_size)
            page = min(max(1, page), pages)
            return {
                "items": [
                    _row(item, problems) for item in rows[(page - 1) * page_size : page * page_size]
                ],
                "total": len(rows),
                "page": page,
                "pages": pages,
            }

    # ---------------------------------------------------------------- reporting

    def _draft(self, name: str, title: str, parts: dict[str, str]) -> tuple[UUID, UUID]:
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            if not project.source_repository:
                raise ProjectInvalid("This project has no repository to report a bug in.")
            if not (parts.get("happens") or "").strip():
                raise ProjectInvalid("Say what happens.")
            cleaned = clean_title(title)
            same = next(
                (
                    item
                    for item in _bugs(db, project)
                    if _stands(item) != "closed" and item.title.casefold() == cleaned.casefold()
                ),
                None,
            )
            if same is not None:
                known = f"#{same.issue_number}" if same.issue_number else "being created"
                raise ProjectConflict(f"A bug with this title is open already: {known}.")
            item = BacklogItem(
                project_id=project.id,
                source="manual",
                state="proposed",
                # Reporting is the administrator's word for creating it.
                workspace_review="publishing",
                title=cleaned,
                description=clean_description(body_of(parts)),
                execution_type="bug",
                type_confirmed=True,
                classification_reason="Reported as a bug by the administrator.",
            )
            db.add(item)
            db.flush()
            action = enqueue(db, project.id, "create", item.id, project.source_repository)
            return item.id, action.id

    async def report(
        self, name: str, title: str, parts: dict[str, str], github: GitHubApp | None
    ) -> dict[str, Any]:
        """Report a bug: its issue is created on GitHub now, with the ``bug`` label. Where
        GitHub cannot be reached the bug is kept and created as soon as it can."""
        key, action = await asyncio.to_thread(self._draft, name, title, parts)
        result = await self.publishing.sync(name, action, github)
        return {"id": str(key), **result}

    # ----------------------------------------------------------------- changing

    def _published(self, name: str, key: UUID) -> tuple[str, int, str, str, str]:
        """The bug as it is held here: repository, number, title, text and state."""
        with self.sessions() as db:
            project = active_project(db, name)
            item = db.get(BacklogItem, key)
            if (
                item is None
                or item.project_id != project.id
                or item.id not in {row.id for row in _bugs(db, project)}
            ):
                raise ProjectNotFound(str(key))
            if item.issue_number is None:
                raise ProjectConflict("This bug is not on GitHub yet.")
            return (
                item.repository or project.source_repository,
                item.issue_number,
                item.title,
                item.description or "",
                item.issue_state or "open",
            )

    def _kept(self, name: str, key: UUID, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Keep the bug as GitHub has it now."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            item = db.get(BacklogItem, key)
            if item is None or item.project_id != project.id:
                raise ProjectNotFound(str(key))
            item.title = clean_title(str(snapshot.get("title") or item.title))
            item.description = clean_description(snapshot.get("body"))
            state = "closed" if snapshot.get("state") == "closed" else "open"
            closed = _when(snapshot.get("closed_at"))
            item.issue_state = state
            item.github_closed_at = closed if state == "closed" else None
            item.issue_updated_at = _when(snapshot.get("updated_at")) or datetime.now(UTC)
            item.facts = {**(item.facts or {}), "github_state_reason": snapshot.get("state_reason")}
            if state == "closed":
                item.state, item.done_at = "done", closed or datetime.now(UTC)
            elif item.state == "done":
                item.state, item.done_at = "proposed", None
            item.updated_at = datetime.now(UTC)
            db.flush()
            return _row(item, {})

    async def edit(
        self, name: str, key: UUID, title: str, text: str, github: GitHubApp | None
    ) -> dict[str, Any]:
        """Change a bug's title and text on GitHub. A text that was changed there since it
        was read here is read again, and nothing is overwritten."""
        repository, number, _title, held, _state = await asyncio.to_thread(
            self._published, name, key
        )
        cleaned = clean_title(title)
        wanted = clean_description(text) or ""
        if github is None:
            raise ProjectConflict("Configure the GitHub App to change a bug on GitHub.")
        try:
            snapshot = await github.workspace_issue(repository, number)
            current = str(snapshot.get("body") or "")
            if not _same(current, held):
                await asyncio.to_thread(self._kept, name, key, snapshot)
                raise ProjectConflict(
                    "This bug was changed on GitHub since it was read here. It is read again "
                    "now: look at it and make your change again."
                )
            marks = "\n".join(MARKER.findall(current))
            token = await github.repository_token(repository, {"issues": "write"})
            try:
                changed = await github.update_workspace_issue(
                    repository,
                    number,
                    token=token,
                    title=cleaned,
                    body=wanted + ("\n\n" + marks if marks else ""),
                )
            finally:
                await github.revoke(token)
        except GitHubAppError as error:
            raise ProjectConflict(str(error)) from error
        return await asyncio.to_thread(self._kept, name, key, changed)

    def close(self, name: str, key: UUID, reason: str, note: str) -> dict[str, Any]:
        """Close a bug as fixed or as not going to be fixed. GitHub is told the reason,
        and the note, where one is given, is left on the issue as a comment."""
        if reason not in REASONS:
            raise ProjectInvalid("A bug is closed as fixed or as not going to be fixed.")
        with self.sessions() as db:
            project = active_project(db, name)
            if key not in {row.id for row in _bugs(db, project)}:
                raise ProjectNotFound(str(key))
        said = (clean_description(note) or "")[:4000]
        impact = self.planning.lifecycle.preview(name, "issues", key, "close")
        mine = next((entry for entry in impact["items"] if entry["id"] == str(key)), None)
        if mine is None or mine["action"] != "close":
            raise ProjectConflict("This bug is closed already, or its closure is under way.")
        return self.planning.lifecycle.confirm(
            name, "issues", key, "close", impact["token"], closure=(REASONS[reason], said)
        )

    async def reopen(self, name: str, key: UUID, github: GitHubApp | None) -> dict[str, Any]:
        """Open a closed bug again, on GitHub and here."""
        repository, number, _title, _held, state = await asyncio.to_thread(
            self._published, name, key
        )
        if state != "closed":
            raise ProjectConflict("This bug is open.")
        if github is None:
            raise ProjectConflict("Configure the GitHub App to open a bug again on GitHub.")
        try:
            token = await github.repository_token(repository, {"issues": "write"})
            try:
                snapshot = await github.reopen_workspace_issue(repository, number, token=token)
            finally:
                await github.revoke(token)
        except GitHubAppError as error:
            raise ProjectConflict(str(error)) from error
        return await asyncio.to_thread(self._kept, name, key, snapshot)

    def not_a_bug(self, name: str, key: UUID) -> dict[str, Any]:
        """Say that the issue is ordinary work and no bug. Its ``bug`` label is taken off
        on GitHub; where the project has a plan it then waits for a User Story like any
        other issue."""
        with self.sessions.begin() as db:
            project = active_project(db, name, lock=True)
            item = db.get(BacklogItem, key)
            if (
                item is None
                or item.project_id != project.id
                or item.id not in {row.id for row in _bugs(db, project)}
            ):
                raise ProjectNotFound(str(key))
            confirm_classification(db, project, item, "enhancement")
            return {"id": str(key), "bug": False}
