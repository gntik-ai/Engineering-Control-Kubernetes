from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from engineering_control.config import Settings
from engineering_control.dashboard.app import create_app
from engineering_control.dashboard.repository import DashboardRepository
from engineering_control.db.base import Base

PASSWORD = "correct horse battery staple"  # noqa: S105 - deterministic test fixture


def _csrf(text: str) -> str:
    match = re.search(r'"csrf_token":\s*"([^"]+)"', text)
    assert match
    return match.group(1)


def _authenticated_client(tmp_path: Path) -> tuple[TestClient, DashboardRepository]:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    client = TestClient(
        create_app(
            settings=Settings(database_url=str(repository.engine.url)),
            repository=repository,
            session_key=b"w" * 48,
        ),
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    )
    client.__enter__()
    login = client.get("/login/challenge")
    response = client.post(
        "/login",
        data={"csrf_token": _csrf(login.text), "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client, repository


def test_only_the_console_and_sign_in_pages_remain(tmp_path: Path) -> None:
    client, repository = _authenticated_client(tmp_path)
    try:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 303
        assert root.headers["location"] == "/console"
        # The server-rendered pages are gone; their data stays behind /api/dashboard.
        for path in (
            "/overview",
            "/backlog",
            "/activity",
            "/workflows",
            "/workflows/known-workflow",
            "/executions/00000000-0000-0000-0000-000000000000",
            "/logs",
            "/audits",
            "/commands",
            "/findings",
            "/enhancements",
            "/connections",
            "/settings",
            "/static/dashboard.js",
            "/static/dashboard.css",
            "/login/unknown",
        ):
            response = client.get(path)
            assert response.status_code == 404
            assert response.json() == {"detail": "not found"}
            assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert client.post("/settings/password", data={}).status_code == 404
        html_routes = {
            route.path
            for route in client.app.routes
            if "GET" in getattr(route, "methods", set())
            and not route.path.startswith(("/api/", "/health/", "/internal/", "/console"))
        }
        public = {"/", "/login", "/login/challenge", "/robots.txt"}
        assert html_routes >= public
        assert html_routes <= public | {"/docs", "/redoc", "/openapi.json"}
        login_page = client.get("/login", follow_redirects=False)
        assert login_page.status_code == 303
        assert login_page.headers["location"] == "/console"
        # The old page's stylesheet and icon are gone with the page.
        assert client.get("/login?asset=css", follow_redirects=False).status_code == 303
        assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
        client.cookies.clear()
        assert client.get("/overview", follow_redirects=False).headers["location"] == "/login"
    finally:
        client.__exit__(None, None, None)
        repository.engine.dispose()


def test_dashboard_apis_require_session_and_return_empty_states(tmp_path: Path) -> None:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    app = create_app(
        settings=Settings(database_url=str(repository.engine.url)),
        repository=repository,
        session_key=b"a" * 48,
    )
    with TestClient(
        app,
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    ) as client:
        listings = (
            "projects",
            "items",
            "inbox",
            "queues",
            "runs",
            "flows",
            "accounts",
            "budgets",
            "usage",
            "supervisor",
        )
        for name in listings:
            assert client.get(f"/api/dashboard/{name}").status_code == 401
        page = client.get("/login/challenge")
        client.post("/login", data={"csrf_token": _csrf(page.text), "password": PASSWORD})
        for name in ("items", "queues", "runs", "accounts"):
            assert client.get(f"/api/dashboard/{name}").json()["items"] == []
        projects = client.get("/api/dashboard/projects").json()["items"]
        assert projects
        assert {project["open_items"] for project in projects} == {0}
        assert {project["active_runs"] for project in projects} == {0}
        assert client.get("/api/dashboard/items?project=outside").status_code == 422
        assert client.get("/api/dashboard/runs?project=outside").status_code == 422
        # What the original's dashboard listed is gone with the original's way of working.
        for name in (
            "overview",
            "workflows",
            "executions",
            "services",
            "root-blockers",
            "findings",
            "enhancements",
            "audits",
            "commands",
            "connections",
            "activity/stream",
        ):
            assert client.get(f"/api/dashboard/{name}").status_code == 404


def test_dashboard_exposes_only_typed_mutations_and_no_raw_journal_route(
    tmp_path: Path,
) -> None:
    repository = DashboardRepository(f"sqlite+pysqlite:///{tmp_path / 'routes.db'}")
    Base.metadata.create_all(repository.engine)
    repository.initialize_password_sync(PASSWORD)
    app = create_app(
        settings=Settings(database_url=str(repository.engine.url)),
        repository=repository,
        session_key=b"r" * 48,
    )
    post_routes = {route.path for route in app.routes if "POST" in getattr(route, "methods", set())}
    assert post_routes == {
        "/api/dashboard/accounts",
        "/api/dashboard/accounts/{account_id}/update",
        "/api/dashboard/accounts/{account_id}/sign-in",
        "/api/dashboard/accounts/{account_id}/remove",
        "/api/dashboard/accounts/{account_id}/try",
        "/api/dashboard/accounts/{account_id}/check",
        "/api/dashboard/runs/{run_id}/cancel",
        "/api/dashboard/flows",
        "/api/dashboard/flows/check",
        "/api/dashboard/flows/saved-tasks",
        "/api/dashboard/flows/saved-tasks/{saved_id}/remove",
        "/api/dashboard/projects/{name}/commands",
        "/api/dashboard/projects/{name}/commands/try",
        "/api/dashboard/projects/{name}/summaries",
        "/api/dashboard/projects/{name}/summaries/forget",
        "/api/dashboard/tasks",
        "/api/dashboard/tasks/{saved_id}",
        "/api/dashboard/tasks/{saved_id}/remove",
        "/api/dashboard/tasks/{saved_id}/apply",
        "/api/dashboard/agents",
        "/api/dashboard/agents/{agent_id}",
        "/api/dashboard/agents/{agent_id}/remove",
        "/api/dashboard/skills",
        "/api/dashboard/skills/{skill_id}",
        "/api/dashboard/skills/{skill_id}/remove",
        "/api/dashboard/task-kinds/{task}/hidden",
        "/api/dashboard/projects/{name}/triggers",
        "/api/dashboard/triggers/{trigger_id}/update",
        "/api/dashboard/triggers/{trigger_id}/remove",
        "/api/dashboard/triggers/{trigger_id}/fire",
        "/api/dashboard/flows/{key}/draft",
        "/api/dashboard/flows/{key}/draft/discard",
        "/api/dashboard/flows/{key}/publish",
        "/api/dashboard/flows/{key}/simulate",
        "/api/dashboard/flows/{key}/evaluations",
        "/api/dashboard/evaluations/{evaluation_id}/stop",
        "/api/dashboard/runs/{run_id}/decide",
        "/api/dashboard/items/{item_id}/deliver",
        "/api/dashboard/items/{item_id}/edit",
        "/api/dashboard/projects/{name}/bring-over",
        "/api/dashboard/projects/{name}/bring-over/preview",
        "/api/dashboard/projects/{name}/cut-over",
        "/api/dashboard/projects/{name}/application",
        "/api/dashboard/projects/{name}/pause",
        "/api/dashboard/projects/{name}/resume",
        "/api/dashboard/supervisor/incidents/{incident_id}/dismiss",
        "/api/dashboard/supervisor/model",
        "/api/dashboard/projects/{name}/sections/{key}/run",
        "/api/dashboard/projects/{name}/queues",
        "/api/dashboard/queues/{queue_id}/add",
        "/api/dashboard/queues/{queue_id}/order",
        "/api/dashboard/queues/{queue_id}/update",
        "/api/dashboard/queues/{queue_id}/start",
        "/api/dashboard/queues/{queue_id}/pause",
        "/api/dashboard/queues/{queue_id}/discard",
        "/api/dashboard/queues/{queue_id}/items/{item_id}/remove",
        "/api/dashboard/budgets",
        "/api/dashboard/supervisor/messages",
        "/api/dashboard/supervisor/stop",
        "/api/dashboard/supervisor/settings",
        "/api/dashboard/supervisor/actions/{action_id}/decide",
        "/login",
        "/logout",
        "/api/dashboard/projects",
        "/api/dashboard/projects/{name}/rename",
        "/api/dashboard/projects/{name}/archive",
        "/api/dashboard/projects/{name}/sections",
        "/api/dashboard/projects/{name}/sections/{key}/update",
        "/api/dashboard/projects/{name}/sections/{key}/remove",
        "/api/dashboard/projects/{name}/sections/{key}/flow",
        "/api/dashboard/projects/{name}/backlog/sync",
        "/api/dashboard/projects/{name}/items",
        "/api/dashboard/projects/{name}/items/bulk",
        "/api/dashboard/items/{item_id}/route",
        "/api/dashboard/items/{item_id}/state",
    }
    paths = {route.path for route in app.routes}
    assert not any("journal" in path or "shell" in path or "terminal" in path for path in paths)
    assert not any("codex" in path or "claude" in path for path in paths)

    with TestClient(
        app,
        base_url="https://ade.musematic.ai",
        headers={"Origin": "https://ade.musematic.ai"},
    ) as client:
        login = client.get("/login/challenge")
        client.post("/login", data={"csrf_token": _csrf(login.text), "password": PASSWORD})


def test_signing_in_again_replaces_the_session_and_the_old_cookie_is_refused(
    tmp_path: Path,
) -> None:
    from engineering_control.dashboard.auth import SESSION_COOKIE

    client, repository = _authenticated_client(tmp_path)
    try:
        old = client.cookies.get(SESSION_COOKIE)
        assert old
        assert client.get("/api/dashboard/projects").status_code == 200
        # A second sign-in that presents the first session's cookie takes its place.
        client.cookies.clear()
        page = client.get("/login/challenge")
        client.cookies.set(SESSION_COOKIE, old, domain="ade.musematic.ai", path="/")
        response = client.post(
            "/login",
            data={"csrf_token": _csrf(page.text), "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303
        new = client.cookies.get(SESSION_COOKIE)
        assert new and new != old
        assert client.get("/api/dashboard/projects").status_code == 200
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, old)
        assert client.get("/console", follow_redirects=False).status_code == 303
        assert client.get("/api/dashboard/projects").status_code == 401
    finally:
        client.__exit__(None, None, None)
        repository.engine.dispose()
