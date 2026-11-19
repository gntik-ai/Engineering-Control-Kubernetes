"""Classify the issue itself and mirror technical work without changing GitHub."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import clean_description
from engineering_control.dashboard.project_records import ProjectConflict, active_project
from engineering_control.db.models import (
    BacklogItem,
    ProjectIssueStory,
    ProjectRecord,
    ProjectUserStory,
    WorkspaceCandidate,
    WorkspaceImport,
    WorkspaceSource,
)
from engineering_control.github_app import GitHubApp

LOGGER = logging.getLogger(__name__)

PLANNING = {"epic": "epics", "story": "stories", "user story": "stories"}
TECHNICAL = {"bug", "task", "technical task", "technical issue", "change request", "chore"}
TITLE_KIND = re.compile(
    r"^\s*(?:\[(epic|user[ -]story|story|task|bug|chore|change request)\]|"
    r"(epic|user[ -]story|story|task|bug|chore|change request)\s*:)",
    re.I,
)
STORY_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:parent\s+)?(?:user\s+)?stor(?:y|ies)\s*:\s*(.*)$", re.I | re.M
)
STORY_REF = re.compile(
    r"(?:https://github\.com/([\w.-]+/[\w.-]+)/issues/|"
    r"(?:([\w.-]+/[\w.-]+))?#)([1-9]\d*)",
    re.I,
)


def planning_kind(issue: dict[str, Any]) -> str | None:
    """Only an explicit, unambiguous self-designation can authorize a source closure."""
    labels = [v.get("name") if isinstance(v, dict) else v for v in issue.get("labels") or []]
    issue_type = issue.get("type")
    markers = [
        issue.get("issue_type"),
        issue_type.get("name") if isinstance(issue_type, dict) else issue_type,
        *labels,
    ]
    body = str(issue.get("body") or "")
    header = re.match(r"^\s*(?:---\s*\n)?(?:type|kind)\s*:\s*([\w -]+)\s*(?:\n|$)", body, re.I)
    if header:
        markers.append(header[1])
    prefix = TITLE_KIND.match(issue.get("title") or "")
    if prefix:
        markers.append(prefix.group(1) or prefix.group(2))
    tags = {
        re.sub(r"^(?:type|kind)\s*[:/]\s*", "", re.sub(r"[-_]+", " ", m.strip().casefold()))
        for m in markers
        if isinstance(m, str)
    }
    # A change request can quote its upstream Epic and Story in the same template.
    if tags & TECHNICAL or re.search(
        r"^#{1,3}\s+Change request\s*$", issue.get("body") or "", re.I | re.M
    ):
        return None
    kinds = {PLANNING[t] for t in tags if t in PLANNING}
    return kinds.pop() if len(kinds) == 1 else None


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _story_numbers(issue: dict[str, Any], repository: str) -> set[int]:
    return {
        int(ref[3])
        for line in STORY_LINE.findall(issue.get("body") or "")
        for ref in STORY_REF.finditer(line)
        if (ref[1] or ref[2] or repository).lower() == repository.lower()
    }


def add_story_links(db: Session, project: UUID, item: UUID, stories: set[UUID]) -> None:
    if not stories:
        return
    existing = set(
        db.scalars(
            select(ProjectIssueStory.story_id).where(
                ProjectIssueStory.project_id == project,
                ProjectIssueStory.item_id == item,
            )
        )
    )
    db.add_all(
        ProjectIssueStory(project_id=project, item_id=item, story_id=s) for s in stories - existing
    )


def import_issues(db: Session, project: ProjectRecord, issues: list[dict[str, Any]]) -> None:
    """Called under the project lock. A real issue keeps one row and its original number."""
    if not issues:
        return
    numbers = {i["number"] for i in issues}
    repo = project.source_repository
    items = {
        i.issue_number: i
        for i in db.scalars(
            select(BacklogItem).where(
                BacklogItem.project_id == project.id,
                func.lower(BacklogItem.repository) == repo.lower(),
                BacklogItem.issue_number.in_(numbers),
            )
        )
    }
    candidates = {
        c.number: c
        for c in db.scalars(
            select(WorkspaceCandidate).where(
                WorkspaceCandidate.project_id == project.id,
                WorkspaceCandidate.number.in_(numbers),
            )
        )
    }
    sources = {
        s.key: s
        for s in db.scalars(
            select(WorkspaceSource).where(
                WorkspaceSource.project_id == project.id,
                WorkspaceSource.key.in_([f"issue:{n}" for n in numbers]),
            )
        )
    }
    references = {n for issue in issues for n in _story_numbers(issue, repo)}
    references.update(
        n for source in sources.values() for n in _story_numbers(source.evidence, repo)
    )
    stories = {
        number: uid
        for number, uid, evidence in db.execute(
            select(WorkspaceCandidate.number, ProjectUserStory.id, WorkspaceSource.evidence)
            .join(ProjectUserStory, ProjectUserStory.id == WorkspaceCandidate.local_id)
            .join(
                WorkspaceSource,
                (WorkspaceSource.project_id == project.id)
                & (WorkspaceSource.key == WorkspaceCandidate.identity),
            )
            .where(
                WorkspaceCandidate.project_id == project.id,
                WorkspaceCandidate.kind == "stories",
                WorkspaceCandidate.number.in_(references),
                WorkspaceCandidate.decision == "approved",
                ProjectUserStory.project_id == project.id,
                ProjectUserStory.archived_at.is_(None),
            )
        )
        if number is not None and planning_kind(evidence) == "stories"
    }
    for issue in issues:
        n, date = issue["number"], _time(issue.get("updated_at"))
        key = f"issue:{n}"
        src = sources.get(key)
        old_date = _time(src.evidence.get("updated_at")) if src else None
        if src and old_date and (date is None or old_date > date):
            issue, date = src.evidence, old_date
        elif src:
            src.evidence = issue
        else:
            db.add(WorkspaceSource(project_id=project.id, key=key, kind="issue", evidence=issue))
        kind = planning_kind(issue)
        cand = candidates.get(n)
        item = items.get(n)
        if cand and cand.kind != (kind or "issues") and cand.decision == "pending":
            cand.decision, cand.number = "dismissed", None
        if kind:
            if item:
                item.workspace_review = "planning"
            continue
        if item is None:
            item = BacklogItem(
                project_id=project.id,
                source="github",
                repository=repo,
                issue_number=n,
                state="proposed",
                workspace_review="pending",
            )
            db.add(item)
        elif date and item.issue_updated_at and item.issue_updated_at.replace(tzinfo=UTC) > date:
            continue
        elif item.workspace_review == "planning":
            item.workspace_review = "pending"
        elif item.workspace_review is None:
            # An existing operational backlog record was already available for delivery.
            item.workspace_review = "approved"
        previous_state = item.issue_state
        item.title = (issue.get("title") or f"Issue {n}")[:300]
        item.description = clean_description(issue.get("body"))
        item.issue_state = issue["state"]
        item.github_closed_at = _time(issue.get("closed_at"))
        item.issue_url = issue.get("html_url") or f"https://github.com/{repo}/issues/{n}"
        item.issue_labels = issue.get("labels") or []
        item.issue_updated_at = date
        item.facts = {**(item.facts or {}), "github_state_reason": issue.get("state_reason")}
        if item.issue_state == "closed" and item.state in ("proposed", "accepted", "done"):
            item.state = "done"
            item.done_at = item.github_closed_at
        elif previous_state == "closed" and item.state == "done":
            item.state = "proposed"
        if item.facts.get("manual_story_links"):
            continue
        refs = {stories[n] for n in _story_numbers(issue, repo) if n in stories}
        if refs:
            db.flush()
            add_story_links(db, project.id, item.id, refs)
            if cand and cand.kind == "issues" and cand.decision == "pending":
                cand.parents = sorted(set(cand.parents or []) | {str(v) for v in refs})


class WorkspaceIssueInventory:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    async def refresh(self, name: str, github: GitHubApp, *, full: bool = False) -> None:
        def checkpoint() -> tuple[str, datetime | None] | None:
            with self.sessions() as db:
                project = active_project(db, name)
                row = db.get(WorkspaceImport, project.id)
                if (
                    row is None
                    or row.repository != project.source_repository
                    or row.stage in ("inventory", "files", "history", "analysis")
                ):
                    return None
                return row.repository, row.issues_synced_at

        def store(issues: list[dict[str, Any]], finished: bool) -> None:
            with self.sessions.begin() as db:
                project = active_project(db, name, lock=True)
                row = db.get(WorkspaceImport, project.id)
                if (
                    row is None
                    or row.repository != repository
                    or project.source_repository != repository
                ):
                    raise ProjectConflict("The repository changed during issue synchronization")
                import_issues(db, project, issues)
                if finished and (
                    row.issues_synced_at is None
                    or row.issues_synced_at.replace(tzinfo=UTC) < started
                ):
                    row.issues_synced_at = started

        snapshot = await asyncio.to_thread(checkpoint)
        if snapshot is None:
            return
        repository, previous = snapshot
        started = datetime.now(UTC)
        since = (
            None
            if full or not previous
            else (previous.replace(tzinfo=UTC) - timedelta(seconds=1)).isoformat()
        )
        page = 1
        scanned = technical = 0
        while True:
            issues, last = await github.workspace_history(repository, page, since=since)
            await asyncio.to_thread(store, issues, last)
            scanned += len(issues)
            technical += sum(planning_kind(i) is None for i in issues)
            if last:
                LOGGER.info(
                    "GitHub issue inventory completed project=%s mode=%s "
                    "technical=%d planning=%d pages=%d",
                    name,
                    "incremental" if since else "full",
                    technical,
                    scanned - technical,
                    page,
                )
                return
            page += 1
