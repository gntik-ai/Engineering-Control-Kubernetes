import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from engineering_control.db.models import (
    BacklogItem,
    ProjectRecord,
    ProviderAccount,
    WorkspaceImport,
    WorkspaceSource,
)
from engineering_control.github_app import GitHubAppError
from engineering_control.models.service import Question, Reply
from tests.test_dashboard_web import _authenticated_client


class ReviewModel:
    async def aclose(self) -> None:
        pass

    async def ask(self, question: Question) -> Reply:
        data = json.loads(question.prompt)
        stage = question.system.split(".")[0].split()[-1]
        parents = [p["id"] for p in data["parents"]]
        if "records" in question.schema["properties"]:
            hints = {"epics": "epic", "stories": "story", "issues": "tech"}
            answer = {
                "records": [
                    {
                        "include": hints[stage] in r["labels"],
                        "number": r["number"],
                        "title": r["title"],
                        "description": r["body"],
                        "parents": parents,
                    }
                    for r in data["github_issues"]
                ]
            }
        else:
            title = {
                "epics": "Reporting",
                "stories": "Export reports",
                "issues": "Add export endpoint",
            }[stage]
            answer = {
                "complete": title in data["existing"],
                "title": title,
                "description": "Missing coverage",
                "parents": parents,
            }
        return Reply(answer, "", "fixture", "analysis", uuid4(), "Fixture", {"tokens": 30}, 0.1, 1)


class PublishingFixture:
    def __init__(self) -> None:
        self.issues = {
            n: {
                "number": n,
                "state": state,
                "title": name,
                "body": name + " details",
                "labels": [kind],
                "closed_at": "2026-10-06T09:10:00Z" if state == "closed" else None,
                "html_url": f"https://github.com/gntik-ai/falcone/issues/{n}",
            }
            for n, kind, name, state in (
                (1, "epic", "Bookings", "open"),
                (2, "story", "View seats", "closed"),
                (3, "tech", "Render seats", "closed"),
                (4, "tech", "Seat filter", "open"),
            )
        }
        self.closed: list[int] = []
        self.creates = 0
        self.lost = True

    async def repository_token(self, repository: str, permissions: dict[str, str]) -> str:
        assert repository == "gntik-ai/falcone" and permissions == {"issues": "write"}
        return "fixture"

    async def revoke(self, token: str) -> None:
        pass

    async def workspace_issue(self, repository: str, number: int) -> dict[str, Any]:
        return dict(self.issues[number])

    async def close_workspace_issue(self, repo: str, number: int, *, token: str) -> None:
        self.issues[number]["state"] = "closed"
        self.closed.append(number)

    async def create_workspace_issue(self, repo: str, title: str, body: str, *, token: str) -> int:
        self.creates += 1
        self.issues[100] = {"number": 100, "title": title, "body": body, "state": "open"}
        if self.lost:
            self.lost = False
            raise GitHubAppError("GitHub did not answer")
        return 100

    async def workspace_published(self, repo: str, marker: str) -> int | None:
        return next((n for n, issue in self.issues.items() if marker in issue["body"]), None)


def test_mixed_history_human_gates_and_uncertain_publication(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    remote = PublishingFixture()
    client.app.state.github_app = remote
    client.app.state.models = ReviewModel()
    try:
        with repo.sessions.begin() as session:
            project = session.scalar(select(ProjectRecord).where(ProjectRecord.name == "falcone"))
            assert project is not None
            session.add(
                ProviderAccount(
                    name="Configured",
                    provider="fixture",
                    state="ready",
                    models=["analysis"],
                    sealed_key="fixture",
                )
            )
            session.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch="main",
                    stage="epics",
                    version=1,
                    draft="Transport",
                    model="analysis",
                    provider="fixture",
                    progress={},
                )
            )
            session.flush()
            session.add_all(
                WorkspaceSource(
                    project_id=project.id, key=f"issue:{n}", kind="issue", evidence=issue
                )
                for n, issue in remote.issues.items()
            )
        base = "/api/dashboard/projects/falcone/"
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]

        def post(path: str, **body: Any) -> Any:
            return client.post(base + path, json={"csrf_token": csrf, **body})

        def advance(current: dict[str, Any]) -> dict[str, Any]:
            response = post(
                "import/advance", version=current["version"], provider="fixture", model="analysis"
            )
            assert response.status_code == 200, response.text
            return response.json()

        state = client.get(base + "import").json()
        for kind in ("epics", "stories", "issues"):
            assert state["stage"] == kind
            assert post("import/review", version=state["version"], choices=[]).status_code == 409
            state = advance(state)
            assert state["review"]["mode"] == "gaps"
            count = len(state["review"]["candidates"])
            state = advance(state)
            assert len(state["review"]["candidates"]) == count + 1
            state = advance(state)
            assert state["review"]["mode"] == "review"
            choices = [
                {k: c[k] for k in ("id", "title", "description", "parents")} | {"include": True}
                for c in state["review"]["candidates"]
            ]
            if kind != "epics":
                broken = [{**choices[0], "parents": [str(uuid4())]}, *choices[1:]]
                assert (
                    post("import/review", version=state["version"], choices=broken).status_code
                    == 422
                )
            saved = post("import/review", version=state["version"], choices=choices)
            assert saved.status_code == 200, saved.text
            assert (
                post("import/review", version=state["version"], choices=choices).status_code == 409
            )
            state = saved.json()
        assert state["stage"] == "complete" and remote.closed == [] and remote.creates == 0
        actions = client.get(base + "synchronization").json()["items"]
        assert [action["number"] for action in actions] == [1]
        assert post("synchronization/" + actions[0]["id"]).json()["status"] == "done"
        assert post("synchronization/" + actions[0]["id"]).json()["status"] == "done"
        assert remote.closed == [1]
        planning = client.get(base + "planning").json()
        assert len(planning["epics"]) == 2 and len(planning["stories"]) == 2
        assert all(len(story["epic_ids"]) == 2 for story in planning["stories"])
        for issue in planning["issues"]:
            assert issue["review"] == "pending" and len(issue["story_ids"]) == 2
            result = post("planning/issues/" + issue["id"] + "/approve").json()
            if issue["number"]:
                assert result["status"] == "done"
                assert remote.issues[issue["number"]]["state"] == issue["github_state"]
            else:
                assert result["status"] == "needs_attention"
                again = post("planning/issues/" + issue["id"] + "/approve")
                assert again.json()["status"] == "done" and remote.creates == 1
        assert remote.closed == [1]
        assert {i["number"] for i in client.get(base + "planning").json()["issues"]} == {3, 4, 100}
        assert all(
            i["review"] == "approved" for i in client.get(base + "planning").json()["issues"]
        )
        with repo.sessions() as session:
            historical = session.scalar(select(BacklogItem).where(BacklogItem.issue_number == 3))
            assert historical is not None and historical.state == "done"
            assert historical.github_closed_at.replace(tzinfo=UTC) == datetime(
                2026, 10, 6, 9, 10, tzinfo=UTC
            )
    finally:
        client.__exit__(None, None, None)


def test_no_github_planning_issues_generate_epics_individually(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    client.app.state.models = ReviewModel()
    try:
        with repo.sessions.begin() as db:
            project = db.scalar(select(ProjectRecord).where(ProjectRecord.name == "llmwiki"))
            assert project is not None
            db.add(
                ProviderAccount(
                    name="Review model",
                    provider="fixture",
                    state="ready",
                    models=["analysis"],
                    sealed_key="fixture",
                )
            )
            db.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch=project.default_branch,
                    stage="epics",
                    version=3,
                    draft="Trip reporting",
                    provider="fixture",
                    model="analysis",
                    progress={},
                )
            )
        base = "/api/dashboard/projects/llmwiki/"
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]

        def next_step(version: int) -> dict[str, Any]:
            response = client.post(
                base + "import/advance",
                json={
                    "csrf_token": csrf,
                    "version": version,
                    "provider": "fixture",
                    "model": "analysis",
                },
            )
            assert response.status_code == 200
            return dict(response.json())

        assert next_step(3)["review"]["candidates"] == []
        one = next_step(4)
        assert (
            len(one["review"]["candidates"]) == 1
            and one["review"]["candidates"][0]["number"] is None
        )
        assert client.get(base + "planning").json()["epics"] == []
        done = next_step(5)
        assert done["review"]["mode"] == "review"
        c = done["review"]["candidates"][0]
        choice = {key: c[key] for key in ("id", "title", "description", "parents")}
        result = client.post(
            base + "import/review",
            json={
                "csrf_token": csrf,
                "version": done["version"],
                "choices": [{**choice, "include": True}],
            },
        )
        assert result.json()["stage"] == "stories"
        assert [e["title"] for e in client.get(base + "planning").json()["epics"]] == ["Reporting"]
        assert client.get(base + "synchronization").json()["items"] == []
    finally:
        client.__exit__(None, None, None)
