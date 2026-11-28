"""A failing check of the default branch becomes a draft bug (B5 of
docs/design/bugs-section.md, the part that asks no model).

On the administrator's word the checks GitHub ran on the last commit of the project's
default branch are read. Each check that ended badly, and that no bug still open stands
for, becomes a bug kept here as a draft: it is not on GitHub, and nothing is done about
it, until the administrator reports it there, or deletes it. Nothing is read by itself.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from sqlalchemy.orm import Session, sessionmaker

from engineering_control.dashboard.backlog_items import clean_description, clean_title
from engineering_control.dashboard.bug_section import _bugs, _stands, body_of
from engineering_control.dashboard.project_records import (
    ProjectConflict,
    ProjectInvalid,
    active_project,
)
from engineering_control.db.models import BacklogItem
from engineering_control.github_app import GitHubApp, GitHubAppError

# What a draft made from a check carries, to know the check again.
CHECK: Final = "failing_check"
COMMIT: Final = "failing_commit"
SAID: Final = {
    "none": "GitHub ran no check on it.",
    "pending": "Its checks are still running.",
    "failed": "At least one check ended badly.",
    "passed": "Every check passed.",
}


def _where(sessions: sessionmaker[Session], name: str) -> tuple[str, str]:
    with sessions() as db:
        project = active_project(db, name)
        if not project.source_repository:
            raise ProjectInvalid("This project has no repository whose checks could be read.")
        return project.source_repository, project.default_branch or "main"


def _draft(
    sessions: sessionmaker[Session],
    name: str,
    repository: str,
    branch: str,
    commit: str,
    failed: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep a draft bug for each check that ended badly and has none yet. Returns the
    drafts made, and the bugs that stood for a check already."""
    made: list[dict[str, Any]] = []
    known: list[dict[str, Any]] = []
    with sessions.begin() as db:
        project = active_project(db, name, lock=True)
        standing = {
            str((item.facts or {}).get(CHECK)): item
            for item in _bugs(db, project)
            if _stands(item) != "closed" and (item.facts or {}).get(CHECK)
        }
        for check in dict.fromkeys(failed):
            before = standing.get(check)
            if before is not None:
                known.append(
                    {
                        "check": check,
                        "id": str(before.id),
                        "number": before.issue_number,
                        "title": before.title,
                    }
                )
                continue
            item = BacklogItem(
                project_id=project.id,
                source="manual",
                state="proposed",
                # A draft: it waits for the administrator, and is not on GitHub.
                workspace_review="pending",
                title=clean_title(f"The check “{check}” fails on {branch}"),
                description=clean_description(
                    body_of(
                        {
                            "happens": f"The check “{check}” ended badly on the branch "
                            f"{branch}, at commit {commit[:12]}.",
                            "expected": f"Every check of {branch} passes.",
                            "steps": "Open the checks of the commit on GitHub: "
                            f"https://github.com/{repository}/commit/{commit}/checks",
                            "seen_in": f"{branch} at {commit[:12]}",
                        }
                    )
                ),
                execution_type="bug",
                type_confirmed=True,
                classification_reason="A check of the default branch ended badly.",
                issue_labels=[],
                facts={CHECK: check, COMMIT: commit},
            )
            db.add(item)
            db.flush()
            standing[check] = item
            made.append({"check": check, "id": str(item.id), "title": item.title})
    return made, known


async def read(
    sessions: sessionmaker[Session], name: str, github: GitHubApp | None
) -> dict[str, Any]:
    """Read the checks of the default branch's last commit, and keep a draft bug for
    each that ended badly and has no bug yet."""
    repository, branch = await asyncio.to_thread(_where, sessions, name)
    if github is None:
        raise ProjectConflict("Configure the GitHub App to read the checks of a branch.")
    try:
        token = await github.repository_token(repository, {"contents": "read"})
        try:
            commit = await github.branch_head(repository, branch, token=token)
        finally:
            await github.revoke(token)
        checks = await github.checks(repository, commit)
    except GitHubAppError as error:
        raise ProjectConflict(str(error)) from error
    made, known = await asyncio.to_thread(
        _draft, sessions, name, repository, branch, commit, checks.failed
    )
    return {
        "repository": repository,
        "branch": branch,
        "commit": commit,
        "url": f"https://github.com/{repository}/commit/{commit}/checks",
        "state": checks.state,
        "says": SAID.get(checks.state, ""),
        "total": checks.total,
        "failed": list(checks.failed),
        "pending": list(checks.pending),
        "drafted": made,
        "known": known,
    }
