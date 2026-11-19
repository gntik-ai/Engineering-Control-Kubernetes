import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from engineering_control.db.models import (
    BacklogItem,
    ProjectEpic,
    ProjectRecord,
    ProviderAccount,
    WorkspaceCandidate,
    WorkspaceImport,
    WorkspaceSource,
)
from engineering_control.github_app import GitHubAppError
from engineering_control.models.sealing import seal
from engineering_control.models.service import Models, Question, Reply
from tests.test_dashboard_web import _authenticated_client
from tests.test_models_api import said


class ReviewModel:
    async def aclose(self) -> None:
        pass

    async def ask(self, question: Question, *, retry: bool = True) -> Reply:
        data = json.loads(question.prompt)
        stage = question.system.split(".")[0].split()[-1]
        parents = [p["id"] for p in data.get("parents", [])]
        if "matches" in question.schema["properties"]:
            answer = {"matches": []}
        elif "epics" in question.schema["properties"]:
            assert not retry
            answer = {
                "complete": True,
                "epics": [
                    {"title": title, "description": "Project reporting capabilities"}
                    for title in ("Reporting", "Data exports")
                    if title not in data["existing"]
                ],
            }
        elif "records" in question.schema["properties"]:
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


def test_bulk_epics_use_one_request_and_remain_reviewable(tmp_path: Path) -> None:
    client, repo = _authenticated_client(tmp_path)
    base = "/api/dashboard/projects/falcone/"
    remote = PublishingFixture()
    client.app.state.github_app = remote
    requests = []
    replies = [
        {"complete": False, "epics": [{"title": "Partial", "description": "Incomplete"}]},
        {
            "complete": True,
            "epics": [
                {"title": "Partial", "description": "Valid first row"},
                {"title": " ", "description": "Missing title"},
            ],
        },
        {
            "complete": True,
            "epics": [
                {"title": title, "description": "An evidenced outcome"}
                for title in (
                    "PAYMENTS",
                    " Bookings ",
                    "Reporting",
                    "REPORTING",
                    "Exports",
                    "Alerts",
                )
            ],
        },
    ]

    def provider(request: httpx.Request) -> httpx.Response:
        assert (request.method, str(request.url)) == (
            "POST",
            "https://api.openai.com/v1/chat/completions",
        )
        body = json.loads(request.content)
        assert body["model"] == "analysis"
        assert (
            "Payments" in body["messages"][1]["content"]
            and "Bookings" in body["messages"][1]["content"]
        )
        assert "Approved booking platform" in body["messages"][1]["content"]
        assert "docs/architecture.md" in body["messages"][1]["content"]
        requests.append(body)
        if len(requests) == 3:
            repo.planning.save("falcone", "epics", "Alerts", "Entered while generating", [])
        return said(replies.pop(0))

    sealing = b"bulk-epic-test-key" * 3
    client.app.state.models = Models(
        repo.sessions, sealing, http=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    try:
        with repo.sessions.begin() as db:
            project = db.scalars(select(ProjectRecord).where(ProjectRecord.name == "falcone")).one()
            account_id = uuid4()
            db.add(
                ProviderAccount(
                    id=account_id,
                    name="API",
                    provider="openai",
                    state="ready",
                    models=["analysis"],
                    sealed_key=seal(sealing, account_id, "fixture"),
                    prices={"analysis": {"input": 1, "output": 2}},
                )
            )
            db.add(ProjectEpic(project_id=project.id, title="Payments"))
            db.add(
                WorkspaceImport(
                    project_id=project.id,
                    repository=project.source_repository,
                    branch=project.default_branch,
                    stage="epics",
                    version=4,
                    draft="Approved booking platform",
                    progress={
                        "evidence_notes": [
                            {
                                "references": ["docs/architecture.md"],
                                "summary": "Payments and exports",
                            }
                        ]
                    },
                )
            )
            db.flush()
            db.add(
                WorkspaceCandidate(
                    project_id=project.id,
                    kind="epics",
                    identity="issue:1",
                    number=1,
                    title="Bookings",
                    description="Booking tools",
                    parents=[],
                )
            )
            db.add(
                WorkspaceSource(
                    project_id=project.id, key="issue:1", kind="issue", evidence=remote.issues[1]
                )
            )
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]

        def advance(version: int, *, bulk: bool = True) -> httpx.Response:
            return client.post(
                base + "import/advance",
                json={
                    "csrf_token": csrf,
                    "version": version,
                    "all_epics": bulk,
                    "provider": "openai",
                    "model": "analysis",
                },
            )

        assert advance(4).status_code == 409
        before = advance(4, bulk=False).json()
        version = before["version"]
        assert before["review"]["mode"] == "gaps" and not requests
        for expected_calls, status in ((1, 409), (2, 422)):
            assert advance(version).status_code == status
            unchanged = client.get(base + "import").json()
            assert (unchanged["version"], unchanged["review"], unchanged["busy"]) == (
                version,
                before["review"],
                False,
            )
            assert len(requests) == expected_calls
            # A paid response is metered even when its proposed records fail validation.
            usage = repo.workspace_metrics.daily("falcone", 7)["series"]
            assert sum(d["tokens"] for d in usage) == 1280 * expected_calls
            assert sum(d["unpriced_tokens"] for d in usage) == 0

        done = advance(version).json()
        assert done["stage"] == "epics" and done["review"]["mode"] == "review"
        candidates = done["review"]["candidates"]
        assert {c["title"]: c["number"] for c in candidates} == {
            "Bookings": 1,
            "Exports": None,
            "Reporting": None,
        }
        assert len(candidates) == 3 and len(requests) == 3
        assert advance(version).status_code == 409 and len(requests) == 3
        assert client.get(base + "import").json()["review"]["candidates"] == candidates
        assert {c["title"] for c in client.get(base + "planning").json()["epics"]} == {
            "Payments",
            "Alerts",
        }
        assert client.get(base + "synchronization").json()["items"] == [] and not remote.closed

        assert done["review"]["bulk_complete"]
        for bulk in (True, False):
            assert advance(done["version"], bulk=bulk).json() == done
        assert len(requests) == 3
        assert (
            sum(d["tokens"] for d in repo.workspace_metrics.daily("falcone", 7)["series"]) == 3840
        )
        choices = [
            {k: c[k] for k in ("id", "title", "description", "parents")}
            | {"include": c["title"] == "Exports"}
            for c in candidates
        ]
        selected = next(c for c in choices if c["include"])
        selected["title"] = "Custom exports"
        approved = client.post(
            base + "import/review",
            json={"csrf_token": csrf, "version": done["version"], "choices": choices},
        ).json()
        assert approved["stage"] == "stories"
        assert {c["title"] for c in client.get(base + "planning").json()["epics"]} == {
            "Payments",
            "Alerts",
            "Custom exports",
        }
        assert advance(approved["version"]).status_code == 409 and len(requests) == 3
    finally:
        client.__exit__(None, None, None)


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
