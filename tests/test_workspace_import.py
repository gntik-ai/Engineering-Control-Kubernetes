import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select

from engineering_control.dashboard.workspace_import import WorkspaceImports
from engineering_control.db.models import ProviderAccount, WorkspaceImport, WorkspaceSource
from engineering_control.github_app import GitHubApp, Issue
from engineering_control.models.sealing import seal
from engineering_control.models.service import Models, Question, Reply
from tests.test_dashboard_web import _authenticated_client
from tests.test_github_app import _key_file

SHA = "a" * 40
TREE = "b" * 40
BLOB = "c" * 40


class RepositoryFixture:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.fail_page = not empty
        self.pages: list[int] = []
        self.grants: list[dict[str, Any]] = []
        self.revokes = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            self.grants.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={
                    "token": "installation-fixture",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        if path == "/installation/token":
            self.revokes += 1
            return httpx.Response(204)
        assert request.method == "GET"
        if "/git/ref/" in path:
            return httpx.Response(409 if self.empty else 200, json={"object": {"sha": SHA}})
        if path == "/repos/gntik-ai/falcone":
            return httpx.Response(200, json={"size": 0})
        if "/git/commits/" in path:
            return httpx.Response(200, json={"tree": {"sha": TREE}})
        if "/git/trees/" in path:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {
                            "path": "documentation/architecture.md",
                            "type": "blob",
                            "sha": BLOB,
                            "size": 22,
                        },
                        {"path": "src/main.py", "type": "blob", "sha": BLOB, "size": 22},
                    ],
                    "truncated": False,
                },
            )
        if "/git/blobs/" in path:
            return httpx.Response(
                200,
                json={
                    "content": base64.b64encode(b"Provides public transit routes.").decode(),
                    "encoding": "base64",
                },
            )
        assert path.endswith("/issues")
        assert dict(request.url.params)["state"] == "all"
        page = int(request.url.params["page"])
        self.pages.append(page)
        if self.empty:
            return httpx.Response(200, json=[])
        if page == 2 and self.fail_page:
            self.fail_page = False
            return httpx.Response(503, json={})
        issue = {
            "number": page,
            "title": "Route history",
            "body": "Transit timetable",
            "state": "open" if page == 1 else "closed",
            "labels": [],
            "closed_at": None if page == 1 else "2026-10-06T09:10:00Z",
        }
        # A full page of mostly pull requests still requires fetching the next page.
        return httpx.Response(
            200, json=[issue] + ([{"pull_request": {}}] * 99 if page == 1 else [])
        )


class FixtureModel:
    def __init__(self) -> None:
        self.questions: list[Question] = []

    async def aclose(self) -> None:
        pass

    async def ask(self, question: Question, *, retry: bool = True) -> Reply:
        self.questions.append(question)
        return Reply(
            {"description": "A transit routing service."},
            "",
            "fixture",
            "analysis",
            uuid4(),
            "Test",
            {"tokens": 21},
            0.1,
            1,
        )


def test_existing_project_uses_durable_history_and_explicit_description_approval(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    remote = RepositoryFixture()
    model = FixtureModel()
    client.app.state.github_app = GitHubApp(
        app_id=10,
        installation_id=11,
        private_key_file=_key_file(tmp_path),
        transport=httpx.MockTransport(remote),
    )
    client.app.state.models = model
    try:
        with repo.sessions.begin() as db:
            db.add(
                ProviderAccount(
                    name="configured",
                    provider="fixture",
                    state="ready",
                    models=["analysis"],
                    sealed_key="fixture",
                )
            )
        path = "/api/dashboard/projects/falcone/import"
        csrf = client.get("/api/dashboard/session").json()["csrf_token"]

        def post(action: str, **body: Any) -> Any:
            return client.post(path + "/" + action, json={"csrf_token": csrf, **body})

        assert client.get(path).json()["stage"] == "new"
        assert post("start").json()["stage"] == "inventory"
        assert post("description", version=0, description="Too early").status_code == 409
        assert post("advance", version=0).json()["commit"] == SHA
        assert post("advance", version=0).status_code == 409
        assert post("advance", version=1).json()["stage"] == "history"
        assert post("advance", version=2).json()["counts"]["issues_open"] == 1
        assert post("advance", version=3).status_code == 502
        # Rebuild the importer as if the console restarted after the failed page.
        repo.workspace_imports = WorkspaceImports(repo.sessions)
        saved = client.get(path).json()
        assert saved["version"] == 3 and not saved["busy"]
        assert post("advance", version=3).json()["stage"] == "analysis"
        assert remote.pages == [1, 2, 2]
        assert post("advance", version=4, provider="another", model="model").status_code == 422
        found = post("advance", version=4, provider="fixture", model="analysis").json()
        assert (
            found["description"] == "A transit routing service." and found["stage"] == "description"
        )
        assert client.get("/api/dashboard/projects/llmwiki/import").json()["stage"] == "new"
        with repo.sessions() as db:
            sources = list(db.scalars(select(WorkspaceSource)))
            assert len(sources) == 4
            assert (
                next(s for s in sources if s.key == "issue:2").evidence["closed_at"]
                == "2026-10-06T09:10:00Z"
            )
        assert len(model.questions) == 1
        assert "documentation/architecture.md" in model.questions[0].prompt
        assert "Route history" in model.questions[0].prompt
        assert remote.revokes == 2
        assert not any("write" in g.get("permissions", {}).values() for g in remote.grants)
        github_issue = Issue(5, "Await review", "", (), None)
        counts = repo.backlog.apply_import("falcone", [github_issue], complete=True)
        assert counts["imported"] == 0
        assert (
            post(
                "description", version=found["version"], description="Approved transit description."
            ).json()["stage"]
            == "epics"
        )
        listing = client.get("/api/dashboard/projects").json()["items"]
        assert (
            next(p for p in listing if p["project"] == "falcone")["description"]
            == "Approved transit description."
        )
    finally:
        client.__exit__(None, None, None)


def test_refresh_exposes_the_full_account_catalog_without_advancing_an_import(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    path = "/api/dashboard/projects/falcone/import"
    refresh = "/api/dashboard/account-models/refresh"
    available = [f"earlier-{i:03d}" for i in range(220)] + ["gpt-6.2-astra", "gpt-6.2-sol"]
    responses = [
        httpx.Response(200, json={"data": [{"id": m} for m in available]}),
        httpx.Response(200, json={"error": "unavailable"}),
    ]

    def provider(request: httpx.Request) -> httpx.Response:
        assert (request.method, str(request.url)) == ("GET", "https://api.openai.com/v1/models")
        return responses.pop(0)

    sealing = b"fixture" * 7
    client.app.state.models = Models(
        repo.sessions, sealing, http=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    try:
        csrf = {"csrf_token": client.get("/api/dashboard/session").json()["csrf_token"]}
        client.post(path + "/start", json=csrf).raise_for_status()
        uid = uuid4()
        with repo.sessions.begin() as db:
            row = db.scalars(select(WorkspaceImport)).one()
            row.stage, row.provider, row.model = "analysis", "openai", available[0]
            db.add(
                ProviderAccount(
                    id=uid,
                    name="API",
                    provider="openai",
                    state="limited",
                    limited_until=datetime.now(UTC) + timedelta(minutes=5),
                    sealed_key=seal(sealing, uid, "fixture"),
                    models=available[:1],
                )
            )
            db.add(
                ProviderAccount(
                    name="Paused",
                    provider="openai",
                    state="ready",
                    paused=True,
                    sealed_key="unused",
                )
            )
        before = client.get(path).json()
        assert client.post(refresh, json={"csrf_token": "x" * 40}).status_code == 403
        assert client.post(refresh, json=csrf).json() == {"refreshed": 1, "unavailable": 0}
        after = client.get(path).json()
        assert [m["model"] for m in after["models"]] == available
        assert {**after, "models": before["models"]} == before
        assert client.post(refresh, json=csrf).json() == {"refreshed": 0, "unavailable": 1}
        assert client.get(path).json() == after
        assert not responses
        with repo.sessions() as db:
            account = db.get(ProviderAccount, uid)
            assert account and account.state == "limited" and account.limited_until
    finally:
        client.__exit__(None, None, None)


def test_empty_repository_accepts_human_description_without_model_or_github_writes(
    tmp_path: Path,
) -> None:
    client, repo = _authenticated_client(tmp_path)
    remote = RepositoryFixture(empty=True)
    client.app.state.github_app = GitHubApp(
        app_id=10,
        installation_id=11,
        private_key_file=_key_file(tmp_path),
        transport=httpx.MockTransport(remote),
    )
    try:
        path = "/api/dashboard/projects/falcone/import"
        csrf = {"csrf_token": client.get("/api/dashboard/session").json()["csrf_token"]}
        client.post(path + "/start", json=csrf).raise_for_status()
        assert (
            client.post(path + "/advance", json={**csrf, "version": 0}).json()["stage"] == "history"
        )
        blank = client.post(path + "/advance", json={**csrf, "version": 1}).json()
        assert blank["stage"] == "description" and not blank["description"]
        assert not remote.pages[1:]
        created = repo.projects.create(
            name="transit", display_name="Transit", repository="gntik-ai/new-example"
        )
        assert repo.workspace_imports.status(created["project"])["stage"] == "inventory"
    finally:
        client.__exit__(None, None, None)
