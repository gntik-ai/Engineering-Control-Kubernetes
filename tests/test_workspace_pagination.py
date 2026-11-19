"""Project planning tables page proposals and saved records together."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from engineering_control.db.models import (
    BacklogItem,
    ProjectEpic,
    ProjectRecord,
    WorkspaceCandidate,
    WorkspaceImport,
)
from tests.test_dashboard_web import _authenticated_client

BASE = "/api/dashboard/projects/falcone/planning/"


def test_mixed_pages_and_approval_snapshot(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            now = datetime.now(UTC)
            db.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch="main",
                    stage="epics",
                    version=7,
                    progress={"review_mode": "review"},
                )
            )
            db.add_all(
                ProjectEpic(
                    id=UUID(int=n), project_id=project.id, title=f"Saved {n}", created_at=now
                )
                for n in (5, 4, 3)
            )
            db.add(ProjectEpic(project_id=project.id, title="Archived", archived_at=now))
            db.add_all(
                WorkspaceCandidate(
                    id=UUID(int=n),
                    project_id=project.id,
                    kind=kind,
                    identity=str(n),
                    title=f"Candidate {n}",
                    decision=decision,
                    created_at=now,
                )
                for n, kind, decision in (
                    (2, "epics", "pending"),
                    (1, "epics", "pending"),
                    (6, "epics", "dismissed"),
                    (7, "stories", "pending"),
                )
            )
        repo.planning.save("llmwiki", "epics", "Other project", None, [])
        pages = [client.get(BASE + f"epics?page={n}&page_size=2").json() for n in (1, 2, 3)]
        assert [[r["id"] for r in p["items"]] for p in pages] == [
            [str(UUID(int=n)) for n in group] for group in ([1, 2], [3, 4], [5])
        ]
        assert all(
            (p["total"], p["pending"], p["pages"], p["version"]) == (5, 2, 3, 7) for p in pages
        )
        assert not pages[0]["can_continue"] and all(r["proposal"] for r in pages[0]["items"])
        assert client.get(BASE + "stories").json()["items"] == []
        pending = client.get(BASE + "epics/pending").json()
        assert pending == {"ids": [str(UUID(int=n)) for n in (1, 2)], "version": 7}
        # Removing the last row takes a stale bookmark back to the last real page.
        repo.planning.remove("falcone", "epics", UUID(int=5))
        clamped = client.get(BASE + "epics?page=3&page_size=2").json()
        assert (clamped["requested_page"], clamped["page"], clamped["pages"]) == (3, 2, 2)
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]
        approved = client.post(BASE + "epics/decisions", json={**pending, "csrf_token": csrf})
        assert approved.status_code == 200, approved.text
        saved = client.get(BASE + "epics").json()
        assert saved["pending"] == 0 and saved["total"] == 4 and saved["can_continue"]
        assert client.get(BASE + "epics/pending").json() == {"ids": [], "version": 8}
    finally:
        client.__exit__(None, None, None)


def test_page_parent_titles_and_pending_issues(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:

        def epic(name: str) -> str:
            return repo.planning.save("falcone", "epics", name, None, [])["id"]

        first, second = epic("First"), epic("Second")
        left = repo.planning.save("falcone", "stories", "First story", None, [UUID(second)])["id"]
        right = repo.planning.save(
            "falcone", "stories", "Both Epics", None, [UUID(first), UUID(second)]
        )["id"]
        draft = repo.planning.save("falcone", "issues", "Local draft", None, [UUID(left)])["id"]
        candidate = uuid4()
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            db.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch="main",
                    stage="issues",
                    version=4,
                    progress={"review_mode": "gaps"},
                    lease=uuid4(),
                    lease_until=datetime.now(UTC) + timedelta(minutes=2),
                )
            )
            db.add(
                WorkspaceCandidate(
                    id=candidate,
                    project_id=project.id,
                    kind="issues",
                    identity="issue:41",
                    number=41,
                    title="Imported issue",
                    parents=[right],
                )
            )
            db.add(
                BacklogItem(
                    project_id=project.id,
                    title="Unlinked backlog issue",
                    source="manual",
                    state="proposed",
                )
            )
        story = client.get(BASE + "stories?page_size=1").json()
        assert story["has_parents"] and story["parents"] == {second: "Second"}
        assert story["items"][0]["epic_ids"] == [second]
        assert client.get(BASE + "stories/parents").json() == [
            {"id": first, "title": "First"},
            {"id": second, "title": "Second"},
        ]
        p1, p2 = [client.get(BASE + f"issues?page_size=1&page={n}").json() for n in (1, 2)]
        assert p1["busy"] and p1["pending"] == p1["total"] == 2
        assert p1["parents"] == {right: "Both Epics"} and p2["parents"] == {left: "First story"}
        assert (
            p1["items"][0]["url"].endswith("/issues/41") and p2["items"][0]["review"] == "pending"
        )
        assert client.get(BASE + "issues/pending").json() == {
            "ids": [str(candidate), draft],
            "version": 4,
        }
        assert client.get(BASE.replace("falcone", "llmwiki") + "issues/parents").json() == []
    finally:
        client.__exit__(None, None, None)


def test_empty_invalid_and_authenticated_pages(tmp_path: Path) -> None:
    client, _ = _authenticated_client(tmp_path)
    try:
        empty = client.get(BASE + "epics?page=99").json()
        assert (empty["page"], empty["pages"], empty["total"], empty["page_size"]) == (1, 1, 0, 10)
        assert empty["items"] == [] and not empty["busy"]
        for query in ("page=0", "page_size=0", "page_size=101", "page=abc"):
            assert client.get(BASE + "epics?" + query).status_code == 422
        assert client.get(BASE.replace("falcone", "unknown") + "epics").status_code == 404
        client.cookies.clear()
        for suffix in ("epics", "stories/parents", "issues/pending"):
            assert client.get(BASE + suffix).status_code == 401
    finally:
        client.__exit__(None, None, None)
