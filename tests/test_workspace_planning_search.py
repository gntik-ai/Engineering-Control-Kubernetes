"""Filtered planning pages and stable links from User Stories to their Epics."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from engineering_control.db.models import (
    ProjectEpic,
    ProjectRecord,
    WorkspaceCandidate,
    WorkspaceImport,
    WorkspaceSource,
)
from tests.test_dashboard_web import _authenticated_client
from tests.test_workspace_planning import confirmed

BASE = "/api/dashboard/projects/falcone/planning/"


def test_local_lifecycle_preserves_links_and_resolves_closed_deep_links(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        epic = repo.planning.save("falcone", "epics", "Runtime", None, [])["id"]
        story = repo.planning.save("falcone", "stories", "Call a function", None, [UUID(epic)])[
            "id"
        ]
        task = repo.planning.save("falcone", "issues", "Send a request", None, [UUID(story)])["id"]
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]

        def change(kind: str, key: str, state: str):
            path = BASE + kind + "/" + key
            body = {"csrf_token": csrf, "state": state}
            if state == "closed":
                preview = client.get(path + "/impact?operation=close")
                if preview.status_code != 200:
                    return preview
                body["confirmation"] = preview.json()["token"]
            return client.post(path + "/state", json=body)

        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            db.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch="main",
                    stage="epics",
                )
            )
            db.flush()
            db.add(
                WorkspaceSource(
                    project_id=project.id,
                    kind="issue",
                    key="issue:11",
                    evidence={"state": "closed"},
                )
            )
            proposal = WorkspaceCandidate(
                project_id=project.id,
                kind="epics",
                identity="issue:11",
                number=11,
                title="Runtime proposal",
                parents=[],
            )
            db.add(proposal)
            db.flush()
            proposal_id = str(proposal.id)
        assert change("epics", proposal_id, "closed").status_code == 404
        assert (
            client.post(
                BASE + f"issues/{task}/state", json={"csrf_token": csrf, "state": "closed"}
            ).status_code
            == 409
        )
        other = repo.planning.save("llmwiki", "epics", "Elsewhere", None, [])["id"]
        assert change("epics", other, "closed").status_code == 404
        for kind, key in (("epics", epic), ("stories", story)):
            assert change(kind, key, "closed").status_code == 200
            assert change(kind, key, "closed").status_code == 200
            assert client.get(BASE + kind + "/pending?state=closed").json()["ids"] == []
            closed = client.get(BASE + kind, params={"state": "closed", "search": "Runtime"}).json()
            assert [r["id"] for r in closed["items"]] == [key]
            assert (
                closed["items"][0]["local_state"] == "closed"
                and closed["state_counts"]["closed"] == 1
            )
            focus = "epic" if kind == "epics" else "record"
            deep = client.get(BASE + kind, params={"state": "open", focus: key}).json()
            assert deep["state"] == "closed" and deep["total"] == 1
        assert closed["items"][0]["epic_ids"] == [epic]
        assert closed["parents"][epic] == "Runtime"
        opened = client.get(BASE + "epics?state=open").json()
        assert opened["total"] == 1 and opened["items"][0]["id"] == proposal_id
        assert opened["items"][0]["github_state"] == "closed" and opened["pending"] == 1
        assert client.get(BASE + "issues?issue_state=draft").json()["items"][0]["story_ids"] == [
            story
        ]
        for kind, key in (("stories", story), ("epics", epic)):
            assert change(kind, key, "open").status_code == 200
            assert client.get(BASE + kind + "?state=closed").json()["items"] == []
        assert client.get("/api/dashboard/projects/falcone/synchronization").json()["items"] == []
    finally:
        client.__exit__(None, None, None)


def test_search_matches_text_numbers_and_parents_before_paging(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        save = repo.planning.save
        epic = UUID(save("falcone", "epics", "Payments 100%_ready", None, [])["id"])
        near = UUID(save("falcone", "epics", "Payments 100XYready", "Billing", [])["id"])
        first = save("falcone", "stories", "Checkout", "Invoice copy", [epic, near])["id"]
        second = save("falcone", "stories", "Accounting", "Invoice totals", [near])["id"]
        local_issue = save("falcone", "issues", "Calculate tax", "Pennies", [UUID(first)])["id"]
        save("falcone", "issues", "Retention", None, [UUID(second)])
        save("llmwiki", "epics", "Only elsewhere", None, [])
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            pid = project.id
            db.add(
                WorkspaceImport(
                    project_id=pid,
                    repository=project.source_repository,
                    branch="main",
                    stage="stories",
                    version=1,
                    progress={"review_mode": "review"},
                )
            )
            db.add_all(
                [
                    WorkspaceCandidate(
                        project_id=pid,
                        kind="stories",
                        identity="story:18",
                        number=18,
                        title="Invoice draft",
                        parents=[str(epic)],
                    ),
                    WorkspaceCandidate(
                        project_id=pid,
                        kind="issues",
                        identity="issue:27",
                        number=27,
                        title="Imported proposal",
                        parents=[first],
                    ),
                ]
            )

        def search(kind: str, text: str, page: int = 1) -> dict:
            response = client.get(
                BASE + kind, params={"search": text, "page": page, "page_size": 1}
            )
            assert response.status_code == 200, response.text
            return response.json()

        assert search("epics", "  %_rEadY  ")["items"][0]["id"] == str(epic)
        assert search("epics", "%_ready")["total"] == 1
        assert search("epics", "billing")["items"][0]["id"] == str(near)
        assert search("epics", "elsewhere")["total"] == 0
        pages = [search("stories", "invoice", n) for n in (1, 2, 3)]
        assert [p["items"][0]["title"] for p in pages] == [
            "Invoice draft",
            "Checkout",
            "Accounting",
        ]
        assert all(p["total"] == p["pages"] == 3 and p["search"] == "invoice" for p in pages)
        assert (
            search("stories", "PAYMENTS")["total"] == 3
        )  # A Story linked twice still appears once.
        assert search("stories", "100%_ready")["total"] == 2
        assert search("stories", "100%_ready", 2)["items"][0]["id"] == first
        hidden = search("stories", "unmatched", 90)
        assert (hidden["total"], hidden["page"], hidden["pages"]) == (0, 1, 1)
        assert hidden["pending"] == 1 and not hidden["can_continue"]
        assert client.get(BASE + "stories/pending").json()["ids"] == [pages[0]["items"][0]["id"]]
        assert search("stories", "#18")["total"] == 1

        with repo.sessions.begin() as db:
            wizard = db.get(WorkspaceImport, pid)
            assert wizard is not None
            wizard.stage = "issues"
        assert search("issues", "Checkout")["total"] == 2
        assert search("issues", "Checkout", 2)["items"][0]["id"] == local_issue
        for query in ("#27", "27", "IMPORTED"):
            issue = search("issues", query)
            assert issue["items"][0]["number"] == 27 and issue["total"] == 1
        hidden = search("issues", "#2")
        assert hidden["total"] == 0 and hidden["pending"] == 3
        assert search("issues", "Pennies")["items"][0]["id"] == local_issue
        assert client.get(BASE + "epics", params={"search": "a" * 201}).status_code == 422
        assert client.get(BASE + "epics", params={"search": "a\x00b"}).status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_epic_link_locates_exact_active_row_in_project(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:
        elsewhere = repo.planning.save("llmwiki", "epics", "Same title", None, [])["id"]
        now = datetime.now(UTC)
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            db.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch="main",
                    stage="epics",
                )
            )
            db.add_all(
                WorkspaceCandidate(
                    id=UUID(int=n),
                    project_id=project.id,
                    kind="epics",
                    identity=str(n),
                    title="Same title",
                    created_at=now,
                )
                for n in (12, 11)
            )
            db.add_all(
                ProjectEpic(
                    id=UUID(int=n), project_id=project.id, title="Same title", created_at=now
                )
                for n in (5, 1, 4, 2, 3)
            )
        target = str(UUID(int=4))

        def locate(identifier: str, size: int = 2) -> dict:
            response = client.get(BASE + "epics", params={"epic": identifier, "page_size": size})
            assert response.status_code == 200, response.text
            return response.json()

        page = locate(target)
        assert (page["epic"], page["page"], page["total"]) == (target, 3, 7)
        assert page["items"][1]["id"] == target and not page["items"][1]["proposal"]
        assert locate(target, 5)["page"] == 2
        # The same link resolves again as earlier rows are removed. Global title matches
        # and lookalikes in another project cannot determine which record opens.
        confirmed(repo.planning, "falcone", "epics", UUID(int=1), "delete")
        confirmed(repo.planning, "falcone", "epics", UUID(int=2), "delete")
        assert locate(target)["page"] == 2
        confirmed(repo.planning, "falcone", "epics", UUID(target), "delete")
        for missing in (target, elsewhere):
            result = locate(missing)
            assert result["page"] == 1 and all(r["id"] != missing for r in result["items"])
        client.cookies.clear()
        assert client.get(BASE + "epics", params={"epic": target}).status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_tab_counts_track_all_visible_project_records(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    try:

        def counts() -> dict:
            response = client.get(BASE + "counts")
            assert response.status_code == 200, response.text
            return response.json()

        assert counts() == {"epics": 0, "stories": 0, "issues": 0}
        epic = repo.planning.save("falcone", "epics", "Payments", None, [])["id"]
        story = repo.planning.save("falcone", "stories", "Checkout", None, [UUID(epic)])["id"]
        repo.planning.save("falcone", "issues", "Draft", None, [UUID(story)])
        repo.planning.save("llmwiki", "epics", "Other project", None, [])
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            db.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch="main",
                    stage="epics",
                    version=1,
                )
            )
            db.add_all(
                WorkspaceCandidate(
                    project_id=project.id, kind=kind, identity=str(n), title=f"Candidate {n}"
                )
                for n, kind in enumerate(("epics", "epics", "stories"))
            )
        assert counts() == {"epics": 3, "stories": 1, "issues": 1}
        assert client.get(BASE + "epics", params={"search": "Payments"}).json()["total"] == 1
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]
        pending = client.get(BASE + "epics/pending").json()
        ids = pending["ids"]
        for identifier, dismiss in zip(ids, (False, True), strict=True):
            version = client.get(BASE + "epics/pending").json()["version"]
            done = client.post(
                BASE + "epics/decisions",
                json={
                    "csrf_token": csrf,
                    "ids": [identifier],
                    "version": version,
                    "dismiss": dismiss,
                },
            )
            assert done.status_code == 200, done.text
            assert counts()["epics"] == (2 if dismiss else 3)
        assert client.get(BASE.replace("falcone", "llmwiki") + "counts").json() == {
            "epics": 1,
            "stories": 0,
            "issues": 0,
        }
        assert client.get(BASE.replace("falcone", "unknown") + "counts").status_code == 404
        client.cookies.clear()
        assert client.get(BASE + "counts").status_code == 401
    finally:
        client.__exit__(None, None, None)
